from rest_framework import serializers

from accounts.models import User
from accounts.serializers import UserSerializer

from .models import Loan


class LoanSerializer(serializers.ModelSerializer):
    borrower = UserSerializer(read_only=True)
    loan_officer = UserSerializer(read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Loan
        fields = [
            "id", "opportunity", "contact", "borrower", "loan_officer", "status", "status_label",
            "name", "loan_amount", "location_id", "pipeline_id", "pipeline_name",
            "pipeline_stage_id", "stage_name", "opportunity_status", "source", "assigned_to",
            "expected_close_date", "custom_fields", "created_by", "created_at", "updated_at",
        ]
        read_only_fields = fields


class LoanOfficerContactSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="get_full_name", read_only=True)

    class Meta:
        model = User
        fields = ["full_name", "email", "phone"]


class BorrowerLoanSerializer(serializers.ModelSerializer):
    """What a borrower sees of their own loan: no internal CRM/pipeline fields."""

    loan_officer = LoanOfficerContactSerializer(read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Loan
        fields = [
            "id", "name", "loan_amount", "status", "status_label", "expected_close_date",
            "loan_officer", "created_at", "updated_at",
        ]
        read_only_fields = fields


class LoanUpdateSerializer(serializers.ModelSerializer):
    """Editable loan fields. loan_officer may only be changed by admins (enforced in validate)."""

    loan_officer = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.exclude(role=User.Role.BORROWER), allow_null=True, required=False,
    )

    class Meta:
        model = Loan
        fields = ["status", "name", "loan_amount", "expected_close_date", "custom_fields", "loan_officer"]

    def validate_loan_amount(self, value):
        if value < 0:
            raise serializers.ValidationError("Loan amount can't be negative.")
        return value

    def validate(self, attrs):
        user = self.context["request"].user
        if "loan_officer" in attrs and attrs["loan_officer"] != self.instance.loan_officer and not user.is_app_admin:
            raise serializers.ValidationError({"loan_officer": "Only admins can reassign a loan."})
        return attrs

    def to_representation(self, instance):
        return LoanSerializer(instance, context=self.context).data
