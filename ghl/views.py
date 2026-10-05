import secrets
from urllib.parse import quote, urlencode

from django.conf import settings
from django.http import HttpResponseRedirect
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response

from .client import AUTHORIZE_URL, GHLAuthError, exchange_code
from .models import GHLToken
from .serializers import GHLConnectionSerializer


@api_view(["GET"])
@permission_classes([AllowAny])
def oauth_start(request):
    """Redirect the user to GHL to pick a location and approve the app."""
    state = secrets.token_urlsafe(32)
    request.session["ghl_oauth_state"] = state
    params = {
        "response_type": "code",
        "client_id": settings.GHL_CLIENT_ID,
        "redirect_uri": settings.GHL_REDIRECT_URI,
        "scope": settings.GHL_SCOPES,
        "state": state,
    }
    return HttpResponseRedirect(f"{AUTHORIZE_URL}?{urlencode(params, quote_via=quote)}")


@api_view(["GET"])
@permission_classes([AllowAny])
def oauth_callback(request):
    """GHL redirects here with ?code=... after the user approves."""
    if request.query_params.get("error"):
        return Response({"error": request.query_params["error"]}, status=status.HTTP_400_BAD_REQUEST)

    # Only enforce state when the flow was started from oauth_start; installs
    # launched directly from the GHL Marketplace arrive without one.
    expected_state = request.session.pop("ghl_oauth_state", None)
    if expected_state and request.query_params.get("state") != expected_state:
        return Response({"error": "invalid state"}, status=status.HTTP_400_BAD_REQUEST)

    code = request.query_params.get("code")
    if not code:
        return Response({"error": "missing code"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        token = exchange_code(code)
    except GHLAuthError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    if settings.GHL_SUCCESS_REDIRECT:
        return HttpResponseRedirect(settings.GHL_SUCCESS_REDIRECT)
    return Response({"connected": True, **GHLConnectionSerializer(token).data})


@api_view(["GET"])
@permission_classes([IsAdminUser])
def oauth_status(request):
    """List connected GHL accounts (no tokens exposed)."""
    return Response({"connections": GHLConnectionSerializer(GHLToken.objects.all(), many=True).data})
