from datetime import timedelta

from django.db.models import Prefetch
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.views import IsStaffMember

from .models import DocumentRequest, DrawRequest, LoanDocument
from .serializers import LoanDocumentSerializer
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


def loan_summary(loan):
    borrower = loan.borrower
    return {
        "id": loan.pk,
        "name": str(loan),
        "borrower": (borrower.get_full_name().strip() or borrower.email) if borrower else "",
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

    Query params: scope=mine (default; loans you're an officer on) or all (admins: every loan).
    Returns {"open": [...], "done": [...]}, newest first; done is reviews from the last DONE_DAYS days.
    """

    permission_classes = [IsStaffMember]

    def get(self, request):
        user = request.user
        loans = visible_loans(user)
        if not (user.is_app_admin and request.query_params.get("scope") == "all"):
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

        since = timezone.now() - timedelta(days=DONE_DAYS)
        done = []
        reviewed = LoanDocument.objects.filter(
            loan_id__in=loan_ids, is_deleted=False, direction=LoanDocument.Direction.BORROWER_UPLOAD,
            draw__isnull=True, reviewed_at__gte=since,
        ).exclude(review_status=LoanDocument.ReviewStatus.UNREVIEWED).select_related("request", "reviewed_by")
        for document in reviewed.order_by("-reviewed_at")[:DONE_LIMIT]:
            reviewer = document.reviewed_by
            accepted = document.review_status == LoanDocument.ReviewStatus.ACCEPTED
            done.append({
                "id": f"file-{document.pk}",
                "type": "document_review" if document.request_id else "file_review",
                "title": f'{"Accepted" if accepted else "Rejected"}: '
                         f'{document.request.name if document.request_id else document.name}',
                "outcome": document.review_status,
                "loan": loan_summary(loans[document.loan_id]),
                "at": document.reviewed_at,
                "by": (reviewer.get_full_name().strip() or reviewer.email) if reviewer else "",
            })

        open_tasks.sort(key=lambda t: t["at"], reverse=True)
        for task in open_tasks + done:
            task["at"] = task["at"].isoformat()
        return Response({"open": open_tasks, "done": done})
