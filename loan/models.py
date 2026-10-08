from django.conf import settings
from django.db import models


class Loan(models.Model):
    """A loan created from a GHL opportunity.

    Opportunity details are copied in at conversion time, so the loan keeps its
    own values even as the opportunity keeps syncing from GHL.
    """

    class Status(models.TextChoices):
        APPLICATION_RECEIVED = "application_received", "Application Received"
        PROCESSING = "processing", "Processing"
        UNDERWRITING = "underwriting", "Underwriting"
        APPRAISAL = "appraisal", "Appraisal"
        FINAL_APPROVAL = "final_approval", "Final Approval"
        CLOSING = "closing", "Closing"
        FUNDED = "funded", "Funded"

    # One loan per opportunity; the loan is kept if the opportunity is deleted.
    opportunity = models.OneToOneField(
        "ghl.GHLOpportunity",
        to_field="opportunity_id",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="loan",
    )
    contact = models.ForeignKey(
        "ghl.GHLContact",
        to_field="ghl_id",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="loans",
    )
    borrower = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="loans",
    )
    loan_officer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="officer_loans",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.APPLICATION_RECEIVED, db_index=True)

    # Copied from the opportunity.
    name = models.CharField(max_length=255, blank=True)
    loan_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    location_id = models.CharField(max_length=64, blank=True, db_index=True)
    pipeline_id = models.CharField(max_length=64, blank=True)
    pipeline_name = models.CharField(max_length=255, blank=True)
    pipeline_stage_id = models.CharField(max_length=64, blank=True)
    stage_name = models.CharField(max_length=255, blank=True)
    opportunity_status = models.CharField(max_length=16, blank=True)
    source = models.CharField(max_length=255, blank=True)
    assigned_to = models.CharField(max_length=64, blank=True)  # GHL user id
    expected_close_date = models.DateField(null=True, blank=True)
    custom_fields = models.JSONField(default=list, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name or f"Loan {self.pk}"
