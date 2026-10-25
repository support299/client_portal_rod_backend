from rest_framework import serializers

from .models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()
    loan_name = serializers.CharField(source="loan.opportunity.name", read_only=True, default=None)
    is_read = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = [
            "id", "kind", "title", "body", "loan", "loan_name", "data", "actor", "actor_name",
            "is_read", "read_at", "created_at",
        ]
        read_only_fields = fields

    def get_actor_name(self, obj):
        if obj.actor is None:
            return None
        return obj.actor.get_full_name().strip() or obj.actor.email

    def get_is_read(self, obj):
        return obj.read_at is not None
