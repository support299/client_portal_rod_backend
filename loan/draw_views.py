from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.generics import ListCreateAPIView, RetrieveUpdateDestroyAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.views import IsAppAdmin

from .document_views import LoanScopedMixin, is_borrower
from .draw_services import DrawError, DrawService
from .models import DrawRequestStatusChange, LoanDocument
from .serializers import (
    DrawRequestCreateSerializer, DrawRequestSerializer, DrawRequestUpdateSerializer, DrawStatusSerializer,
)


def loan_draws(loan, user):
    """The loan's draws, each with its files and history; borrowers don't see internal files."""
    files = LoanDocument.objects.filter(is_deleted=False).select_related("uploaded_by", "reviewed_by", "document_type")
    if is_borrower(user):
        files = files.filter(visible_to_borrower=True)
    changes = DrawRequestStatusChange.objects.select_related("changed_by")
    return (
        loan.draws.filter(is_deleted=False)
        .select_related("submitted_by")
        .prefetch_related(
            Prefetch("files", queryset=files, to_attr="active_files"),
            Prefetch("status_changes", queryset=changes),
        )
    )


class DrawErrorMixin:
    def handle_exception(self, exc):
        if isinstance(exc, DrawError):
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return super().handle_exception(exc)


class DrawRequestListView(DrawErrorMixin, LoanScopedMixin, ListCreateAPIView):
    """GET a loan's draws, each with its files and status history; POST submits a new draw.

    Borrowers submit draws on their own loans; staff may submit for the borrower.
    Query params (optional): status.
    POST body: amount_requested, borrower_comment. 400 if it's more than the loan has left, or
    the loan has no holdback. Upload files afterwards to the loan's documents with draw=<id>.
    """

    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.request.method == "POST":
            return DrawRequestCreateSerializer
        return DrawRequestSerializer

    def get_queryset(self):
        qs = loan_draws(self.loan, self.request.user)
        if self.request.query_params.get("status"):
            qs = qs.filter(status=self.request.query_params["status"])
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        draw = DrawService.create(self.loan, request.user, **serializer.validated_data)
        draw = self.get_queryset().get(pk=draw.pk)
        return Response(
            DrawRequestSerializer(draw, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )


class DrawRequestDetailView(DrawErrorMixin, LoanScopedMixin, RetrieveUpdateDestroyAPIView):
    """GET one draw; PATCH/PUT its details (see DrawRequestUpdateSerializer: borrowers only the
    amount and comment, while submitted or needs info); DELETE (admins) removes it (soft delete).

    To cancel, approve, fund etc. use the status endpoint instead.
    """

    def get_permissions(self):
        if self.request.method == "DELETE":
            return [IsAppAdmin()]
        return [IsAuthenticated()]

    def get_queryset(self):
        return loan_draws(self.loan, self.request.user)

    def get_serializer_class(self):
        if self.request.method in ("PUT", "PATCH"):
            return DrawRequestUpdateSerializer
        return DrawRequestSerializer

    def perform_destroy(self, instance):
        DrawService.delete(instance)


class DrawRequestStatusView(DrawErrorMixin, LoanScopedMixin, APIView):
    """POST moves a draw to a new status; the change is logged and posted in the loan chat.

    Body: status, note (optional reason), amount_approved (staff; required to approve unless
    already set), funding_reference (staff, when funding). The draw's allowed_statuses lists
    what the user may send: borrowers may only resubmit after needs_info or cancel early.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, loan_pk, pk):
        draw = get_object_or_404(self.loan.draws.filter(is_deleted=False), pk=pk)
        serializer = DrawStatusSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        DrawService.change_status(
            draw, data["status"], request.user, note=data["note"],
            amount_approved=data.get("amount_approved"), funding_reference=data.get("funding_reference"),
        )
        draw = loan_draws(self.loan, request.user).get(pk=pk)
        return Response(DrawRequestSerializer(draw, context={"request": request, "loan": self.loan}).data)


class DrawSummaryView(LoanScopedMixin, APIView):
    """GET the loan's holdback with what's redeemed, in progress and still available."""

    permission_classes = [IsAuthenticated]

    def get(self, request, loan_pk):
        return Response(self.loan.draw_summary())
