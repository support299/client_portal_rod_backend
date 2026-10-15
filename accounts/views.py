from django.db.models import Count, Q, Value
from django.db.models.functions import Concat
from rest_framework.generics import ListCreateAPIView, UpdateAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from .models import User
from .serializers import LoginSerializer, UserSerializer, UserWriteSerializer


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
    POST body: email, password, first_name, last_name, phone, role, is_active.
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
