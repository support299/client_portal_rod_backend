from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import PasswordLink, User


class UserSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="get_full_name", read_only=True)
    role_label = serializers.CharField(source="get_role_display", read_only=True)

    class Meta:
        model = User
        fields = ["id", "email", "first_name", "last_name", "full_name", "phone", "role", "role_label"]


class UserWriteSerializer(serializers.ModelSerializer):
    """Create or update a user. Passwords are never set here: users set their own from an
    emailed link (on first sign-in, or via an admin-sent reset link)."""

    full_name = serializers.CharField(source="get_full_name", read_only=True)
    role_label = serializers.CharField(source="get_role_display", read_only=True)
    # Annotated in the user list view; absent (None) on create/update responses.
    loan_count = serializers.IntegerField(read_only=True, default=None)
    # The linked GHL user's id, or null; only users with one can be a loan's main officer.
    ghl_user_id = serializers.CharField(source="ghl_user.user_id", read_only=True, default=None)
    # The broker managing this borrower (only borrowers can have one).
    broker = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(role=User.Role.BROKER), allow_null=True, required=False,
    )
    broker_name = serializers.CharField(source="broker.get_full_name", read_only=True, default=None)

    class Meta:
        model = User
        fields = [
            "id", "email", "first_name", "last_name", "full_name",
            "phone", "role", "role_label", "is_active", "loan_count", "ghl_user_id",
            "broker", "broker_name",
        ]

    def validate_email(self, value):
        return User.objects.normalize_email(value)

    def validate(self, attrs):
        # Borrower logins are tied to their loans, so they can't be turned into staff.
        if (
            self.instance is not None
            and self.instance.role == User.Role.BORROWER
            and attrs.get("role", User.Role.BORROWER) != User.Role.BORROWER
        ):
            raise serializers.ValidationError({"role": "A borrower's role can't be changed."})
        role = attrs.get("role", self.instance.role if self.instance else User.Role.BORROWER)
        broker = attrs.get("broker", self.instance.broker if self.instance else None)
        if broker is not None and role != User.Role.BORROWER:
            raise serializers.ValidationError({"broker": "Only borrowers can be assigned a broker."})
        return attrs

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class LoginSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["email"] = user.email
        token["role"] = user.role
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        data["user"] = UserSerializer(self.user).data
        return data


class SendPasswordLinkSerializer(serializers.Serializer):
    """Admin sends a link; purpose is optional (set if the user has no password yet, else reset)."""

    purpose = serializers.ChoiceField(choices=PasswordLink.Purpose.choices, required=False)


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordLinkTokenSerializer(serializers.Serializer):
    """Resolves `token` to an active PasswordLink, available as attrs["link"]."""

    token = serializers.CharField()

    def validate(self, attrs):
        link = (
            PasswordLink.active()
            .select_related("user")
            .filter(token_hash=PasswordLink.hash_token(attrs["token"]), user__is_active=True)
            .first()
        )
        if link is None:
            raise serializers.ValidationError({"token": "This link is invalid or has expired."})
        attrs["link"] = link
        return attrs


class SetPasswordSerializer(PasswordLinkTokenSerializer):
    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    def validate(self, attrs):
        attrs = super().validate(attrs)
        try:
            validate_password(attrs["password"], attrs["link"].user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)})
        return attrs
