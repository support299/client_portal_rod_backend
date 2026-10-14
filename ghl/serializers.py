from rest_framework import serializers

from .models import GHLContact, GHLOpportunity, GHLOutboundLog, GHLPipelineStage, GHLToken, GHLUser


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
            "effective_probability", "forecast_probability", "original_close_date",
            "followers", "forecast_slippage_count", "forecast_days_slipped",
            "custom_fields", "last_status_change_at",
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


class OpportunityCustomFieldSerializer(serializers.Serializer):
    id = serializers.CharField(max_length=64)
    field_value = serializers.JSONField()


class OpportunityUpdateSerializer(serializers.Serializer):
    """Fields of an opportunity that can be edited; every field is optional (partial update)."""

    name = serializers.CharField(max_length=255)
    monetary_value = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    status = serializers.ChoiceField(choices=GHLOpportunity.STATUS_CHOICES)
    pipeline_stage_id = serializers.CharField(max_length=64)
    assigned_to = serializers.CharField(max_length=64)
    expected_close_date = serializers.DateField(allow_null=True)
    pipeline_id = serializers.CharField(max_length=64)
    lost_reason_id = serializers.CharField(max_length=64, allow_blank=True)
    custom_fields = serializers.ListField(child=OpportunityCustomFieldSerializer())

    # GHL's update call has no equivalent for these, so they are saved in the dashboard only
    # (a later sync from GHL can overwrite them).
    LOCAL_ONLY_FIELDS = (
        "source", "followers", "original_close_date", "forecast_probability",
        "effective_probability", "forecast_slippage_count", "forecast_days_slipped",
    )
    source = serializers.CharField(max_length=255, allow_blank=True)
    followers = serializers.ListField(child=serializers.CharField(max_length=64))
    original_close_date = serializers.DateField(allow_null=True)
    forecast_probability = serializers.IntegerField(min_value=0, max_value=100, allow_null=True)
    effective_probability = serializers.IntegerField(min_value=0, max_value=100, allow_null=True)
    forecast_slippage_count = serializers.IntegerField(min_value=0)
    forecast_days_slipped = serializers.IntegerField(min_value=0)

    def validate_pipeline_stage_id(self, value):
        opportunity = self.context["opportunity"]
        stage = GHLPipelineStage.objects.filter(stage_id=value).first()
        if stage is None or stage.location_id != opportunity.location_id:
            raise serializers.ValidationError("Unknown pipeline stage for this location.")
        return value

    def validate_assigned_to(self, value):
        if not GHLUser.objects.filter(user_id=value, deleted=False).exists():
            raise serializers.ValidationError("Unknown GHL user.")
        return value

    def validate(self, attrs):
        # Moving to another pipeline needs a stage from that pipeline.
        stage_id = attrs.get("pipeline_stage_id")
        pipeline_id = attrs.get("pipeline_id")
        if pipeline_id and not stage_id:
            raise serializers.ValidationError({"pipeline_stage_id": "Required when changing the pipeline."})
        if stage_id and pipeline_id:
            if not GHLPipelineStage.objects.filter(stage_id=stage_id, pipeline_id=pipeline_id).exists():
                raise serializers.ValidationError({"pipeline_stage_id": "Stage is not in that pipeline."})
        return attrs


class OutboundLogListSerializer(serializers.ModelSerializer):
    triggered_by_email = serializers.EmailField(source="triggered_by.email", read_only=True, default=None)

    class Meta:
        model = GHLOutboundLog
        fields = [
            "id", "action", "object_id", "location_id", "triggered_by", "triggered_by_email",
            "method", "endpoint", "http_status", "success", "error", "duration_ms", "created_at",
        ]


class OutboundLogDetailSerializer(OutboundLogListSerializer):
    class Meta(OutboundLogListSerializer.Meta):
        fields = OutboundLogListSerializer.Meta.fields + ["request_body", "response_body"]
