from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.contrib.auth.models import AnonymousUser
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed, InvalidToken, TokenError


@database_sync_to_async
def user_from_token(raw_token):
    auth = JWTAuthentication()
    try:
        return auth.get_user(auth.get_validated_token(raw_token))
    except (InvalidToken, TokenError, AuthenticationFailed):
        return AnonymousUser()


class JWTAuthMiddleware(BaseMiddleware):
    """Authenticate websockets with the same access token as the REST API: ws://.../?token=<access>.

    Browsers can't set headers on a websocket, so the token travels in the query string.
    """

    async def __call__(self, scope, receive, send):
        token = parse_qs(scope.get("query_string", b"").decode()).get("token", [None])[0]
        scope["user"] = await user_from_token(token) if token else AnonymousUser()
        return await super().__call__(scope, receive, send)
