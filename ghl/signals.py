from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from loan.services import LoanService

from .services import AccountLinkService

LINK_FIELDS = {"email", "role"}


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def link_account_to_ghl(sender, instance, created, update_fields=None, **kwargs):
    """Link a login to its GHL user/contacts when it is created or its email/role may have changed."""
    # Saves limited to other fields (e.g. last_login on every login) can't change the match.
    if update_fields is not None and not LINK_FIELDS & set(update_fields):
        return
    AccountLinkService.link_account(instance)
    if instance.role != instance.Role.BORROWER:
        # A newly linked staff login joins the loans they're assigned to in GHL.
        LoanService.add_ghl_assignees()
