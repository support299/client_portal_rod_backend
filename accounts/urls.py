from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("login/email/", views.LoginEmailView.as_view(), name="login_email"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("me/", views.MeView.as_view(), name="me"),
    path("users/", views.UserCreateView.as_view(), name="user_create"),
    path("users/<int:pk>/", views.UserUpdateView.as_view(), name="user_update"),
    path("users/<int:pk>/password-link/", views.UserPasswordLinkView.as_view(), name="user_password_link"),
    path("password/forgot/", views.ForgotPasswordView.as_view(), name="password_forgot"),
    path("password/check/", views.PasswordLinkCheckView.as_view(), name="password_check"),
    path("password/set/", views.SetPasswordView.as_view(), name="password_set"),
]
