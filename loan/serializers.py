from django.urls import reverse
from rest_framework import serializers

from accounts.models import User
from accounts.serializers import UserSerializer

from .draw_services import DrawService
from .models import (
    DocumentRequest, DocumentType, DrawRequest, DrawRequestStatusChange, Loan, LoanDocument, LoanNote,
)
from .services import DocumentService


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
            "lost_reason_id", "expected_close_date", "custom_fields", "holdback_amount", "created_by",
            "created_at", "updated_at",
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
            "id", "name", "loan_amount", "status", "status_label", "expected_close_date", "holdback_amount",
            "loan_officers", "loan_officer", "created_at", "updated_at",
        ]
        read_only_fields = fields


class LoanUpdateSerializer(serializers.ModelSerializer):
    """Editable loan fields. The officers may only be changed by admins (enforced in validate).

    loan_officers and holdback_amount are saved on the loan; name, loan_amount, expected_close_date and
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
        fields = [
            "name", "loan_amount", "expected_close_date", "holdback_amount", "loan_officers", "loan_officer",
            "main_officer",
        ]

    def validate_loan_amount(self, value):
        if value < 0:
            raise serializers.ValidationError("Loan amount can't be negative.")
        return value

    def validate_holdback_amount(self, value):
        if value is None:
            if self.instance.draws.filter(is_deleted=False).exists():
                raise serializers.ValidationError("This loan has draws, so its holdback can't be removed.")
            return value
        if value < 0:
            raise serializers.ValidationError("Holdback can't be negative.")
        committed = self.instance.draw_summary()
        committed = committed["redeemed"] + committed["in_progress"]
        if value < committed:
            raise serializers.ValidationError(
                f"Draws already take ${committed:,.2f} of this loan, so the holdback can't be less."
            )
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


def validate_upload(file):
    if file.size > DocumentService.MAX_UPLOAD_SIZE:
        raise serializers.ValidationError(
            f"Files can be at most {DocumentService.MAX_UPLOAD_SIZE // (1024 * 1024)} MB."
        )
    return file


def validate_same_loan(serializer, doc_request):
    if doc_request is not None and doc_request.loan_id != serializer.context["loan"].pk:
        raise serializers.ValidationError("That request belongs to another loan.")
    return doc_request


class DocumentUserSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="get_full_name", read_only=True)

    class Meta:
        model = User
        fields = ["id", "full_name"]


class DocumentTypeSerializer(serializers.ModelSerializer):
    """A document type. template_file (multipart, fill_form types only) replaces the blank form."""

    has_template = serializers.SerializerMethodField()
    template_file = serializers.FileField(write_only=True, required=False, validators=[validate_upload])

    class Meta:
        model = DocumentType
        fields = [
            "id", "name", "category", "kind", "description", "is_default", "position", "is_active",
            "template_name", "has_template", "template_file",
        ]
        read_only_fields = ["template_name"]

    def get_has_template(self, obj):
        return bool(obj.template_ghl_media_id)

    def validate(self, attrs):
        kind = attrs.get("kind") or getattr(self.instance, "kind", DocumentType.Kind.UPLOAD)
        if "template_file" in attrs and kind != DocumentType.Kind.FILL_FORM:
            raise serializers.ValidationError({"template_file": "Only fill_form types have a blank form."})
        return attrs


class LoanDocumentSerializer(serializers.ModelSerializer):
    """A file's metadata. The GHL url is left out on purpose (files are public there): use download_url."""

    document_type_name = serializers.CharField(source="document_type.name", read_only=True, default=None)
    uploaded_by = DocumentUserSerializer(read_only=True)
    reviewed_by = DocumentUserSerializer(read_only=True)
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = LoanDocument
        fields = [
            "id", "loan", "request", "draw", "document_type", "document_type_name", "direction",
            "visible_to_borrower", "origin", "review_status", "reviewed_by", "reviewed_at",
            "name", "content_type", "size", "uploaded_by", "uploaded_at", "download_url",
        ]
        read_only_fields = fields

    def get_download_url(self, obj):
        # A shared blank form has no loan; it is downloaded through the loan whose request shares it.
        loan_id = obj.loan_id or self.context.get("loan_id")
        if loan_id is None:
            return None
        return reverse("loan:loan_document_download", kwargs={"loan_pk": loan_id, "pk": obj.pk})


class LoanDocumentUploadSerializer(serializers.Serializer):
    """POST body (multipart) for a new file.

    direction and visible_to_borrower are staff-only: borrower files are always borrower
    uploads, visible to them. Staff files default to a lender share, or a borrower upload when
    sent against a request (uploading on the borrower's behalf).
    """

    file = serializers.FileField(validators=[validate_upload])
    request = serializers.PrimaryKeyRelatedField(
        queryset=DocumentRequest.objects.filter(is_deleted=False), required=False, allow_null=True,
    )
    # A draw's photos, invoices, lien waivers or inspection report.
    draw = serializers.PrimaryKeyRelatedField(
        queryset=DrawRequest.objects.filter(is_deleted=False), required=False, allow_null=True,
    )
    document_type = serializers.PrimaryKeyRelatedField(
        queryset=DocumentType.objects.filter(is_active=True), required=False, allow_null=True,
    )
    direction = serializers.ChoiceField(choices=LoanDocument.Direction.choices, required=False)
    visible_to_borrower = serializers.BooleanField(required=False, default=True)

    def validate_request(self, value):
        return validate_same_loan(self, value)

    def validate_draw(self, value):
        if value is not None and value.loan_id != self.context["loan"].pk:
            raise serializers.ValidationError("That draw belongs to another loan.")
        return value

    def validate(self, attrs):
        user = self.context["request"].user
        if user.role == User.Role.BORROWER:
            attrs["direction"] = LoanDocument.Direction.BORROWER_UPLOAD
            attrs["visible_to_borrower"] = True
        elif "direction" not in attrs:
            attrs["direction"] = (
                LoanDocument.Direction.BORROWER_UPLOAD if attrs.get("request")
                else LoanDocument.Direction.LENDER_SHARE
            )
        return attrs


class LoanDocumentUpdateSerializer(serializers.ModelSerializer):
    """Staff edits to a file. A new review_status records the reviewer and moves the file's
    request along (see DocumentService.review); reviewer_note is saved on that request.
    """

    request = serializers.PrimaryKeyRelatedField(
        queryset=DocumentRequest.objects.filter(is_deleted=False), required=False, allow_null=True,
    )
    reviewer_note = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = LoanDocument
        fields = ["name", "request", "document_type", "visible_to_borrower", "review_status", "reviewer_note"]

    def validate_request(self, value):
        return validate_same_loan(self, value)

    def update(self, instance, validated_data):
        review_status = validated_data.pop("review_status", None)
        note = validated_data.pop("reviewer_note", None)
        instance = super().update(instance, validated_data)
        if review_status is not None and (review_status != instance.review_status or note is not None):
            DocumentService.review(instance, review_status, self.context["request"].user, note)
        return instance

    def to_representation(self, instance):
        return LoanDocumentSerializer(instance, context=self.context).data


class DocumentRequestSerializer(serializers.ModelSerializer):
    """A document request with its files. Expects documents prefetched into active_documents."""

    document_type_name = serializers.CharField(source="document_type.name", read_only=True, default=None)
    requested_by = DocumentUserSerializer(read_only=True)
    reviewed_by = DocumentUserSerializer(read_only=True)
    form_document = serializers.SerializerMethodField()
    documents = serializers.SerializerMethodField()

    class Meta:
        model = DocumentRequest
        fields = [
            "id", "loan", "document_type", "document_type_name", "name", "description", "kind",
            "form_document", "source", "message", "status", "reviewer_note", "requested_by",
            "requested_at", "due_date", "reviewed_by", "reviewed_at", "updated_at", "documents",
        ]
        read_only_fields = fields

    def get_form_document(self, obj):
        if obj.form_document is None or obj.form_document.is_deleted:
            return None
        return LoanDocumentSerializer(obj.form_document, context={**self.context, "loan_id": obj.loan_id}).data

    def get_documents(self, obj):
        return LoanDocumentSerializer(obj.active_documents, many=True, context=self.context).data


class DocumentRequestCreateSerializer(serializers.Serializer):
    """POST body for a new request: a document_type (name, description and kind default to its
    own), or a name. form_file (multipart, fill_form only) shares this loan's own blank form
    instead of the type's template.
    """

    document_type = serializers.PrimaryKeyRelatedField(
        queryset=DocumentType.objects.filter(is_active=True), required=False, allow_null=True,
    )
    name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)
    kind = serializers.ChoiceField(choices=DocumentType.Kind.choices, required=False)
    due_date = serializers.DateField(required=False, allow_null=True)
    form_file = serializers.FileField(required=False, validators=[validate_upload])

    def validate(self, attrs):
        document_type = attrs.get("document_type")
        if not attrs.get("name"):
            if document_type is None:
                raise serializers.ValidationError({"name": "Send a name or a document_type."})
            attrs["name"] = document_type.name
        if "description" not in attrs and document_type is not None:
            attrs["description"] = document_type.description
        attrs.setdefault("kind", document_type.kind if document_type else DocumentType.Kind.UPLOAD)
        if "form_file" in attrs and attrs["kind"] != DocumentType.Kind.FILL_FORM:
            raise serializers.ValidationError({"form_file": "Only fill_form requests share a form."})
        return attrs


class DocumentRequestUpdateSerializer(serializers.ModelSerializer):
    """Staff edits to a request; the view records the reviewer when the status is decided."""

    class Meta:
        model = DocumentRequest
        fields = ["name", "description", "due_date", "status", "reviewer_note"]

    def to_representation(self, instance):
        return DocumentRequestSerializer(instance, context=self.context).data


class DrawStatusChangeSerializer(serializers.ModelSerializer):
    changed_by = DocumentUserSerializer(read_only=True)

    class Meta:
        model = DrawRequestStatusChange
        fields = ["id", "from_status", "to_status", "amount_approved", "changed_by", "changed_at", "note"]
        read_only_fields = fields


class DrawRequestSerializer(serializers.ModelSerializer):
    """A draw with its files and status history. Expects files prefetched into active_files.

    allowed_statuses lists what the requesting user may move it to (see DrawService).
    """

    submitted_by = DocumentUserSerializer(read_only=True)
    files = serializers.SerializerMethodField()
    status_changes = DrawStatusChangeSerializer(many=True, read_only=True)
    allowed_statuses = serializers.SerializerMethodField()

    class Meta:
        model = DrawRequest
        fields = [
            "id", "loan", "number", "status", "amount_requested", "amount_approved", "borrower_comment",
            "staff_note", "inspector", "inspection_date", "funding_reference", "submitted_by",
            "submitted_at", "message", "updated_at", "allowed_statuses", "files", "status_changes",
        ]
        read_only_fields = fields

    def get_files(self, obj):
        return LoanDocumentSerializer(obj.active_files, many=True, context=self.context).data

    def get_allowed_statuses(self, obj):
        return DrawService.allowed_transitions(obj, self.context["request"].user)


class DrawRequestCreateSerializer(serializers.Serializer):
    """POST body for a new draw; files are uploaded afterwards to the loan's documents with draw=<id>."""

    amount_requested = serializers.DecimalField(max_digits=14, decimal_places=2)
    borrower_comment = serializers.CharField(required=False, allow_blank=True, default="")


class DrawRequestUpdateSerializer(serializers.ModelSerializer):
    """Edits to a draw's details; status changes go through DrawStatusSerializer.

    Borrowers may only change amount_requested and borrower_comment, and only while the draw
    is submitted or needs info. Staff may change everything here.
    """

    BORROWER_FIELDS = {"amount_requested", "borrower_comment"}

    class Meta:
        model = DrawRequest
        fields = [
            "amount_requested", "amount_approved", "borrower_comment", "staff_note", "inspector",
            "inspection_date", "funding_reference",
        ]

    def validate(self, attrs):
        user = self.context["request"].user
        if user.role == User.Role.BORROWER:
            if set(attrs) - self.BORROWER_FIELDS:
                raise serializers.ValidationError("You can only change the amount and your comment.")
            if self.instance.status not in DrawService.BORROWER_EDITABLE:
                raise serializers.ValidationError("This draw is being reviewed and can't be changed now.")
        return attrs

    def update(self, instance, validated_data):
        return DrawService.update(instance, **validated_data)

    def to_representation(self, instance):
        return DrawRequestSerializer(instance, context=self.context).data


class DrawStatusSerializer(serializers.Serializer):
    """POST body to move a draw: status, and optionally a note (the reason, logged and posted in
    chat), amount_approved (staff, needed to approve) and funding_reference (staff, on funding).
    """

    status = serializers.ChoiceField(choices=DrawRequest.Status.choices)
    note = serializers.CharField(required=False, allow_blank=True, default="")
    amount_approved = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    funding_reference = serializers.CharField(max_length=100, required=False, allow_blank=True)

    def validate(self, attrs):
        user = self.context["request"].user
        if user.role == User.Role.BORROWER and ({"amount_approved", "funding_reference"} & set(attrs)):
            raise serializers.ValidationError("Only staff can set the approved amount or funding reference.")
        return attrs


class LoanNoteSerializer(serializers.ModelSerializer):
    """An internal loan note. can_edit: the requesting user wrote it or is an admin."""

    author = DocumentUserSerializer(read_only=True)
    can_edit = serializers.SerializerMethodField()

    class Meta:
        model = LoanNote
        fields = ["id", "loan", "body", "is_pinned", "author", "created_at", "updated_at", "can_edit"]
        read_only_fields = ["id", "loan", "author", "created_at", "updated_at", "can_edit"]

    def validate_body(self, value):
        if not value.strip():
            raise serializers.ValidationError("A note can't be empty.")
        return value

    def get_can_edit(self, obj):
        user = self.context["request"].user
        return user.is_app_admin or obj.author_id == user.pk
