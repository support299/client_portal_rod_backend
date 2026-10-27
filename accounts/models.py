import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("The email must be set")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", User.Role.ADMIN)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    class Role(models.TextChoices):
        BORROWER = "borrower", "Borrower"
        LOAN_OFFICER = "loan_officer", "Loan Officer"
        PROCESSOR = "processor", "Processor"
        CLOSER = "closer", "Closer"
        DRAW_SPECIALIST = "draw_specialist", "Draw Specialist"
        ACCOUNTING = "accounting", "Accounting"
        ADMIN = "admin", "Admin"

    username = None

    email = models.EmailField(
        unique=True,
    )

    phone = models.CharField(
        max_length=20,
        blank=True,
    )

    role = models.CharField(
        max_length=30,
        choices=Role.choices,
        default=Role.BORROWER,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    def __str__(self):
        return self.email

    @property
    def is_app_admin(self):
        """Admins (admin role or Django staff) see everything; other roles see only their own data."""
        return self.is_staff or self.role == self.Role.ADMIN


class PasswordLink(models.Model):
    """One-time link emailed to a user to set (first time) or reset their password.

    Only the SHA-256 hash of the token is stored; the raw token lives only in the emailed URL.
    """

    class Purpose(models.TextChoices):
        SET = "set", "Set Password"
        RESET = "reset", "Reset Password"

    EXPIRY = {
        Purpose.SET: timedelta(hours=72),
        Purpose.RESET: timedelta(hours=2),
    }

    # Self-service requests within this window of the last link are ignored (no new email).
    RESEND_COOLDOWN = timedelta(minutes=1)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="password_links",
    )

    purpose = models.CharField(
        max_length=10,
        choices=Purpose.choices,
    )

    token_hash = models.CharField(
        max_length=64,
        unique=True,
    )

    sent_to_email = models.EmailField()

    # Execution id returned by the GHL email webhook, for tracing delivery.
    webhook_execution_id = models.CharField(
        max_length=64,
        blank=True,
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="issued_password_links",
    )

    expires_at = models.DateTimeField()

    used_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    revoked_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "purpose"]),
        ]

    def __str__(self):
        return f"{self.get_purpose_display()} link for {self.sent_to_email}"

    @staticmethod
    def hash_token(raw_token):
        return hashlib.sha256(raw_token.encode()).hexdigest()

    @classmethod
    def issue(cls, user, purpose, created_by=None):
        """Revoke the user's previous active links and create a new one. Returns (link, raw_token)."""
        now = timezone.now()
        cls.objects.filter(user=user, used_at__isnull=True, revoked_at__isnull=True).update(revoked_at=now)
        raw_token = secrets.token_urlsafe(32)
        link = cls.objects.create(
            user=user,
            purpose=purpose,
            token_hash=cls.hash_token(raw_token),
            sent_to_email=user.email,
            created_by=created_by,
            expires_at=now + cls.EXPIRY[purpose],
        )
        return link, raw_token

    @classmethod
    def recently_sent(cls, user):
        return cls.objects.filter(user=user, created_at__gte=timezone.now() - cls.RESEND_COOLDOWN).exists()

    @classmethod
    def active(cls):
        return cls.objects.filter(used_at__isnull=True, revoked_at__isnull=True, expires_at__gt=timezone.now())

    @property
    def is_active(self):
        return self.used_at is None and self.revoked_at is None and timezone.now() < self.expires_at
