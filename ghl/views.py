import secrets
from urllib.parse import quote, urlencode

from django.conf import settings
from django.db.models import F, OuterRef, Q, Subquery
from django.http import HttpResponseRedirect
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.generics import ListAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .client import AUTHORIZE_URL, GHLAuthError, exchange_code
from .models import GHLOpportunity, GHLPipelineStage, GHLToken, GHLUser
from .serializers import GHLConnectionSerializer, OpportunitySerializer, UserSerializer
from .tasks import sync_ghl_location


@api_view(["GET"])
@permission_classes([AllowAny])
def oauth_start(request):
    """Redirect the user to GHL to pick a location and approve the app."""
    state = secrets.token_urlsafe(32)
    request.session["ghl_oauth_state"] = state
    params = {
        "response_type": "code",
        "client_id": settings.GHL_CLIENT_ID,
        "redirect_uri": settings.GHL_REDIRECT_URI,
        "scope": settings.GHL_SCOPES,
        "state": state,
    }
    if settings.GHL_VERSION_ID:
        params["version_id"] = settings.GHL_VERSION_ID
    return HttpResponseRedirect(f"{AUTHORIZE_URL}?{urlencode(params, quote_via=quote)}")


@api_view(["GET"])
@permission_classes([AllowAny])
def oauth_callback(request):
    """GHL redirects here with ?code=... after the user approves."""
    if request.query_params.get("error"):
        return Response({"error": request.query_params["error"]}, status=status.HTTP_400_BAD_REQUEST)

    # Only enforce state when the flow was started from oauth_start; installs
    # launched directly from the GHL Marketplace arrive without one.
    expected_state = request.session.pop("ghl_oauth_state", None)
    if expected_state and request.query_params.get("state") != expected_state:
        return Response({"error": "invalid state"}, status=status.HTTP_400_BAD_REQUEST)

    code = request.query_params.get("code")
    if not code:
        return Response({"error": "missing code"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        token = exchange_code(code)
    except GHLAuthError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    # Pull the new location's data in the background so the redirect isn't held up.
    if token.location_id:
        sync_ghl_location.delay(token.location_id)

    if settings.GHL_SUCCESS_REDIRECT:
        return HttpResponseRedirect(settings.GHL_SUCCESS_REDIRECT)
    return Response({"connected": True, **GHLConnectionSerializer(token).data})


def visible_opportunities(user):
    """Admins see every opportunity; everyone else only those assigned to their linked GHL user."""
    qs = GHLOpportunity.objects.all()
    if user.is_app_admin:
        return qs
    return qs.filter(assigned_to__in=GHLUser.objects.filter(account=user).values("user_id"))


class OpportunityPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


class OpportunityListView(ListAPIView):
    """List synced opportunities. Non-admins only see opportunities assigned to them.

    Query params (all optional): location_id, pipeline_id, stage_id, status,
    assigned_to, source, search (opportunity name / contact name, email, phone),
    date_from / date_to (YYYY-MM-DD, on date_added), ordering (any of
    ORDERING_FIELDS, prefix "-" for descending), page, page_size.
    """

    serializer_class = OpportunitySerializer
    pagination_class = OpportunityPagination
    permission_classes = [IsAuthenticated]

    FILTER_PARAMS = {
        "location_id": "location_id",
        "pipeline_id": "pipeline_id",
        "stage_id": "pipeline_stage_id",
        "status": "status",
        "assigned_to": "assigned_to",
        "source": "source",
    }
    ORDERING_FIELDS = {"date_added", "date_updated", "monetary_value", "name", "last_stage_change_at"}

    def get_queryset(self):
        params = self.request.query_params
        assigned_name = GHLUser.objects.filter(user_id=OuterRef("assigned_to")).values("name")[:1]
        qs = visible_opportunities(self.request.user).select_related("contact", "stage").annotate(
            assigned_to_name=Subquery(assigned_name),
            loan_id=F("loan__id"),
        )

        for param, field in self.FILTER_PARAMS.items():
            if params.get(param):
                qs = qs.filter(**{field: params[param]})

        search = params.get("search", "").strip()
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(contact__contact_name__icontains=search)
                | Q(contact__email__icontains=search)
                | Q(contact__phone__icontains=search)
            )

        date_from = parse_date(params.get("date_from", ""))
        date_to = parse_date(params.get("date_to", ""))
        if date_from:
            qs = qs.filter(date_added__date__gte=date_from)
        if date_to:
            qs = qs.filter(date_added__date__lte=date_to)

        ordering = params.get("ordering", "-date_added")
        if ordering.lstrip("-") not in self.ORDERING_FIELDS:
            ordering = "-date_added"
        return qs.order_by(ordering, "-id")


class UserListView(ListAPIView):
    """List synced GHL users (deleted users excluded).

    Query params (all optional): search (partial, case-insensitive match on
    name, email, phone or user_id), location_id, page, page_size.
    """

    serializer_class = UserSerializer
    pagination_class = OpportunityPagination
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        params = self.request.query_params
        qs = GHLUser.objects.filter(deleted=False)

        location_id = params.get("location_id")
        if location_id:
            qs = qs.filter(location_ids__contains=[location_id])

        search = params.get("search", "").strip()
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(email__icontains=search)
                | Q(phone__icontains=search)
                | Q(user_id__icontains=search)
            )
        return qs.order_by("name", "id")


class OpportunityFiltersView(APIView):
    """Options for the opportunity list filters.

    Query param: location_id (optional, limits every list to that location).
    For non-admins, sources and assigned_to only cover their own opportunities.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        location_id = request.query_params.get("location_id")
        stages = GHLPipelineStage.objects.all()
        opportunities = visible_opportunities(request.user)
        users = GHLUser.objects.filter(deleted=False)
        if not request.user.is_app_admin:
            users = users.filter(account=request.user)
        if location_id:
            stages = stages.filter(location_id=location_id)
            opportunities = opportunities.filter(location_id=location_id)
            users = users.filter(location_ids__contains=[location_id])

        pipelines = {}
        for stage in stages.order_by("pipeline_name", "position"):
            pipeline = pipelines.setdefault(stage.pipeline_id, {
                "id": stage.pipeline_id,
                "name": stage.pipeline_name,
                "stages": [],
            })
            pipeline["stages"].append({"id": stage.stage_id, "name": stage.name, "position": stage.position})

        sources = (
            opportunities.exclude(source="").order_by("source")
            .values_list("source", flat=True).distinct()
        )
        return Response({
            "pipelines": list(pipelines.values()),
            "statuses": [{"value": value, "label": label} for value, label in GHLOpportunity.STATUS_CHOICES],
            "assigned_to": list(users.order_by("name").values("user_id", "name", "email")),
            "sources": list(sources),
            "ordering": sorted(OpportunityListView.ORDERING_FIELDS),
        })


@api_view(["GET"])
@permission_classes([IsAdminUser])
def oauth_status(request):
    """List connected GHL accounts (no tokens exposed)."""
    return Response({"connections": GHLConnectionSerializer(GHLToken.objects.all(), many=True).data})
