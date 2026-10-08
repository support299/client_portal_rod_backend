from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


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
