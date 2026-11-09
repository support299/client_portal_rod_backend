from datetime import timedelta

from django.db.models import Prefetch, Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from rest_framework.generics import CreateAPIView, RetrieveUpdateDestroyAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.views import IsStaffMember

from .models import DocumentRequest, DrawRequest, LoanDocument, StaffTask
from .serializers import LoanDocumentSerializer, StaffTaskSerializer
from .views import visible_loans

# How far back the "Done" list reaches.
DONE_DAYS = 14
DONE_LIMIT = 30

# Draw statuses waiting on staff, with what the task asks for.
DRAW_ACTIONS = {
    DrawRequest.Status.SUBMITTED: "Review",
    DrawRequest.Status.INSPECTED: "Approve",
    DrawRequest.Status.APPROVED: "Fund",
}


def user_name(user):
    return (user.get_full_name().strip() or user.email) if user else ""


def loan_summary(loan):
    return {
        "id": loan.pk,
        "name": str(loan),
        "borrower": user_name(loan.borrower),
    }


def unreviewed_borrower_files():
    return LoanDocument.objects.filter(
        is_deleted=False,
        direction=LoanDocument.Direction.BORROWER_UPLOAD,
        review_status=LoanDocument.ReviewStatus.UNREVIEWED,
    ).select_related("uploaded_by", "reviewed_by", "document_type")


class TaskListView(APIView):
    """GET the review work waiting on staff, worked out from the loans' current state.

    A task opens when a borrower does something staff must act on, and closes by itself once
    staff act on it:
    - document_review: a requested document came in (request pending review); its unreviewed files ride along
    - file_review: a borrower uploaded a file without a request
    - draw_review: a draw waits on staff (submitted, inspected or approved)

    Manual tasks (StaffTask, type "manual") ride along: open ones in open, completed ones in done.
    They open and close by hand instead (see StaffTaskCreateView / StaffTaskDetailView).

    Query params: scope=mine (default; loans you're an officer on, manual tasks assigned to you)
    or all (admins: every loan and everyone's tasks).
    Returns {"open": [...], "done": [...]}, newest first; done is from the last DONE_DAYS days.
    """

    permission_classes = [IsStaffMember]

    def get(self, request):
        user = request.user
        all_scope = user.is_app_admin and request.query_params.get("scope") == "all"
        loans = visible_loans(user)
        if not all_scope:
            loans = loans.filter(officer_assignments__officer=user, officer_assignments__is_deleted=False)
        loans = {loan.pk: loan for loan in loans.distinct()}
        loan_ids = list(loans)
        context = {"request": request}

        open_tasks = []

        requests_ = DocumentRequest.objects.filter(
            loan_id__in=loan_ids, is_deleted=False, status=DocumentRequest.Status.PENDING_REVIEW,
        ).prefetch_related(Prefetch("documents", queryset=unreviewed_borrower_files(), to_attr="unreviewed"))
        for doc_request in requests_:
            files = doc_request.unreviewed
            open_tasks.append({
                "id": f"request-{doc_request.pk}",
                "type": "document_review",
                "title": f'Review "{doc_request.name}"',
                "loan": loan_summary(loans[doc_request.loan_id]),
                "at": max((f.uploaded_at for f in files), default=doc_request.updated_at),
                "document_request": doc_request.pk,
                "files": LoanDocumentSerializer(files, many=True, context=context).data,
            })

        # Chat files already show up as unread messages; GHL/survey imports aren't new borrower activity.
        loose_files = unreviewed_borrower_files().filter(
            loan_id__in=loan_ids, request__isnull=True, draw__isnull=True, origin=LoanDocument.Origin.APP,
        )
        for document in loose_files:
            open_tasks.append({
                "id": f"file-{document.pk}",
                "type": "file_review",
                "title": f"Review new file: {document.name}",
                "loan": loan_summary(loans[document.loan_id]),
                "at": document.uploaded_at,
                "files": [LoanDocumentSerializer(document, context=context).data],
            })

        draws = DrawRequest.objects.filter(loan_id__in=loan_ids, is_deleted=False, status__in=DRAW_ACTIONS)
        for draw in draws:
            open_tasks.append({
                "id": f"draw-{draw.pk}",
                "type": "draw_review",
                "title": f"{DRAW_ACTIONS[draw.status]} Draw #{draw.number}",
                "loan": loan_summary(loans[draw.loan_id]),
                "at": draw.updated_at,
                "draw": draw.pk,
                "draw_status": draw.status,
                "amount": draw.amount_approved if draw.amount_approved is not None else draw.amount_requested,
            })

        # Manual tasks follow their assignee, not the loan's officers: one you're given shows up
        # even on a loan you're not on.
        manual = StaffTask.objects.filter(is_deleted=False).select_related(
            "loan__opportunity", "loan__borrower", "assigned_to", "created_by", "completed_by",
        )
        if not all_scope:
            manual = manual.filter(assigned_to=user)
        for task in manual.filter(completed_at__isnull=True):
            open_tasks.append({
                "id": f"manual-{task.pk}",
                "type": "manual",
                "title": task.title,
                "note": task.note,
                "loan": loan_summary(task.loan) if task.loan_id else None,
                "at": task.created_at,
                "task": task.pk,
                "due_date": task.due_date,
                "assigned_to": {"id": task.assigned_to_id, "full_name": user_name(task.assigned_to)},
                "created_by": user_name(task.created_by),
            })

        since = timezone.now() - timedelta(days=DONE_DAYS)
        done = []
        reviewed = LoanDocument.objects.filter(
            loan_id__in=loan_ids, is_deleted=False, direction=LoanDocument.Direction.BORROWER_UPLOAD,
            draw__isnull=True, reviewed_at__gte=since,
        ).exclude(review_status=LoanDocument.ReviewStatus.UNREVIEWED).select_related("request", "reviewed_by")
        for document in reviewed.order_by("-reviewed_at")[:DONE_LIMIT]:
            accepted = document.review_status == LoanDocument.ReviewStatus.ACCEPTED
            done.append({
                "id": f"file-{document.pk}",
                "type": "document_review" if document.request_id else "file_review",
                "title": f'{"Accepted" if accepted else "Rejected"}: '
                         f'{document.request.name if document.request_id else document.name}',
                "outcome": document.review_status,
                "loan": loan_summary(loans[document.loan_id]),
                "at": document.reviewed_at,
                "by": user_name(document.reviewed_by),
            })

        for task in manual.filter(completed_at__gte=since):
            done.append({
                "id": f"manual-{task.pk}",
                "type": "manual",
                "title": f"Completed: {task.title}",
                "outcome": "completed",
                "loan": loan_summary(task.loan) if task.loan_id else None,
                "at": task.completed_at,
                "by": user_name(task.completed_by),
            })

        open_tasks.sort(key=lambda t: t["at"], reverse=True)
        done.sort(key=lambda t: t["at"], reverse=True)
        done = done[:DONE_LIMIT]
        for task in open_tasks + done:
            task["at"] = task["at"].isoformat()
        return Response({"open": open_tasks, "done": done})


class StaffTaskCreateView(CreateAPIView):
    """POST a manual task for yourself or a teammate.

    Body: title, assigned_to_id, and optionally note, loan, due_date.
    A loan-linked task must be on a loan you can see.
    """

    permission_classes = [IsStaffMember]
    serializer_class = StaffTaskSerializer

    def perform_create(self, serializer):
        loan = serializer.validated_data.get("loan")
        if loan and not visible_loans(self.request.user).filter(pk=loan.pk).exists():
            raise PermissionDenied("You can't add a task to a loan you don't have access to.")
        serializer.save(created_by=self.request.user)


class StaffTaskDetailView(RetrieveUpdateDestroyAPIView):
    """GET one manual task; PATCH edits it (completed: true/false marks done or reopens);
    DELETE removes it (soft delete).

    Visible to its assignee, its creator and admins; only the creator or an admin may delete.
    """

    permission_classes = [IsStaffMember]
    serializer_class = StaffTaskSerializer

    def get_queryset(self):
        user = self.request.user
        qs = StaffTask.objects.filter(is_deleted=False).select_related(
            "loan__opportunity", "assigned_to", "created_by", "completed_by",
        )
        if user.is_app_admin:
            return qs
        return qs.filter(Q(assigned_to=user) | Q(created_by=user))

    def check_object_permissions(self, request, task):
        super().check_object_permissions(request, task)
        if request.method == "DELETE" and not (
            request.user.is_app_admin or task.created_by_id == request.user.pk
        ):
            raise PermissionDenied("Only the task's creator or an admin can delete it.")

    def perform_destroy(self, instance):
        instance.is_deleted = True
        instance.save(update_fields=["is_deleted", "updated_at"])
