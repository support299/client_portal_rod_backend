"""Password set/reset links: issue a PasswordLink and email it through the GHL webhook."""

import logging
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.db import transaction

from .models import PasswordLink

logger = logging.getLogger(__name__)


class PasswordLinkSendError(Exception):
    """The email webhook could not be reached or rejected the request."""


def password_link_url(raw_token):
    return f"{settings.FRONTEND_URL.rstrip('/')}/set-password?{urlencode({'token': raw_token})}"


@transaction.atomic
def send_password_link(user, purpose=None, created_by=None):
    """Issue a new link for `user` and send it; previous active links are revoked.

    `purpose` defaults to SET for users who have never had a password, RESET otherwise.
    Raises PasswordLinkSendError if the webhook fails; the transaction is rolled back, so the
    user's earlier links stay valid.
    """
    if purpose is None:
        purpose = PasswordLink.Purpose.RESET if user.has_usable_password() else PasswordLink.Purpose.SET

    link, raw_token = PasswordLink.issue(user, purpose, created_by=created_by)
    payload = {
        "isNewPassword": purpose == PasswordLink.Purpose.SET,
        "user": {
            "id": str(user.pk),
            "name": user.get_full_name() or user.email,
            "email": user.email,
        },
        "reset_link": password_link_url(raw_token),
        "expires_in_hours": int(PasswordLink.EXPIRY[purpose].total_seconds() // 3600),
        "email": user.email,
    }
    # The link is a credential, so it's masked in the logs
    log_payload = {**payload, "reset_link": "<hidden>"}
    logger.info(
        "Sending %s password link to user %s via %s: %s",
        purpose, user.pk, settings.PASSWORD_LINK_WEBHOOK_URL, log_payload,
    )
    try:
        resp = requests.post(settings.PASSWORD_LINK_WEBHOOK_URL, json=payload, timeout=30)
    except requests.RequestException as exc:
        logger.error("Password link webhook unreachable for user %s: %s", user.pk, exc)
        raise PasswordLinkSendError(str(exc)) from exc
    if not resp.ok:
        logger.error(
            "Password link webhook rejected the request for user %s: HTTP %s %s",
            user.pk, resp.status_code, resp.text[:1000],
        )
        raise PasswordLinkSendError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    logger.info(
        "Password link webhook accepted for user %s: HTTP %s %s", user.pk, resp.status_code, resp.text[:1000]
    )

    try:
        execution_id = resp.json().get("id") or ""
    except (ValueError, AttributeError):
        execution_id = ""
    if execution_id:
        link.webhook_execution_id = str(execution_id)[:64]
        link.save(update_fields=["webhook_execution_id"])
    return link
