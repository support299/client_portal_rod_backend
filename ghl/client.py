"""Helpers for GHL OAuth v3 token exchange/refresh and authenticated API calls.

Docs: https://marketplace.gohighlevel.com/docs/ghl/oauth/o-auth-2-0
"""

from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import GHLToken

AUTHORIZE_URL = "https://marketplace.gohighlevel.com/oauth/chooselocation"
API_BASE = "https://services.leadconnectorhq.com"
TOKEN_URL = f"{API_BASE}/oauth/token"
LOCATION_TOKEN_URL = f"{API_BASE}/oauth/location-token"
API_VERSION = "v3"
REFRESH_MARGIN = timedelta(minutes=5)


class GHLAuthError(Exception):
    pass


def _post_form(url, data, headers=None):
    resp = requests.post(
        url,
        data=data,
        headers={"Accept": "application/json", "Version": API_VERSION, **(headers or {})},
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise GHLAuthError(f"GHL token request failed ({resp.status_code}): {resp.text}")
    return resp.json()


def _post_token(data):
    return _post_form(TOKEN_URL, {
        "client_id": settings.GHL_CLIENT_ID,
        "client_secret": settings.GHL_CLIENT_SECRET,
        **data,
    })


def save_tokens(data, token=None, user_type=None):
    """Create or update a GHLToken from a GHL token response."""
    fields = {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": timezone.now() + timedelta(seconds=int(data["expires_in"])),
        "scope": data.get("scope", ""),
    }
    if token is not None:
        for key, value in fields.items():
            setattr(token, key, value)
        token.save()
        return token

    token, _ = GHLToken.objects.update_or_create(
        user_type=user_type or data.get("userType") or settings.GHL_USER_TYPE,
        location_id=data.get("locationId") or "",
        company_id=data.get("companyId") or "",
        defaults=fields,
    )
    return token


def exchange_code(code):
    return save_tokens(_post_token({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.GHL_REDIRECT_URI,
        "user_type": settings.GHL_USER_TYPE,
    }))


def get_access_token(token, margin=REFRESH_MARGIN):
    """Return a valid access token for `token`, refreshing it if it expires within `margin`.

    Refresh tokens are single-use, so the row is locked while refreshing to
    stop concurrent requests from burning the same refresh token.
    """
    if token.expires_at - timezone.now() > margin:
        return token.access_token

    with transaction.atomic():
        locked = GHLToken.objects.select_for_update().get(pk=token.pk)
        if locked.expires_at - timezone.now() <= margin:
            save_tokens(_post_token({
                "grant_type": "refresh_token",
                "refresh_token": locked.refresh_token,
                "user_type": locked.user_type,
            }), token=locked)
    token.refresh_from_db()
    return token.access_token


def get_location_token(company_token, location_id):
    """Use an agency (Company) token to get and store a token for one location."""
    data = _post_form(
        LOCATION_TOKEN_URL,
        {"companyId": company_token.company_id, "locationId": location_id},
        headers={"Authorization": f"Bearer {get_access_token(company_token)}"},
    )
    data.setdefault("companyId", company_token.company_id)
    data.setdefault("locationId", location_id)
    return save_tokens(data, user_type="Location")


def api_request(token, method, path, version=API_VERSION, **kwargs):
    """Make an authenticated call to the GHL API, e.g. api_request(t, "GET", "/contacts/", params={...})."""
    headers = {
        "Authorization": f"Bearer {get_access_token(token)}",
        "Version": version,
        "Accept": "application/json",
        **kwargs.pop("headers", {}),
    }
    kwargs.setdefault("timeout", 30)
    return requests.request(method, f"{API_BASE}{path}", headers=headers, **kwargs)
