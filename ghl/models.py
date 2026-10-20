from django.conf import settings
from django.db import models


class GHLToken(models.Model):
    """OAuth tokens for one connected GHL location (or agency/company)."""

    USER_TYPE_CHOICES = [("Location", "Location"), ("Company", "Company")]

    location_id = models.CharField(max_length=64, blank=True, db_index=True)
    company_id = models.CharField(max_length=64, blank=True, db_index=True)
    user_type = models.CharField(max_length=16, choices=USER_TYPE_CHOICES)
    access_token = models.TextField()
    refresh_token = models.TextField()
    expires_at = models.DateTimeField()
    scope = models.TextField(blank=True)
    # Location tokens only: the GHL media folder that holds this location's loan documents.
    media_folder_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user_type", "location_id", "company_id"],
                name="unique_ghl_token_owner",
            ),
        ]

    def __str__(self):
        return f"{self.user_type} {self.location_id or self.company_id}"


class GHLContact(models.Model):
    """A contact synced from a GHL location."""

    ghl_id = models.CharField(max_length=64, unique=True)
    location_id = models.CharField(max_length=64, db_index=True)
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    contact_name = models.CharField(max_length=255, blank=True)
    email = models.CharField(max_length=255, blank=True, db_index=True)
    phone = models.CharField(max_length=64, blank=True, db_index=True)
    company_name = models.CharField(max_length=255, blank=True)
    type = models.CharField(max_length=64, blank=True)
    source = models.CharField(max_length=255, blank=True)
    assigned_to = models.CharField(max_length=64, blank=True)
    tags = models.JSONField(default=list, blank=True)
    custom_fields = models.JSONField(default=list, blank=True)
    address1 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=255, blank=True)
    postal_code = models.CharField(max_length=32, blank=True)
    country = models.CharField(max_length=64, blank=True)
    dnd = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False, db_index=True)
    date_added = models.DateTimeField(null=True, blank=True)
    date_updated = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)
    # The borrower login for this contact; linked by email (see AccountLinkService).
    account = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="ghl_contacts",
    )

    def __str__(self):
        return self.contact_name or self.email or self.ghl_id


class GHLUser(models.Model):
    """A GHL user; one row per user, with every location they belong to."""

    user_id = models.CharField(max_length=64, unique=True)
    company_id = models.CharField(max_length=64, blank=True, db_index=True)
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    name = models.CharField(max_length=255, blank=True)
    email = models.CharField(max_length=255, blank=True, db_index=True)
    phone = models.CharField(max_length=64, blank=True)
    extension = models.CharField(max_length=32, blank=True)
    role_type = models.CharField(max_length=32, blank=True)  # "account" or "agency"
    role = models.CharField(max_length=32, blank=True)  # "admin" or "user"
    location_ids = models.JSONField(default=list, blank=True)
    permissions = models.JSONField(default=dict, blank=True)
    deleted = models.BooleanField(default=False)
    date_added = models.DateTimeField(null=True, blank=True)
    date_updated = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)
    # The staff login for this GHL user; linked by email (see AccountLinkService).
    account = models.OneToOneField(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="ghl_user",
    )

    def __str__(self):
        return self.name or self.email or self.user_id


class GHLPipelineStage(models.Model):
    """A pipeline stage synced from GHL; pipeline details are repeated on each stage."""

    stage_id = models.CharField(max_length=64, unique=True)
    pipeline_id = models.CharField(max_length=64, db_index=True)
    pipeline_name = models.CharField(max_length=255, blank=True)
    location_id = models.CharField(max_length=64, db_index=True)
    name = models.CharField(max_length=255, blank=True)
    position = models.IntegerField(default=0)
    win_probability = models.IntegerField(null=True, blank=True)
    show_in_funnel = models.BooleanField(default=True)
    show_in_pie_chart = models.BooleanField(default=True)
    raw = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["pipeline_id", "position"]

    def __str__(self):
        return f"{self.pipeline_name} / {self.name}" if self.pipeline_name else self.name or self.stage_id


class GHLOpportunity(models.Model):
    """An opportunity synced from a GHL location."""

    STATUS_CHOICES = [("open", "Open"), ("won", "Won"), ("lost", "Lost"), ("abandoned", "Abandoned")]

    opportunity_id = models.CharField(max_length=64, unique=True)
    location_id = models.CharField(max_length=64, db_index=True)
    # Keyed on the GHL contact id, so contact_id holds the same value GHL sends.
    contact = models.ForeignKey(
        GHLContact,
        to_field="ghl_id",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="opportunities",
    )
    name = models.CharField(max_length=255, blank=True)
    monetary_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    pipeline_id = models.CharField(max_length=64, blank=True, db_index=True)
    # Raw stage id as GHL sends it, kept even if the stage isn't in our table.
    pipeline_stage_id = models.CharField(max_length=64, blank=True, db_index=True)
    stage = models.ForeignKey(
        GHLPipelineStage,
        to_field="stage_id",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="opportunities",
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, blank=True, db_index=True)
    source = models.CharField(max_length=255, blank=True)
    assigned_to = models.CharField(max_length=64, blank=True)
    lost_reason_id = models.CharField(max_length=64, blank=True)
    followers = models.JSONField(default=list, blank=True)
    expected_close_date = models.DateField(null=True, blank=True)
    original_close_date = models.DateField(null=True, blank=True)
    forecast_slippage_count = models.IntegerField(default=0)
    forecast_days_slipped = models.IntegerField(default=0)
    forecast_probability = models.IntegerField(null=True, blank=True)
    effective_probability = models.IntegerField(null=True, blank=True)
    custom_fields = models.JSONField(default=list, blank=True)
    attributions = models.JSONField(default=list, blank=True)
    last_status_change_at = models.DateTimeField(null=True, blank=True)
    last_stage_change_at = models.DateTimeField(null=True, blank=True)
    date_added = models.DateTimeField(null=True, blank=True)
    date_updated = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "GHL opportunities"

    def __str__(self):
        return self.name or self.opportunity_id


class GHLOutboundLog(models.Model):
    """One row per API call we make to GHL (e.g. updating an opportunity)."""

    action = models.CharField(max_length=100, db_index=True)
    object_id = models.CharField(max_length=64, blank=True, db_index=True)
    location_id = models.CharField(max_length=64, blank=True, db_index=True)
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="ghl_outbound_logs",
    )
    method = models.CharField(max_length=8)
    endpoint = models.CharField(max_length=255)
    request_body = models.JSONField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    success = models.BooleanField(default=False, db_index=True)
    error = models.TextField(blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} {self.object_id} [{self.http_status}]"
