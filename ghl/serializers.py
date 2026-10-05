from rest_framework import serializers

from .models import GHLToken


class GHLConnectionSerializer(serializers.ModelSerializer):
    """A connected GHL account, without its tokens."""

    class Meta:
        model = GHLToken
        fields = ["id", "user_type", "location_id", "company_id", "expires_at", "updated_at"]
