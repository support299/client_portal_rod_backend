from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from rest_framework import status
from rest_framework.generics import ListAPIView, RetrieveUpdateAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import SAFE_METHODS, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.views import IsStaffMember
from ghl.models import GHLPipelineStage
from ghl.outbound import update_opportunity_in_ghl
from ghl.services import GHLAPIError
from ghl.views import visible_opportunities

from .models import Loan
from .serializers import BorrowerLoanSerializer, LoanSerializer, LoanUpdateSerializer
from .services import LoanConversionError, LoanService


def visible_loans(user):
    """Admins see every loan, borrowers their own loans, other staff the loans assigned to them."""
    qs = (
        Loan.objects.filter(is_deleted=False)
        .select_related("borrower", "opportunity__contact", "opportunity__stage")
        .prefetch_related("officer_assignments__officer")
    )
    if user.is_app_admin:
        return qs
    if user.role == user.Role.BORROWER:
        return qs.filter(borrower=user)
    return qs.filter(officer_assignments__officer=user, officer_assignments__is_deleted=False)


def loan_read_serializer(user):
    """Borrowers get a trimmed loan without internal CRM fields."""
    return BorrowerLoanSerializer if user.role == user.Role.BORROWER else LoanSerializer


class LoanPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


class LoanFromOpportunityView(APIView):
    """Convert a GHL opportunity into a Loan, creating the contact's borrower login if needed.

    Allowed for admins, and for the staff user the opportunity is assigned to.
    Body: opportunity_id (the GHL opportunity id).
    """

    permission_classes = [IsStaffMember]

    def post(self, request):
        opportunity_id = request.data.get("opportunity_id")
        if not opportunity_id:
            return Response({"error": "opportunity_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        opportunity = (
            visible_opportunities(request.user).select_related("contact", "stage")
            .filter(opportunity_id=opportunity_id).first()
        )
        if opportunity is None:
            return Response({"error": "opportunity not found"}, status=status.HTTP_404_NOT_FOUND)

        try:
            loan, borrower_created = LoanService.create_from_opportunity(opportunity, created_by=request.user)
        except LoanConversionError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except IntegrityError:
            # Two concurrent clicks: the unique opportunity link rejects the second.
            return Response({"error": "A loan has already been created for this opportunity."},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response(
            {"borrower_created": borrower_created, "loan": LoanSerializer(loan).data},
            status=status.HTTP_201_CREATED,
        )


class LoanStagesView(APIView):
    """The loan pipeline's stages in order; a loan's `status` is one of these ids."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        stages = GHLPipelineStage.objects.filter(pipeline_id=settings.LOAN_PIPELINE_ID).order_by("position")
        return Response([{"id": s.stage_id, "name": s.name.strip(), "position": s.position} for s in stages])


class LoanListView(ListAPIView):
    """List loans: admins see all, borrowers their own, other staff those assigned to them.

    Query params (all optional): status (a stage id from LoanStagesView), borrower, loan_officer, search (loan name / borrower email), page, page_size.
    """

    pagination_class = LoanPagination
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        return loan_read_serializer(self.request.user)

    def get_queryset(self):
        params = self.request.query_params
        qs = visible_loans(self.request.user)
        if params.get("status"):
            qs = qs.filter(opportunity__pipeline_stage_id=params["status"])
        if params.get("borrower"):
            qs = qs.filter(borrower=params["borrower"])
        if params.get("loan_officer"):
            qs = qs.filter(officer_assignments__officer=params["loan_officer"], officer_assignments__is_deleted=False)
        search = params.get("search", "").strip()
        if search:
            qs = qs.filter(Q(opportunity__name__icontains=search) | Q(borrower__email__icontains=search))
        return qs.order_by("-created_at", "-id")


class LoanDetailView(RetrieveUpdateAPIView):
    """Get (GET) or edit (PATCH partial, PUT full) one loan.

    Anyone gets a 404 for loans they can't see (see visible_loans). Borrowers are read-only;
    non-admin staff can't change loan_officer.
    Editable: loan_officer, and name / loan_amount / expected_close_date. The last three
    belong to the GHL opportunity, so they are pushed to GHL first (502 if GHL rejects the change).
    """

    def get_permissions(self):
        if self.request.method in SAFE_METHODS:
            return [IsAuthenticated()]
        return [IsStaffMember()]

    def get_queryset(self):
        return visible_loans(self.request.user)

    def get_serializer_class(self):
        if self.request.method in ("PUT", "PATCH"):
            return LoanUpdateSerializer
        return loan_read_serializer(self.request.user)

    def update(self, request, *args, **kwargs):
        try:
            return super().update(request, *args, **kwargs)
        except GHLAPIError as exc:
            return Response({"error": f"GHL rejected the update: {exc}"}, status=status.HTTP_502_BAD_GATEWAY)

    @transaction.atomic
    def perform_update(self, serializer):
        data = dict(serializer.validated_data)
        shared = {}
        for field, local in (("name", "name"), ("loan_amount", "monetary_value"),
                             ("expected_close_date", "expected_close_date")):
            if field in data:
                shared[local] = data.pop(field)
        officers = data.pop("loan_officers", None)
        serializer.validated_data.clear()
        serializer.validated_data.update(data)
        serializer.save()
        if officers is not None:
            LoanService.set_officers(serializer.instance, officers)
        if shared:
            update_opportunity_in_ghl(serializer.instance.opportunity, triggered_by=self.request.user, **shared)
