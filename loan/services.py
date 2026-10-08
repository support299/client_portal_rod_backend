from django.contrib.auth import get_user_model
from django.db import transaction

from ghl.models import GHLUser

from .models import Loan


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
    def create_from_opportunity(opportunity, created_by=None):
        """Create a Loan (and the borrower login if needed) from a GHL opportunity.

        Returns (loan, borrower_created).
        """
        if Loan.objects.filter(opportunity=opportunity).exists():
            raise LoanConversionError("A loan has already been created for this opportunity.")
        if opportunity.contact is None:
            raise LoanConversionError("The opportunity has no contact.")

        borrower, borrower_created = LoanService.get_or_create_borrower(opportunity.contact)
        officer = (
            GHLUser.objects.filter(user_id=opportunity.assigned_to, account__isnull=False)
            .values_list("account", flat=True).first()
            if opportunity.assigned_to else None
        )
        stage = opportunity.stage
        loan = Loan.objects.create(
            opportunity=opportunity,
            contact=opportunity.contact,
            borrower=borrower,
            loan_officer_id=officer,
            name=opportunity.name,
            loan_amount=opportunity.monetary_value,
            location_id=opportunity.location_id,
            pipeline_id=opportunity.pipeline_id,
            pipeline_name=stage.pipeline_name if stage else "",
            pipeline_stage_id=opportunity.pipeline_stage_id,
            stage_name=stage.name if stage else "",
            opportunity_status=opportunity.status,
            source=opportunity.source,
            assigned_to=opportunity.assigned_to,
            expected_close_date=opportunity.expected_close_date,
            custom_fields=opportunity.custom_fields,
            created_by=created_by,
        )
        return loan, borrower_created
