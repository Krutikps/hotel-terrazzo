"""First-deploy / every-deploy setup, safe to re-run (idempotent). Used by build.sh on Render.

Environment:
  ADMIN_EMAIL, ADMIN_PASSWORD  create or update the super-admin login (password only changed if ADMIN_RESET_PASSWORD=true)
  SEED_DEMO=true               if the database has no hotel yet, load the demo hotel, rooms, rates and sample bookings
                               (demo staff accounts then get ADMIN_PASSWORD, never a published default)
"""
import os

from django.core.management import call_command
from django.core.management.base import BaseCommand

from accounts.models import Role, User
from accounts.permissions import ensure_default_roles
from hotel.models import Hotel


def _flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


class Command(BaseCommand):
    help = "Create default roles, the admin user and (optionally) demo data. Idempotent."

    def handle(self, *a, **o):
        ensure_default_roles()
        email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
        password = os.environ.get("ADMIN_PASSWORD", "")

        # Recover from a seed that crashed half-way (the hotel row is committed first; the logo is applied last).
        # Only when explicitly asked, and only while the seed is still incomplete, so it can never wipe a finished site.
        hotel = Hotel.objects.first()
        if _flag("SEED_DEMO") and _flag("SEED_DEMO_RESET") and hotel and not hotel.logo_url and password:
            self.stdout.write("Incomplete demo seed found - re-seeding from scratch.")
            call_command("seed_demo", reset=True, password=password, stdout=self.stdout)

        if not Hotel.objects.exists():
            if _flag("SEED_DEMO"):
                if not password:
                    self.stderr.write("SEED_DEMO needs ADMIN_PASSWORD (it becomes the demo staff password). Skipping demo data.")
                else:
                    call_command("seed_demo", password=password, stdout=self.stdout)
            else:
                Hotel.objects.create(name="Hotel Terrazzo Suites", booking_prefix="HTS")
                call_command("apply_branding", stdout=self.stdout)  # sets name, address, logo; add rooms/rates in the staff portal

        if email and password:
            role = Role.objects.filter(code="super_admin").first()
            user, created = User.objects.get_or_create(email=email, defaults={"first_name": "Site", "last_name": "Admin", "is_staff": True,
                                                                              "is_superuser": True, "role": role})
            if created or _flag("ADMIN_RESET_PASSWORD"):
                user.set_password(password)
            user.is_staff = user.is_superuser = True
            user.role = role
            user.save()
            self.stdout.write(f"Admin {'created' if created else 'ensured'}: {email}")
        else:
            self.stdout.write("ADMIN_EMAIL / ADMIN_PASSWORD not set; no admin user created.")
