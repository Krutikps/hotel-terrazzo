from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone

from .permissions import ALL_PERMISSIONS


class Role(models.Model):
    """Role with a JSON list of permission codes ('*' = everything)."""

    code = models.SlugField(max_length=30, unique=True)
    name = models.CharField(max_length=60)
    permissions = models.JSONField(default=list, blank=True)
    is_system = models.BooleanField(default=False)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.name

    def allows(self, code):
        perms = self.permissions or []
        return "*" in perms or code in perms


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create(self, email, password, **extra):
        if not email:
            raise ValueError("Email is required")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        return self._create(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        user = self._create(email, password, **extra)
        if not user.role_id:
            user.role = Role.objects.filter(code="super_admin").first()
            user.save(update_fields=["role"])
        return user


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=80)
    last_name = models.CharField(max_length=80, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    role = models.ForeignKey(Role, null=True, blank=True, on_delete=models.PROTECT, related_name="users")
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name"]
    objects = UserManager()

    class Meta:
        ordering = ["first_name", "last_name"]

    @property
    def name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def __str__(self):
        return f"{self.name} <{self.email}>"

    def can(self, code):
        """Permission check used by every staff view and API."""
        if not self.is_active:
            return False
        if self.is_superuser:
            return True
        return bool(self.role and self.role.allows(code))

    @property
    def perm_codes(self):
        if self.is_superuser or (self.role and "*" in (self.role.permissions or [])):
            return list(ALL_PERMISSIONS)
        return list(self.role.permissions) if self.role else []
