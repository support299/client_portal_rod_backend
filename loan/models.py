from django.conf import settings
from django.db import models
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
