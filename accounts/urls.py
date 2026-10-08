from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("me/", views.MeView.as_view(), name="me"),
    path("users/", views.UserCreateView.as_view(), name="user_create"),
    path("users/<int:pk>/", views.UserUpdateView.as_view(), name="user_update"),
]
