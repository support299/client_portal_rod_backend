from rest_framework import serializers

from .models import GHLContact, GHLOpportunity, GHLToken, GHLUser


class GHLConnectionSerializer(serializers.ModelSerializer):
    """A connected GHL account, without its tokens."""

    class Meta:
        model = GHLToken
        fields = ["id", "user_type", "location_id", "company_id", "expires_at", "updated_at"]


class OpportunityContactSerializer(serializers.ModelSerializer):
    class Meta:
        model = GHLContact
        fields = ["ghl_id", "contact_name", "email", "phone", "company_name", "tags"]


class OpportunitySerializer(serializers.ModelSerializer):
    contact = OpportunityContactSerializer(read_only=True)
    pipeline_name = serializers.CharField(source="stage.pipeline_name", default=None, read_only=True)
    stage_name = serializers.CharField(source="stage.name", default=None, read_only=True)
    # Annotated in the view from GHLUser.
    assigned_to_name = serializers.CharField(read_only=True, default=None)
    # Annotated in the view; set once the opportunity has been converted to a Loan.
    loan_id = serializers.IntegerField(read_only=True, default=None)

    class Meta:
        model = GHLOpportunity
        fields = [
            "id", "opportunity_id", "location_id", "name", "monetary_value", "status",
            "source", "pipeline_id", "pipeline_name", "pipeline_stage_id", "stage_name",
            "assigned_to", "assigned_to_name", "loan_id", "contact", "expected_close_date",
            "effective_probability", "custom_fields", "last_status_change_at",
            "last_stage_change_at", "date_added", "date_updated",
        ]


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = GHLUser
        fields = [
            "id", "user_id", "company_id", "first_name", "last_name", "name", "email",
            "phone", "extension", "role_type", "role", "location_ids", "deleted",
            "date_added", "date_updated",
        ]
