from rest_framework.exceptions import PermissionDenied
from rest_framework.generics import ListCreateAPIView, RetrieveUpdateDestroyAPIView

from accounts.views import IsStaffMember

from .document_views import LoanScopedMixin
from .serializers import LoanNoteSerializer


class LoanNoteMixin(LoanScopedMixin):
    """Notes are internal: staff only, so admins and the loan's officers (see visible_loans)."""

    permission_classes = [IsStaffMember]
    serializer_class = LoanNoteSerializer

    def get_queryset(self):
        return self.loan.notes.filter(is_deleted=False).select_related("author")


class LoanNoteListView(LoanNoteMixin, ListCreateAPIView):
    """GET a loan's internal notes, pinned first then newest; POST adds one.

    Query params (optional): pinned=true.
    POST body: body, is_pinned (optional).
    """

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("pinned") == "true":
            qs = qs.filter(is_pinned=True)
        return qs

    def perform_create(self, serializer):
        serializer.save(loan=self.loan, author=self.request.user)


class LoanNoteDetailView(LoanNoteMixin, RetrieveUpdateDestroyAPIView):
    """GET one note; PATCH/PUT its body or is_pinned; DELETE removes it (soft delete).

    Only the note's author or an admin may change or delete it.
    """

    def check_object_permissions(self, request, note):
        super().check_object_permissions(request, note)
        if request.method not in ("GET", "HEAD", "OPTIONS") and not (
            request.user.is_app_admin or note.author_id == request.user.pk
        ):
            raise PermissionDenied("Only the note's author or an admin can change it.")

    def perform_destroy(self, instance):
        instance.is_deleted = True
        instance.save(update_fields=["is_deleted", "updated_at"])
