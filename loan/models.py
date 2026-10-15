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
