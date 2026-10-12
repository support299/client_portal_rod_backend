from django.conf import settings
from django.db import models


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

    @property
    def loan_officers(self):
        """The assigned officers, in assignment order (prefetch officer_assignments__officer)."""
        return [a.officer for a in self.officer_assignments.all() if not a.is_deleted]

    @property
    def loan_officer(self):
        """The first-assigned officer (single-officer view of loan_officers)."""
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
