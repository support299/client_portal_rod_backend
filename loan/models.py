from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Sum
from django.db.models.functions import Coalesce
from django.utils.functional import cached_property

from ghl.models import GHLUser


class Loan(models.Model):
    """App-owned data layered on top of a GHL opportunity.

    Shared details (name, amount, stage, close date, ...) live on the opportunity and are
    read through it, so GHL stays the single source of truth. Only fields GHL doesn't have
    (borrower, loan officer) are stored here. The loan's status is its opportunity's stage in
    the loan pipeline (settings.LOAN_PIPELINE_ID).
    """

    # One loan per opportunity. PROTECT: a loan has nothing to show without its opportunity.
    opportunity = models.OneToOneField(
        "ghl.GHLOpportunity",
        to_field="opportunity_id",
        on_delete=models.PROTECT,
        related_name="loan",
    )
    borrower = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="loans",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    # Total construction money released through draws; entered in the app, not synced to GHL.
    # Null for loans with no draws. Draws never change it: redeemed/remaining come from draw_summary.
    holdback_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    is_deleted = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.opportunity.name or f"Loan {self.pk}"

    @cached_property
    def ghl_assignee_id(self):
        """The login of the opportunity's GHL assigned user, or None if they have none.

        visible_loans annotates this to avoid a query per loan.
        """
        if not self.opportunity.assigned_to:
            return None
        return (
            GHLUser.objects.filter(user_id=self.opportunity.assigned_to, account__isnull=False)
            .values_list("account", flat=True).first()
        )

    @property
    def loan_officers(self):
        """The assigned officers: the GHL assignee first, then assignment order.

        Each officer gets is_ghl_assignee. Prefetch officer_assignments__officer.
        """
        officers = [a.officer for a in self.officer_assignments.all() if not a.is_deleted]
        for officer in officers:
            officer.is_ghl_assignee = officer.pk == self.ghl_assignee_id
        return sorted(officers, key=lambda o: not o.is_ghl_assignee)

    @property
    def loan_officer(self):
        """The first officer, i.e. the GHL assignee when they're on the loan (single-officer view of loan_officers)."""
        officers = self.loan_officers
        return officers[0] if officers else None

    def draw_summary(self, exclude=None):
        """Holdback, redeemed, in-progress and available amounts, calculated from the draws.

        Redeemed is approved amounts on funded draws; in progress is open draws, at their approved
        amount once set, else the requested amount. Available is None when there's no holdback.
        exclude leaves out one draw, to check whether a change to it still fits.
        """
        draws = self.draws.filter(is_deleted=False)
        if exclude is not None:
            draws = draws.exclude(pk=exclude.pk)
        redeemed = draws.filter(status=DrawRequest.Status.FUNDED).aggregate(
            total=Sum("amount_approved"),
        )["total"] or Decimal("0")
        in_progress = draws.filter(status__in=DrawRequest.OPEN_STATUSES).aggregate(
            total=Sum(Coalesce("amount_approved", "amount_requested")),
        )["total"] or Decimal("0")
        available = None
        if self.holdback_amount is not None:
            available = self.holdback_amount - redeemed - in_progress
        return {
            "holdback": self.holdback_amount,
            "redeemed": redeemed,
            "in_progress": in_progress,
            "available": available,
        }


class LoanOfficer(models.Model):
    """Assigns a staff user as one of a loan's loan officers (a loan can have several)."""

    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="officer_assignments")
    officer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="loan_assignments",
    )
    is_deleted = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            # Only one active assignment per officer; removed ones are kept (is_deleted) and may be re-added.
            models.UniqueConstraint(
                fields=["loan", "officer"], condition=models.Q(is_deleted=False), name="unique_loan_officer",
            ),
        ]

    def __str__(self):
        return f"{self.officer} on {self.loan}"


class LoanNote(models.Model):
    """An internal note on a loan, seen only by admins and the loan's officers, never the borrower."""

    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="notes")
    body = models.TextField()
    # Pinned notes are listed first.
    is_pinned = models.BooleanField(default=False)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    is_deleted = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_pinned", "-created_at", "-id"]

    def __str__(self):
        return f"Note {self.pk} ({self.loan})"


class DocumentType(models.Model):
    """A kind of document the office collects, e.g. "Bank Statements"; admins manage the list."""

    class Kind(models.TextChoices):
        UPLOAD = "upload", "Borrower uploads their own file"
        FILL_FORM = "fill_form", "Borrower fills a form we share"

    name = models.CharField(max_length=255, unique=True)
    category = models.CharField(max_length=50, blank=True, db_index=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.UPLOAD)
    # The blank form in GHL media, for fill_form types; shared when the type is requested.
    template_ghl_media_id = models.CharField(max_length=64, blank=True)
    template_name = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    # Requested automatically when a loan is created.
    is_default = models.BooleanField(default=False)
    position = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["category", "position", "name"]

    def __str__(self):
        return self.name


class DocumentRequest(models.Model):
    """A document staff need from the borrower for one loan; its files are LoanDocuments."""

    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        PENDING_REVIEW = "pending_review", "Pending Review"
        NEEDS_REVISION = "needs_revision", "Needs Revision"
        UPLOADED = "uploaded", "Uploaded"
        REJECTED = "rejected", "Rejected"

    class Source(models.TextChoices):
        CHECKLIST = "checklist", "Checklist"
        MANUAL = "manual", "Manual"
        CHAT = "chat", "Chat"

    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="document_requests")
    document_type = models.ForeignKey(
        DocumentType, on_delete=models.PROTECT, null=True, blank=True, related_name="requests",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    kind = models.CharField(
        max_length=20, choices=DocumentType.Kind.choices, default=DocumentType.Kind.UPLOAD,
    )
    # For fill_form: the blank form shared with the borrower for this loan.
    form_document = models.ForeignKey(
        "LoanDocument", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.MANUAL)
    # The loan-chat message for this request; one is posted automatically when not made in chat.
    message = models.ForeignKey(
        "chats.Message", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="document_requests",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.REQUESTED, db_index=True,
    )
    reviewer_note = models.TextField(blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    due_date = models.DateField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["requested_at", "id"]

    def __str__(self):
        return f"{self.name} ({self.loan})"


class DrawRequest(models.Model):
    """A borrower's request for money from the loan's holdback; its files are LoanDocuments.

    Change status only through the service so every change is logged in DrawRequestStatusChange.
    """

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        NEEDS_INFO = "needs_info", "Needs Info"
        INSPECTION_ORDERED = "inspection_ordered", "Inspection Ordered"
        INSPECTED = "inspected", "Inspected"
        APPROVED = "approved", "Approved"
        FUNDED = "funded", "Funded"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    # Draws still holding money back from the holdback.
    OPEN_STATUSES = [
        Status.SUBMITTED, Status.NEEDS_INFO, Status.INSPECTION_ORDERED, Status.INSPECTED,
        Status.APPROVED,
    ]

    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="draws")
    # Draw #1, #2, ... per loan.
    number = models.PositiveIntegerField()
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.SUBMITTED, db_index=True,
    )
    amount_requested = models.DecimalField(max_digits=14, decimal_places=2)
    # Can be less than requested, based on the inspection; counts as redeemed once funded.
    amount_approved = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    borrower_comment = models.TextField(blank=True)
    # Shown to the borrower on needs_info / rejected.
    staff_note = models.TextField(blank=True)
    inspector = models.CharField(max_length=255, blank=True)
    inspection_date = models.DateField(null=True, blank=True)
    # Wire / ACH reference.
    funding_reference = models.CharField(max_length=100, blank=True)
    # The borrower, or staff submitting for them.
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    # The draw's card in the loan chat; moves to the newest status message.
    message = models.ForeignKey(
        "chats.Message", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="draw_requests",
    )
    is_deleted = models.BooleanField(default=False, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["loan", "number"]
        constraints = [
            models.UniqueConstraint(fields=["loan", "number"], name="unique_draw_number"),
        ]

    def __str__(self):
        return f"Draw #{self.number} ({self.loan})"


class DrawRequestStatusChange(models.Model):
    """One status change on a draw, e.g. approved -> inspected when an approval is undone.

    Append-only: rows are never edited or deleted.
    """

    draw = models.ForeignKey(DrawRequest, on_delete=models.CASCADE, related_name="status_changes")
    # Blank on the first row, when the draw is submitted.
    from_status = models.CharField(max_length=20, choices=DrawRequest.Status.choices, blank=True)
    to_status = models.CharField(max_length=20, choices=DrawRequest.Status.choices)
    # The draw's approved amount at the time of the change.
    amount_approved = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    changed_at = models.DateTimeField(auto_now_add=True)
    # Why it changed, e.g. "Wrong amount, re-checking HVAC".
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["changed_at", "id"]

    def __str__(self):
        return f"{self.draw}: {self.from_status or '-'} -> {self.to_status}"


class LoanDocument(models.Model):
    """A file stored in GHL media; only its metadata and link are kept here.

    Uploads from the app are pushed to GHL first; files already in GHL media are picked up by
    the sync. Both are keyed by ghl_media_id.
    """

    class Direction(models.TextChoices):
        BORROWER_UPLOAD = "borrower_upload", "Borrower upload"
        LENDER_SHARE = "lender_share", "Lender share"

    class Origin(models.TextChoices):
        APP = "app", "Uploaded in app"
        CHAT = "chat", "Sent in chat"
        GHL = "ghl", "Imported from GHL"

    class ReviewStatus(models.TextChoices):
        UNREVIEWED = "unreviewed", "Unreviewed"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"

    # Null while a file imported from GHL, or sent in a group/direct chat, has no loan yet.
    loan = models.ForeignKey(
        Loan, on_delete=models.CASCADE, null=True, blank=True, related_name="documents",
    )
    request = models.ForeignKey(
        DocumentRequest, on_delete=models.SET_NULL, null=True, blank=True, related_name="documents",
    )
    # Photos, invoices, lien waivers and inspection reports for a draw; document_type says which.
    draw = models.ForeignKey(
        DrawRequest, on_delete=models.SET_NULL, null=True, blank=True, related_name="files",
    )
    message = models.ForeignKey(
        "chats.Message", on_delete=models.SET_NULL, null=True, blank=True, related_name="attachments",
    )
    document_type = models.ForeignKey(
        DocumentType, on_delete=models.PROTECT, null=True, blank=True, related_name="documents",
    )
    direction = models.CharField(
        max_length=20, choices=Direction.choices, default=Direction.BORROWER_UPLOAD,
    )
    # False for files shared in an internal group or direct message.
    visible_to_borrower = models.BooleanField(default=True)
    origin = models.CharField(max_length=10, choices=Origin.choices, db_index=True)
    review_status = models.CharField(
        max_length=20, choices=ReviewStatus.choices, default=ReviewStatus.UNREVIEWED, db_index=True,
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    ghl_media_id = models.CharField(max_length=64, unique=True)
    ghl_folder_id = models.CharField(max_length=64, blank=True, db_index=True)
    location_id = models.CharField(max_length=64, db_index=True)
    # GHL media URLs are public; serve files only through an access-checked endpoint.
    url = models.URLField(max_length=1024)
    name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100, blank=True)
    size = models.PositiveBigIntegerField(null=True, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    uploaded_at = models.DateTimeField()
    raw = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-uploaded_at", "-id"]

    def __str__(self):
        return self.name
