import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Subquery

from ghl.models import GHLOpportunity, GHLUser

from .models import Loan, LoanOfficer

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
