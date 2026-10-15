from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import User


class UserSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="get_full_name", read_only=True)
    role_label = serializers.CharField(source="get_role_display", read_only=True)

    class Meta:
        model = User
        fields = ["id", "email", "first_name", "last_name", "full_name", "phone", "role", "role_label"]


class UserWriteSerializer(serializers.ModelSerializer):
    """Create or update a user; password is hashed and optional on update."""

    full_name = serializers.CharField(source="get_full_name", read_only=True)
    role_label = serializers.CharField(source="get_role_display", read_only=True)
    password = serializers.CharField(write_only=True, required=False, style={"input_type": "password"})
    # Annotated in the user list view; absent (None) on create/update responses.
    loan_count = serializers.IntegerField(read_only=True, default=None)
    # The linked GHL user's id, or null; only users with one can be a loan's main officer.
    ghl_user_id = serializers.CharField(source="ghl_user.user_id", read_only=True, default=None)

    class Meta:
        model = User
        fields = [
            "id", "email", "password", "first_name", "last_name", "full_name",
            "phone", "role", "role_label", "is_active", "loan_count", "ghl_user_id",
        ]

    def validate_email(self, value):
        return User.objects.normalize_email(value)

    def validate_password(self, value):
        validate_password(value, self.instance)
        return value

    def validate(self, attrs):
        if self.instance is None and not attrs.get("password"):
            raise serializers.ValidationError({"password": "This field is required."})
        # Borrower logins are tied to their loans, so they can't be turned into staff.
        if (
            self.instance is not None
            and self.instance.role == User.Role.BORROWER
            and attrs.get("role", User.Role.BORROWER) != User.Role.BORROWER
        ):
            raise serializers.ValidationError({"role": "A borrower's role can't be changed."})
        return attrs

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        instance = super().update(instance, validated_data)
        if password:
            instance.set_password(password)
            instance.save(update_fields=["password"])
        return instance


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
