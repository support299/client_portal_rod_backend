from django.db import models


class GHLToken(models.Model):
    """OAuth tokens for one connected GHL location (or agency/company)."""

    USER_TYPE_CHOICES = [("Location", "Location"), ("Company", "Company")]

    location_id = models.CharField(max_length=64, blank=True, db_index=True)
    company_id = models.CharField(max_length=64, blank=True, db_index=True)
    user_type = models.CharField(max_length=16, choices=USER_TYPE_CHOICES)
    access_token = models.TextField()
    refresh_token = models.TextField()
    expires_at = models.DateTimeField()
    scope = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user_type", "location_id", "company_id"],
                name="unique_ghl_token_owner",
            ),
        ]

    def __str__(self):
        return f"{self.user_type} {self.location_id or self.company_id}"


class GHLContact(models.Model):
    """A contact synced from a GHL location."""

    ghl_id = models.CharField(max_length=64, unique=True)
    location_id = models.CharField(max_length=64, db_index=True)
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    contact_name = models.CharField(max_length=255, blank=True)
    email = models.CharField(max_length=255, blank=True, db_index=True)
    phone = models.CharField(max_length=64, blank=True, db_index=True)
    company_name = models.CharField(max_length=255, blank=True)
    type = models.CharField(max_length=64, blank=True)
    source = models.CharField(max_length=255, blank=True)
    assigned_to = models.CharField(max_length=64, blank=True)
    tags = models.JSONField(default=list, blank=True)
    custom_fields = models.JSONField(default=list, blank=True)
    address1 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=255, blank=True)
    postal_code = models.CharField(max_length=32, blank=True)
    country = models.CharField(max_length=64, blank=True)
    dnd = models.BooleanField(default=False)
    date_added = models.DateTimeField(null=True, blank=True)
    date_updated = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.contact_name or self.email or self.ghl_id
