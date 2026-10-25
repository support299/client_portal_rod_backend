import requests
from django.db import transaction
from django.db.models import Prefetch, Q
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.http import content_disposition_header
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.generics import ListCreateAPIView, RetrieveUpdateDestroyAPIView
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import SAFE_METHODS, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.views import IsAppAdmin, IsStaffMember
from ghl.client import GHLAuthError
from ghl.models import GHLToken
from ghl.services import GHLAPIError, MediaService

from .models import DocumentRequest, DocumentType, LoanDocument
from .serializers import (
    DocumentRequestCreateSerializer, DocumentRequestSerializer, DocumentRequestUpdateSerializer,
    DocumentTypeSerializer, LoanDocumentSerializer, LoanDocumentUpdateSerializer,
    LoanDocumentUploadSerializer,
)
from .services import DocumentError, DocumentService
from .views import visible_loans

# Raised while pushing a file to GHL media.
GHL_ERRORS = (GHLAPIError, GHLAuthError, GHLToken.DoesNotExist, requests.RequestException)
UPLOAD_PARSERS = [MultiPartParser, FormParser, JSONParser]


def ghl_failed(exc):
    if isinstance(exc, GHLAPIError) and MediaService.is_invalid_file_type(exc):
        return Response(
            {"error": "This file type can't be uploaded. Try a PDF, an image (JPG/PNG) or a Word/Excel file."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return Response({"error": f"Could not store the file in GHL: {exc}"}, status=status.HTTP_502_BAD_GATEWAY)


def is_borrower(user):
    return user.role == user.Role.BORROWER


def stream_file(document, as_attachment=True):
    """Proxy a file from GHL media, so its public url never reaches the client."""
    try:
        upstream = requests.get(document.url, stream=True, timeout=60)
    except requests.RequestException as exc:
        return Response({"error": f"Could not fetch the file from GHL: {exc}"}, status=status.HTTP_502_BAD_GATEWAY)
    if not upstream.ok:
        upstream.close()
        return Response({"error": f"GHL returned {upstream.status_code} for the file."},
                        status=status.HTTP_502_BAD_GATEWAY)
    response = StreamingHttpResponse(
        upstream.iter_content(64 * 1024),
        content_type=document.content_type or upstream.headers.get("Content-Type", "application/octet-stream"),
    )
    response["Content-Disposition"] = content_disposition_header(as_attachment, document.name)
    if upstream.headers.get("Content-Length"):
        response["Content-Length"] = upstream.headers["Content-Length"]
    return response


def loan_documents(loan, user):
    """The loan's files; borrowers don't see internal ones."""
    qs = loan.documents.filter(is_deleted=False).select_related("uploaded_by", "reviewed_by", "document_type")
    return qs.filter(visible_to_borrower=True) if is_borrower(user) else qs


def document_requests(loan, user):
    files = LoanDocument.objects.filter(is_deleted=False).select_related("uploaded_by", "reviewed_by", "document_type")
    if is_borrower(user):
        files = files.filter(visible_to_borrower=True)
    return (
        loan.document_requests.filter(is_deleted=False)
        .select_related("document_type", "requested_by", "reviewed_by", "form_document")
        .prefetch_related(Prefetch("documents", queryset=files, to_attr="active_documents"))
    )


class LoanScopedMixin:
    """For views under /api/loans/<loan_pk>/: 404 for loans the user can't see (see visible_loans)."""

    @cached_property
    def loan(self):
        return get_object_or_404(visible_loans(self.request.user), pk=self.kwargs["loan_pk"])

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "loan": self.loan}


class StaffWritesMixin:
    """Anyone who can see the loan may read; only staff may change things."""

    def get_permissions(self):
        if self.request.method in SAFE_METHODS:
            return [IsAuthenticated()]
        return [IsStaffMember()]


# Document types


class DocumentTypeSaveMixin:
    """Saves a type and, if template_file was sent, its new blank form (uploaded first, outside the transaction)."""

    parser_classes = UPLOAD_PARSERS

    def get_permissions(self):
        if self.request.method in SAFE_METHODS:
            return [IsAuthenticated()]
        return [IsAppAdmin()]

    def get_queryset(self):
        qs = DocumentType.objects.all()
        if not self.request.user.is_app_admin:
            qs = qs.filter(is_active=True)
        return qs

    def save_type(self, serializer):
        template = serializer.validated_data.pop("template_file", None)
        fields = DocumentService.upload_template(template, self.request.user) if template else None
        with transaction.atomic():
            document_type = serializer.save()
            if fields:
                DocumentService.set_template(document_type, fields, self.request.user)

    perform_create = perform_update = save_type

    def handle_exception(self, exc):
        if isinstance(exc, GHL_ERRORS):
            return ghl_failed(exc)
        if isinstance(exc, DocumentError):
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return super().handle_exception(exc)


class DocumentTypeListView(DocumentTypeSaveMixin, ListCreateAPIView):
    """GET document types (admins see inactive ones too); POST (admins) creates one.

    Query params (all optional): category, kind (upload/fill_form), active (true/false, admins).
    POST as multipart to include template_file, the blank form of a fill_form type.
    """

    serializer_class = DocumentTypeSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if params.get("active") in ("true", "false"):
            qs = qs.filter(is_active=params["active"] == "true")
        if params.get("category"):
            qs = qs.filter(category=params["category"])
        if params.get("kind"):
            qs = qs.filter(kind=params["kind"])
        return qs


class DocumentTypeDetailView(DocumentTypeSaveMixin, RetrieveUpdateDestroyAPIView):
    """GET one type; PATCH/PUT (admins, multipart for template_file); DELETE (admins) deactivates it.

    Types are only deactivated, never deleted: requests and files keep pointing at them.
    """

    serializer_class = DocumentTypeSerializer

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=["is_active"])


class DocumentTypeTemplateView(APIView):
    """GET (staff) a fill_form type's blank form."""

    permission_classes = [IsStaffMember]

    def get(self, request, pk):
        document_type = get_object_or_404(DocumentType, pk=pk)
        template = (
            LoanDocument.objects.filter(ghl_media_id=document_type.template_ghl_media_id).first()
            if document_type.template_ghl_media_id else None
        )
        if template is None:
            return Response({"error": "This document type has no blank form."}, status=status.HTTP_404_NOT_FOUND)
        return stream_file(template, as_attachment=request.query_params.get("inline") != "true")


# Document requests


class DocumentRequestListView(LoanScopedMixin, StaffWritesMixin, ListCreateAPIView):
    """GET a loan's document requests, each with its files; POST (staff) requests a document.

    Query params (all optional): status, kind.
    POST body: document_type and/or name, description, kind, due_date; form_file (multipart)
    for a fill_form request. The request is also posted in the loan chat.
    """

    parser_classes = UPLOAD_PARSERS

    def get_serializer_class(self):
        if self.request.method == "POST":
            return DocumentRequestCreateSerializer
        return DocumentRequestSerializer

    def get_queryset(self):
        qs = document_requests(self.loan, self.request.user)
        params = self.request.query_params
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("kind"):
            qs = qs.filter(kind=params["kind"])
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            doc_request = DocumentService.create_request(self.loan, request.user, **serializer.validated_data)
        except GHL_ERRORS as exc:
            return ghl_failed(exc)
        doc_request = self.get_queryset().get(pk=doc_request.pk)
        return Response(
            DocumentRequestSerializer(doc_request, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )


class DocumentRequestDetailView(LoanScopedMixin, StaffWritesMixin, RetrieveUpdateDestroyAPIView):
    """GET one request; PATCH/PUT (staff) name, description, due_date, status, reviewer_note;
    DELETE (staff) removes it (soft delete, its files stay).
    """

    def get_queryset(self):
        return document_requests(self.loan, self.request.user)

    def get_serializer_class(self):
        if self.request.method in ("PUT", "PATCH"):
            return DocumentRequestUpdateSerializer
        return DocumentRequestSerializer

    def perform_update(self, serializer):
        new_status = serializer.validated_data.get("status")
        decided = (DocumentRequest.Status.UPLOADED, DocumentRequest.Status.REJECTED,
                   DocumentRequest.Status.NEEDS_REVISION)
        if new_status in decided and new_status != serializer.instance.status:
            serializer.save(reviewed_by=self.request.user, reviewed_at=timezone.now())
        else:
            serializer.save()

    def perform_destroy(self, instance):
        instance.is_deleted = True
        instance.save(update_fields=["is_deleted", "updated_at"])


# Loan documents (files)


class LoanDocumentListView(LoanScopedMixin, ListCreateAPIView):
    """GET a loan's files (borrowers see only those visible to them); POST (multipart) uploads one.

    Query params (all optional): request, draw, direction, review_status, document_type.
    POST body: file, and optionally request, draw, document_type, direction and visible_to_borrower
    (see LoanDocumentUploadSerializer). The file is stored in GHL media (502 if GHL fails).
    """

    permission_classes = [IsAuthenticated]
    parser_classes = UPLOAD_PARSERS

    def get_serializer_class(self):
        if self.request.method == "POST":
            return LoanDocumentUploadSerializer
        return LoanDocumentSerializer

    def get_queryset(self):
        qs = loan_documents(self.loan, self.request.user)
        params = self.request.query_params
        for field in ("request", "draw", "direction", "review_status", "document_type"):
            if params.get(field):
                qs = qs.filter(**{field: params[field]})
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            document = DocumentService.upload(
                self.loan, data["file"], request.user,
                doc_request=data.get("request"),
                draw=data.get("draw"),
                document_type=data.get("document_type"),
                direction=data["direction"],
                visible_to_borrower=data["visible_to_borrower"],
            )
        except GHL_ERRORS as exc:
            return ghl_failed(exc)
        return Response(
            LoanDocumentSerializer(document, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )


class LoanDocumentDetailView(LoanScopedMixin, RetrieveUpdateDestroyAPIView):
    """GET one file; PATCH/PUT (staff) to review or edit it (see LoanDocumentUpdateSerializer);
    DELETE removes it from the app (it stays in GHL media). Borrowers may only remove their own
    files that haven't been reviewed yet.
    """

    def get_permissions(self):
        if self.request.method in ("PUT", "PATCH"):
            return [IsStaffMember()]
        return [IsAuthenticated()]

    def get_queryset(self):
        return loan_documents(self.loan, self.request.user)

    def get_serializer_class(self):
        if self.request.method in ("PUT", "PATCH"):
            return LoanDocumentUpdateSerializer
        return LoanDocumentSerializer

    @transaction.atomic
    def perform_update(self, serializer):
        serializer.save()

    def perform_destroy(self, instance):
        user = self.request.user
        if is_borrower(user) and (
            instance.uploaded_by_id != user.pk
            or instance.review_status != LoanDocument.ReviewStatus.UNREVIEWED
        ):
            raise PermissionDenied("You can only remove your own files that haven't been reviewed yet.")
        DocumentService.delete(instance)


class LoanDocumentDownloadView(LoanScopedMixin, APIView):
    """GET a file's contents, streamed from GHL; ?inline=true to show it in the browser.

    Covers the loan's files and the blank forms its requests share.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, loan_pk, pk):
        forms = self.loan.document_requests.filter(is_deleted=False).values("form_document")
        documents = LoanDocument.objects.filter(is_deleted=False).filter(Q(loan=self.loan) | Q(pk__in=forms))
        if is_borrower(request.user):
            documents = documents.filter(visible_to_borrower=True)
        document = get_object_or_404(documents, pk=pk)
        return stream_file(document, as_attachment=request.query_params.get("inline") != "true")
