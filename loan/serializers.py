from rest_framework import serializers

from accounts.models import User
from accounts.serializers import UserSerializer

from .models import Loan


class LoanOfficerSerializer(UserSerializer):
    # The officer who is the opportunity's assigned user in GHL (listed first, highlight it).
    is_ghl_assignee = serializers.BooleanField(read_only=True, default=False)
    # Null when the officer has no GHL user, so they can't be made the main officer.
    ghl_user_id = serializers.CharField(source="ghl_user.user_id", read_only=True, default=None)

    class Meta(UserSerializer.Meta):
        fields = UserSerializer.Meta.fields + ["is_ghl_assignee", "ghl_user_id"]


class LoanSerializer(serializers.ModelSerializer):
    # Read through the opportunity: GHL is the source of truth for these.
    name = serializers.CharField(source="opportunity.name", read_only=True)
    loan_amount = serializers.DecimalField(source="opportunity.monetary_value", max_digits=14, decimal_places=2, read_only=True)
    contact = serializers.CharField(source="opportunity.contact_id", read_only=True)
    location_id = serializers.CharField(source="opportunity.location_id", read_only=True)
    pipeline_id = serializers.CharField(source="opportunity.pipeline_id", read_only=True)
    pipeline_name = serializers.CharField(source="opportunity.stage.pipeline_name", read_only=True, allow_null=True)
    pipeline_stage_id = serializers.CharField(source="opportunity.pipeline_stage_id", read_only=True)
    stage_name = serializers.CharField(source="opportunity.stage.name", read_only=True, allow_null=True)
    opportunity_status = serializers.CharField(source="opportunity.status", read_only=True)
    source = serializers.CharField(source="opportunity.source", read_only=True)
    assigned_to = serializers.CharField(source="opportunity.assigned_to", read_only=True)
    lost_reason_id = serializers.CharField(source="opportunity.lost_reason_id", read_only=True)
    expected_close_date = serializers.DateField(source="opportunity.expected_close_date", read_only=True)
    custom_fields = serializers.JSONField(source="opportunity.custom_fields", read_only=True)
    borrower = UserSerializer(read_only=True)
    loan_officers = LoanOfficerSerializer(many=True, read_only=True)  # GHL assignee first
    loan_officer = LoanOfficerSerializer(read_only=True)  # first officer, for single-officer clients
    # The status is the opportunity's stage in the loan pipeline: the id is the stable key, the name the label.
    status = serializers.CharField(source="opportunity.pipeline_stage_id", read_only=True)
    status_label = serializers.CharField(source="opportunity.stage.name", read_only=True, allow_null=True)

    class Meta:
        model = Loan
        fields = [
            "id", "opportunity", "contact", "borrower", "loan_officers", "loan_officer", "status", "status_label",
            "name", "loan_amount", "location_id", "pipeline_id", "pipeline_name",
            "pipeline_stage_id", "stage_name", "opportunity_status", "source", "assigned_to",
            "lost_reason_id", "expected_close_date", "custom_fields", "created_by", "created_at", "updated_at",
        ]
        read_only_fields = fields


class LoanOfficerContactSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="get_full_name", read_only=True)

    class Meta:
        model = User
        fields = ["full_name", "email", "phone"]


class BorrowerLoanSerializer(serializers.ModelSerializer):
    """What a borrower sees of their own loan: no internal CRM/pipeline fields."""

    loan_officers = LoanOfficerContactSerializer(many=True, read_only=True)
    loan_officer = LoanOfficerContactSerializer(read_only=True)
    status = serializers.CharField(source="opportunity.pipeline_stage_id", read_only=True)
    status_label = serializers.CharField(source="opportunity.stage.name", read_only=True, allow_null=True)
    name = serializers.CharField(source="opportunity.name", read_only=True)
    loan_amount = serializers.DecimalField(source="opportunity.monetary_value", max_digits=14, decimal_places=2, read_only=True)
    expected_close_date = serializers.DateField(source="opportunity.expected_close_date", read_only=True)

    class Meta:
        model = Loan
        fields = [
            "id", "name", "loan_amount", "status", "status_label", "expected_close_date",
            "loan_officers", "loan_officer", "created_at", "updated_at",
        ]
        read_only_fields = fields


class LoanUpdateSerializer(serializers.ModelSerializer):
    """Editable loan fields. The officers may only be changed by admins (enforced in validate).

    loan_officers is saved on the loan; name, loan_amount, expected_close_date and
    main_officer (as assigned_to) belong to the opportunity and are pushed to GHL by the view
    (see LoanDetailView).
    """

    name = serializers.CharField(max_length=255, required=False)
    loan_amount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    expected_close_date = serializers.DateField(required=False, allow_null=True)
    loan_officers = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.exclude(role=User.Role.BORROWER), many=True, required=False,
    )
    # Single-officer shorthand: same as loan_officers=[id] (or [] for null).
    loan_officer = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.exclude(role=User.Role.BORROWER), allow_null=True, required=False, write_only=True,
    )
    # The main officer: joins the team and becomes the opportunity's assigned user in GHL.
    main_officer = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.exclude(role=User.Role.BORROWER).select_related("ghl_user"),
        required=False, write_only=True,
    )

    class Meta:
        model = Loan
        fields = ["name", "loan_amount", "expected_close_date", "loan_officers", "loan_officer", "main_officer"]

    def validate_loan_amount(self, value):
        if value < 0:
            raise serializers.ValidationError("Loan amount can't be negative.")
        return value

    def validate(self, attrs):
        user = self.context["request"].user
        if "loan_officer" in attrs:
            if "loan_officers" in attrs:
                raise serializers.ValidationError("Send loan_officers or loan_officer, not both.")
            single = attrs.pop("loan_officer")
            attrs["loan_officers"] = [single] if single else []
        if "loan_officers" in attrs and not user.is_app_admin:
            current = {u.pk for u in self.instance.loan_officers}
            if {u.pk for u in attrs["loan_officers"]} != current:
                raise serializers.ValidationError({"loan_officers": "Only admins can reassign a loan."})
        main = attrs.get("main_officer")
        if main is not None:
            if not user.is_app_admin and main.pk != self.instance.ghl_assignee_id:
                raise serializers.ValidationError({"main_officer": "Only admins can reassign a loan."})
            ghl_user = getattr(main, "ghl_user", None)
            if ghl_user is None or ghl_user.deleted:
                raise serializers.ValidationError(
                    {"main_officer": f"{main.email} has no GHL user, so they can't be assigned in GHL."}
                )
            if "loan_officers" in attrs and main not in attrs["loan_officers"]:
                attrs["loan_officers"].append(main)
        return attrs

    def to_representation(self, instance):
        return LoanSerializer(instance, context=self.context).data
