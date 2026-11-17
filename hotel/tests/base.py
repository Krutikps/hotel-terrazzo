from datetime import timedelta
from decimal import Decimal

from django.test import override_settings
from django.utils import timezone

from accounts.models import Role, User
from accounts.permissions import ensure_default_roles
from hotel.models import (Coupon, DailyRate, Guest, Hotel, PricingRule, RatePlan, Room, RoomType, Service, TaxRule)

# The test runner forces DEBUG off, so the sandbox gateway must be explicitly allowed (it is refused in production).
NO_LIMITS = override_settings(RATE_LIMITS={}, PAYMENT_PROVIDER="mock", ALLOW_MOCK_PAYMENTS=True)


def d(n):
    return timezone.localdate() + timedelta(days=n)


def make_hotel():
    ensure_default_roles()
    hotel = Hotel.objects.create(name="Test Hotel", city="Bengaluru", booking_prefix="TST", hold_minutes=15, payment_provider="mock",
                                 email="front@test.example", check_in_time="14:00", check_out_time="11:00")
    hotel.refresh_from_db()
    TaxRule.objects.create(hotel=hotel, name="GST12", rate=12, min_amount=0, max_amount=7500, applies_to="ROOM")
    TaxRule.objects.create(hotel=hotel, name="GST18", rate=18, min_amount=7500, applies_to="ROOM")
    TaxRule.objects.create(hotel=hotel, name="GSTF", rate=5, applies_to="MEAL")
    TaxRule.objects.create(hotel=hotel, name="GSTS", rate=18, applies_to="SERVICE")
    return hotel


def make_room_type(hotel, name="Deluxe Room", rooms=("101", "102", "103"), rate="5000", **kw):
    opts = dict(max_adults=3, max_children=1, max_occupancy=3, base_occupancy=2, extra_adult_charge=Decimal("1000"), child_charge=Decimal("500"))
    opts.update(kw)
    rt = RoomType.objects.create(hotel=hotel, name=name, base_rate=Decimal(rate), **opts)
    for n in rooms:
        Room.objects.create(room_number=n, room_type=rt, floor=int(n[0]), status=Room.CLEAN)
    return rt


def make_plan(rt, name="Room Only", **kw):
    return RatePlan.objects.create(room_type=rt, name=name, **kw)


def make_user(role_code, email=None):
    role = Role.objects.get(code=role_code)
    return User.objects.create_user(email or f"{role_code}@test.example", "Passw0rd!x", first_name=role.name.split()[0], role=role,
                                    is_superuser=(role_code == "super_admin"))


GUEST = {"first_name": "Asha", "last_name": "Rao", "email": "asha@example.com", "phone": "+91 99999 11111", "city": "Pune", "country": "India"}
