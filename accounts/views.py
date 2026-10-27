import logging

from django.db import transaction
from django.db.models import Count, Q, Value
from django.db.models.functions import Concat
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.generics import ListCreateAPIView, UpdateAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from .models import PasswordLink, User
from .serializers import (
    ForgotPasswordSerializer,
    LoginSerializer,
    PasswordLinkTokenSerializer,
    SendPasswordLinkSerializer,
    SetPasswordSerializer,
    UserSerializer,
    UserWriteSerializer,
)
from .services import PasswordLinkSendError, send_password_link

logger = logging.getLogger(__name__)


class IsAppAdmin(BasePermission):
    """Allow users with the admin role, or Django staff."""

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_app_admin)


class IsStaffMember(BasePermission):
    """Allow every role except borrowers."""

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.role != User.Role.BORROWER)


class LoginView(TokenObtainPairView):
    serializer_class = LoginSerializer


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class UserPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


class UserCreateView(ListCreateAPIView):
    """List users (GET) or create one (POST).

    GET query params (all optional): search (partial, case-insensitive match on
    name, email or phone), role, staff (true: every role except borrower,
    false: borrowers only), is_active (true/false), page, page_size.
    POST body: email, first_name, last_name, phone, role, is_active. No password: the user
    sets one from the link emailed on their first sign-in.
    """

    serializer_class = UserWriteSerializer
    pagination_class = UserPagination
    permission_classes = [IsAppAdmin]

    def get_queryset(self):
        params = self.request.query_params
        qs = User.objects.select_related("ghl_user").annotate(loan_count=Count("loans"))

        staff = params.get("staff", "").lower()
        if staff == "true":
            qs = qs.exclude(role=User.Role.BORROWER)
        elif staff == "false":
            qs = qs.filter(role=User.Role.BORROWER)

        role = params.get("role")
        if role:
            qs = qs.filter(role=role)

        is_active = params.get("is_active", "").lower()
        if is_active in ("true", "false"):
            qs = qs.filter(is_active=is_active == "true")

        search = params.get("search", "").strip()
        if search:
            qs = qs.annotate(search_name=Concat("first_name", Value(" "), "last_name")).filter(
                Q(search_name__icontains=search)
                | Q(email__icontains=search)
                | Q(phone__icontains=search)
            )
        return qs.order_by("first_name", "last_name", "id")


class UserUpdateView(UpdateAPIView):
    """Update a user (PATCH for partial, PUT for full). password is optional."""

    queryset = User.objects.all()
    serializer_class = UserWriteSerializer
    permission_classes = [IsAppAdmin]


class UserPasswordLinkView(APIView):
    """Email a user a set/reset password link (POST). Body: purpose (optional: set/reset).

    The link itself only goes out in the email; the response just confirms it was sent.
    """

    permission_classes = [IsAppAdmin]

    def post(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        serializer = SendPasswordLinkSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            send_password_link(user, serializer.validated_data.get("purpose"), created_by=request.user)
        except PasswordLinkSendError:
            logger.exception("Could not send password link to user %s", user.pk)
            return Response({"detail": "Could not send the email. Try again."}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({"detail": f"Password link sent to {user.email}."})


class LoginEmailView(APIView):
    """First login step. Body: email.

    Answers next="password" if the user has a password; otherwise emails them a set-password link
    (unless one went out within PasswordLink.RESEND_COOLDOWN) and answers next="link_sent".
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = User.objects.normalize_email(serializer.validated_data["email"])
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is None:
            return Response({"email": ["No account found for this email."]}, status=status.HTTP_400_BAD_REQUEST)
        if user.has_usable_password():
            return Response({"next": "password"})
        if PasswordLink.recently_sent(user):
            logger.info("Set-password link for user %s not re-sent: within the resend cooldown", user.pk)
        else:
            try:
                send_password_link(user, PasswordLink.Purpose.SET)
            except PasswordLinkSendError:
                logger.exception("Could not send set-password link to user %s", user.pk)
                return Response(
                    {"detail": "Could not send the email. Try again."}, status=status.HTTP_502_BAD_GATEWAY
                )
        return Response({"next": "link_sent"})


class ForgotPasswordView(APIView):
    """Send a password link if the email belongs to an active user: a set link for users created
    without a password (e.g. on onboarding), a reset link otherwise. Repeat requests within
    PasswordLink.RESEND_COOLDOWN send nothing. Always answers the same, so the endpoint can't
    be used to find out which emails have accounts."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = User.objects.normalize_email(serializer.validated_data["email"])
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is None:
            logger.info("Password link not sent: no active user with email %s", email)
        elif PasswordLink.recently_sent(user):
            logger.info("Password link for user %s not re-sent: within the resend cooldown", user.pk)
        else:
            try:
                send_password_link(user)
            except PasswordLinkSendError:
                logger.exception("Could not send password link to user %s", user.pk)
        return Response({"detail": "If an account exists for this email, a password link has been sent."})


class PasswordLinkCheckView(APIView):
    """Check the token from an emailed link before showing the password form. Body: token."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = PasswordLinkTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        link = serializer.validated_data["link"]
        return Response({"purpose": link.purpose, "email": link.user.email})


class SetPasswordView(APIView):
    """Set the password from an emailed link and use the link up. Body: token, password."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = SetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            # Lock the row so two submits with the same token can't both use it.
            link = PasswordLink.objects.select_for_update().select_related("user").get(
                pk=serializer.validated_data["link"].pk
            )
            if not link.is_active:
                return Response(
                    {"token": ["This link is invalid or has expired."]}, status=status.HTTP_400_BAD_REQUEST
                )
            link.user.set_password(serializer.validated_data["password"])
            link.user.save(update_fields=["password"])
            link.used_at = timezone.now()
            link.save(update_fields=["used_at"])
        return Response({"detail": "Password updated. You can now log in."})
