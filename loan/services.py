import logging
import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Subquery
from django.utils import timezone

from chats.models import Message
from chats.services import ChatService
from ghl.models import GHLOpportunity, GHLUser
from ghl.services import GHLAPIError, GHLService, MediaService

from .models import DocumentRequest, DocumentType, Loan, LoanDocument, LoanOfficer

logger = logging.getLogger(__name__)


class LoanConversionError(Exception):
    pass


class LoanService:
    @staticmethod
    def get_or_create_borrower(contact):
        """Return (user, created) for the contact's borrower login, matched by email."""
        User = get_user_model()
        email = (contact.email or "").strip()
        if not email:
            raise LoanConversionError("The opportunity's contact has no email, so no borrower login can be created.")

        user = User.objects.filter(email__iexact=email).first()
        if user:
            if user.role != User.Role.BORROWER:
                raise LoanConversionError(f"{email} already belongs to a {user.get_role_display()} login.")
            return user, False

        # No password: the borrower can't log in until one is set (e.g. via an invite/reset flow).
        user = User.objects.create_user(
            email=email,
            first_name=contact.first_name,
            last_name=contact.last_name,
            phone=(contact.phone or "")[:20],
            role=User.Role.BORROWER,
        )
        return user, True

    @staticmethod
    @transaction.atomic
    def set_officers(loan, officers):
        """Replace the loan's officers with `officers` (users), keeping existing assignments' order.

        The GHL assignee always stays on (see add_ghl_assignee); to remove them, reassign in GHL.
        """
        wanted = list(dict.fromkeys(u.pk for u in officers))
        if loan.ghl_assignee_id and loan.ghl_assignee_id not in wanted:
            wanted.append(loan.ghl_assignee_id)
        active = loan.officer_assignments.filter(is_deleted=False)
        active.exclude(officer_id__in=wanted).update(is_deleted=True)
        existing = set(active.values_list("officer_id", flat=True))
        LoanOfficer.objects.bulk_create(
            [LoanOfficer(loan=loan, officer_id=pk) for pk in wanted if pk not in existing]
        )

    @staticmethod
    def add_ghl_assignee(loan):
        """Add the opportunity's GHL assigned user to the loan's officers, if they have a login.

        Officers already on the loan are kept, including a previous GHL assignee.
        """
        assignee_id = loan.ghl_assignee_id
        if assignee_id:
            LoanOfficer.objects.bulk_create([LoanOfficer(loan=loan, officer_id=assignee_id)], ignore_conflicts=True)

    @staticmethod
    def add_ghl_assignees():
        """add_ghl_assignee for every loan (after a sync, or once a GHL user gets linked to a login).

        Returns the number of officers added.
        """
        assignee = GHLUser.objects.filter(
            user_id=OuterRef("opportunity__assigned_to"), account__isnull=False,
        ).values("account")[:1]
        on_loan = LoanOfficer.objects.filter(loan=OuterRef("pk"), officer=OuterRef("assignee_id"), is_deleted=False)
        missing = (
            Loan.objects.filter(is_deleted=False)
            .annotate(assignee_id=Subquery(assignee))
            .filter(assignee_id__isnull=False)
            .exclude(Exists(on_loan))
            .values_list("pk", "assignee_id")
        )
        added = LoanOfficer.objects.bulk_create(
            [LoanOfficer(loan_id=pk, officer_id=officer) for pk, officer in missing], ignore_conflicts=True,
        )
        return len(added)

    @staticmethod
    @transaction.atomic
    def create_from_opportunity(opportunity, created_by=None):
        """Create a Loan (and the borrower login if needed) from a GHL opportunity.

        Returns (loan, borrower_created).
        """
        if Loan.objects.filter(opportunity=opportunity).exists():
            raise LoanConversionError("A loan has already been created for this opportunity.")
        if opportunity.pipeline_id != settings.LOAN_PIPELINE_ID:
            raise LoanConversionError("Only opportunities in the loan pipeline can be converted to loans.")
        if opportunity.contact is None:
            raise LoanConversionError("The opportunity has no contact.")

        borrower, borrower_created = LoanService.get_or_create_borrower(opportunity.contact)
        loan = Loan.objects.create(
            opportunity=opportunity,
            borrower=borrower,
            created_by=created_by,
        )
        LoanService.add_ghl_assignee(loan)
        DocumentService.request_defaults(loan, requested_by=created_by)
        return loan, borrower_created

    @staticmethod
    def ensure_loan(opportunity):
        """Create the loan for a loan-pipeline opportunity, if it doesn't have one yet.

        Every opportunity in settings.LOAN_PIPELINE_ID is a loan, so this runs automatically
        (webhook, backfill). An existing loan gets the opportunity's current GHL assignee added
        to its officers instead. Returns the new loan, or None if nothing was created.
        Raises LoanConversionError when the contact can't get a borrower login.
        """
        if opportunity.pipeline_id != settings.LOAN_PIPELINE_ID:
            return None
        existing = Loan.objects.filter(opportunity=opportunity).first()
        if existing:
            LoanService.add_ghl_assignee(existing)
            return None
        try:
            loan, _ = LoanService.create_from_opportunity(opportunity)
        except IntegrityError:
            # Created concurrently (e.g. two webhooks for the same opportunity).
            return None
        return loan

    @staticmethod
    def ensure_pipeline_loans():
        """Create loans for every loan-pipeline opportunity that doesn't have one.

        Returns (created loans, [(opportunity, reason), ...] for the ones skipped).
        """
        opportunities = (
            GHLOpportunity.objects.filter(pipeline_id=settings.LOAN_PIPELINE_ID, loan__isnull=True)
            .select_related("contact")
            .order_by("date_added")
        )
        created, skipped = [], []
        for opportunity in opportunities:
            try:
                loan = LoanService.ensure_loan(opportunity)
            except LoanConversionError as exc:
                logger.info("No loan for opportunity %s: %s", opportunity.opportunity_id, exc)
                skipped.append((opportunity, str(exc)))
                continue
            if loan:
                created.append(loan)
        return created, skipped


class DocumentError(Exception):
    pass


class DocumentService:
    """Loan documents: the files live in the location's GHL media (see MediaService), metadata here."""

    MAX_UPLOAD_SIZE = 25 * 1024 * 1024
    # Media name prefix for blank forms, which belong to a document type rather than a loan.
    TEMPLATE_PREFIX = "template"

    @staticmethod
    def push_to_ghl(location_id, file, prefix):
        """Upload an UploadedFile to the location's GHL media; returns the LoanDocument fields for it.

        Call it outside a transaction: the upload can take a while, and if saving fails
        afterwards the file just stays in GHL media.
        """
        token = GHLService.token_for_location(location_id)
        data = MediaService.upload(token, file, file.name, file.content_type, prefix)
        file_id = data.get("fileId") or data.get("_id")
        if not file_id or not data.get("url"):
            raise GHLAPIError(200, {"error": "GHL stored the file but returned no id or url", "response": data})
        return {
            "ghl_media_id": file_id,
            "ghl_folder_id": token.media_folder_id or "",
            "location_id": location_id,
            "url": data["url"],
            "name": os.path.basename(file.name)[:255],
            "content_type": (file.content_type or "")[:100],
            "size": file.size,
            "raw": data,
            "uploaded_at": timezone.now(),
        }

    @staticmethod
    def upload(loan, file, user, doc_request=None, document_type=None,
               direction=LoanDocument.Direction.BORROWER_UPLOAD, visible_to_borrower=True):
        """Store a file for a loan. A borrower file sent against a request puts the request in review."""
        fields = DocumentService.push_to_ghl(loan.opportunity.location_id, file, loan.opportunity_id)
        with transaction.atomic():
            document = LoanDocument.objects.create(
                loan=loan,
                request=doc_request,
                document_type=document_type or (doc_request.document_type if doc_request else None),
                direction=direction,
                visible_to_borrower=visible_to_borrower,
                origin=LoanDocument.Origin.APP,
                uploaded_by=user,
                **fields,
            )
            if doc_request is not None and direction == LoanDocument.Direction.BORROWER_UPLOAD:
                doc_request.status = DocumentRequest.Status.PENDING_REVIEW
                doc_request.save(update_fields=["status", "updated_at"])
        return document

    @staticmethod
    def upload_template(file):
        """Push a document type's blank form to GHL media; pass the result to set_template."""
        location_id = (
            GHLOpportunity.objects.filter(pipeline_id=settings.LOAN_PIPELINE_ID)
            .values_list("location_id", flat=True).first()
        )
        if not location_id:
            raise DocumentError("No GHL location has the loan pipeline yet; sync GHL first.")
        return DocumentService.push_to_ghl(location_id, file, DocumentService.TEMPLATE_PREFIX)

    @staticmethod
    @transaction.atomic
    def set_template(document_type, fields, user):
        """Make an uploaded blank form (from upload_template) the type's template.

        The form is a LoanDocument with no loan, shared by every request of the type; an old
        template keeps its row, since earlier requests still point at it.
        """
        LoanDocument.objects.create(
            document_type=document_type,
            direction=LoanDocument.Direction.LENDER_SHARE,
            origin=LoanDocument.Origin.APP,
            uploaded_by=user,
            **fields,
        )
        document_type.template_ghl_media_id = fields["ghl_media_id"]
        document_type.template_name = fields["name"]
        document_type.save(update_fields=["template_ghl_media_id", "template_name"])

    @staticmethod
    def request_message(doc_request):
        lines = [f"Document requested: {doc_request.name}"]
        if doc_request.description:
            lines.append(doc_request.description)
        if doc_request.kind == DocumentType.Kind.FILL_FORM:
            lines.append("Fill in the shared form and upload it.")
        if doc_request.due_date:
            lines.append(f"Due {doc_request.due_date:%b %d, %Y}")
        return "\n".join(lines)

    @staticmethod
    def create_request(loan, requested_by, name, document_type=None, kind=None, description="",
                       due_date=None, source=DocumentRequest.Source.MANUAL, form_file=None):
        """Ask the borrower for a document and post the request in the loan chat.

        A fill_form request shares form_file if given, else the type's template.
        """
        kind = kind or (document_type.kind if document_type else DocumentType.Kind.UPLOAD)
        form = None
        if form_file is not None:
            form = DocumentService.upload(loan, form_file, requested_by, document_type=document_type,
                                          direction=LoanDocument.Direction.LENDER_SHARE)
        elif kind == DocumentType.Kind.FILL_FORM and document_type and document_type.template_ghl_media_id:
            form = LoanDocument.objects.filter(ghl_media_id=document_type.template_ghl_media_id).first()
        with transaction.atomic():
            doc_request = DocumentRequest.objects.create(
                loan=loan,
                document_type=document_type,
                name=name,
                description=description,
                kind=kind,
                form_document=form,
                source=source,
                due_date=due_date,
                requested_by=requested_by,
            )
            doc_request.message = ChatService.send_message(
                ChatService.ensure_loan_chat(loan), requested_by,
                DocumentService.request_message(doc_request), kind=Message.Kind.DOCUMENT_REQUEST,
            )
            doc_request.save(update_fields=["message", "updated_at"])
        return doc_request

    @staticmethod
    def request_defaults(loan, requested_by=None):
        """Request every active default document type (the checklist) for a new loan."""
        for document_type in DocumentType.objects.filter(is_default=True, is_active=True):
            DocumentService.create_request(
                loan, requested_by, document_type.name, document_type=document_type,
                description=document_type.description, source=DocumentRequest.Source.CHECKLIST,
            )

    @staticmethod
    @transaction.atomic
    def review(document, review_status, user, note=None):
        """Accept or reject a file; its request follows (accepted: uploaded, rejected: needs revision)."""
        reviewed = review_status != LoanDocument.ReviewStatus.UNREVIEWED
        document.review_status = review_status
        document.reviewed_by = user if reviewed else None
        document.reviewed_at = timezone.now() if reviewed else None
        document.save(update_fields=["review_status", "reviewed_by", "reviewed_at"])
        doc_request = document.request
        if doc_request is None or not reviewed:
            return
        doc_request.status = (
            DocumentRequest.Status.UPLOADED if review_status == LoanDocument.ReviewStatus.ACCEPTED
            else DocumentRequest.Status.NEEDS_REVISION
        )
        doc_request.reviewed_by = user
        doc_request.reviewed_at = document.reviewed_at
        if note is not None:
            doc_request.reviewer_note = note
        doc_request.save(update_fields=["status", "reviewed_by", "reviewed_at", "reviewer_note", "updated_at"])

    @staticmethod
    @transaction.atomic
    def delete(document):
        """Hide a file (it stays in GHL media); a request left with no files goes back to requested."""
        document.is_deleted = True
        document.save(update_fields=["is_deleted"])
        doc_request = document.request
        if (doc_request and doc_request.status == DocumentRequest.Status.PENDING_REVIEW
                and not doc_request.documents.filter(is_deleted=False).exists()):
            doc_request.status = DocumentRequest.Status.REQUESTED
            doc_request.save(update_fields=["status", "updated_at"])
