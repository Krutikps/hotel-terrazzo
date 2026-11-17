"""Relational schema for the HMS. The database is the single source of truth."""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

MONEY = dict(max_digits=12, decimal_places=2, default=Decimal("0.00"))


def money(**kw):
    opts = dict(MONEY)
    opts.update(kw)
    return models.DecimalField(**opts)


# ---------------------------------------------------------------------------
# Hotel & configuration
# ---------------------------------------------------------------------------
class Hotel(models.Model):
    name = models.CharField(max_length=150)
    tagline = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=80, blank=True)
    state = models.CharField(max_length=80, blank=True)
    country = models.CharField(max_length=80, default="India")
    postal_code = models.CharField(max_length=20, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    website = models.URLField(blank=True)
    gstin = models.CharField("Tax / GST number", max_length=30, blank=True)
    timezone = models.CharField(max_length=60, default="Asia/Kolkata")
    currency = models.CharField(max_length=3, default="INR")
    currency_symbol = models.CharField(max_length=5, default="₹")
    check_in_time = models.TimeField(default="14:00")
    check_out_time = models.TimeField(default="11:00")
    logo = models.FileField(upload_to="hotel/", blank=True)
    logo_url = models.CharField(max_length=300, blank=True)
    hero_image_url = models.CharField(max_length=300, blank=True)
    booking_prefix = models.CharField(max_length=6, default="HTL")
    # booking / cancellation settings
    hold_minutes = models.PositiveIntegerField("Payment hold (minutes)", default=15)
    online_payment_percent = models.PositiveIntegerField("Online payment % at booking", default=100)
    allow_guest_cancellation = models.BooleanField(default=True)
    max_rooms_per_booking = models.PositiveIntegerField(default=5)
    max_advance_days = models.PositiveIntegerField("Max days booking can be made in advance", default=540)
    require_id_online = models.BooleanField("Require ID details online", default=False)
    # payment / notification config (secrets live in env vars, never here)
    payment_provider = models.CharField(max_length=20, default="mock")
    notify_email = models.BooleanField(default=True)
    notify_sms = models.BooleanField(default=False)
    notify_whatsapp = models.BooleanField(default=False)
    admin_notification_email = models.EmailField(blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    def __str__(self):
        return self.name

    @property
    def logo_src(self):
        if self.logo:
            return self.logo.url
        return self.logo_url

    @classmethod
    def get(cls):
        return cls.objects.first()

    def money(self, amount):
        return f"{self.currency_symbol}{Decimal(amount):,.2f}"


class TaxRule(models.Model):
    """Tax applied per room-night (ROOM) or per meal / service charge. Slabs: min < amount <= max."""

    ROOM, MEAL, SERVICE = "ROOM", "MEAL", "SERVICE"
    APPLIES = [(ROOM, "Room tariff (per room per night)"), (MEAL, "Meals"), (SERVICE, "Services & extras")]
    hotel = models.ForeignKey(Hotel, on_delete=models.CASCADE, related_name="tax_rules")
    name = models.CharField(max_length=60)
    rate = models.DecimalField(max_digits=5, decimal_places=2, help_text="Percent")
    applies_to = models.CharField(max_length=10, choices=APPLIES, default=ROOM)
    min_amount = money(help_text="Slab lower bound (exclusive), per room per night for ROOM rules")
    max_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text="Slab upper bound (inclusive). Empty = no limit")
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["applies_to", "min_amount"]

    def __str__(self):
        return f"{self.name} {self.rate}%"


class Amenity(models.Model):
    name = models.CharField(max_length=80, unique=True)
    icon = models.CharField(max_length=40, blank=True, help_text="Emoji or short icon text")

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "amenities"

    def __str__(self):
        return self.name


class RoomType(models.Model):
    hotel = models.ForeignKey(Hotel, on_delete=models.CASCADE, related_name="room_types")
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=110, unique=True, blank=True)
    short_description = models.CharField(max_length=220, blank=True)
    description = models.TextField(blank=True)
    size_sqft = models.PositiveIntegerField("Size (sq ft)", default=250)
    bed_type = models.CharField(max_length=80, default="King bed")
    max_adults = models.PositiveSmallIntegerField(default=2)
    max_children = models.PositiveSmallIntegerField(default=1)
    max_occupancy = models.PositiveSmallIntegerField(default=3)
    base_occupancy = models.PositiveSmallIntegerField("Adults included in rate", default=2)
    base_rate = money(validators=[MinValueValidator(0)], help_text="Default nightly rate when no date price is set")
    extra_adult_charge = money(help_text="Per extra adult, per night")
    child_charge = money(help_text="Per child, per night")
    amenities = models.ManyToManyField(Amenity, blank=True, related_name="room_types")
    active = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["display_order", "name"]

    def __str__(self):
        return self.name

    def clean(self):
        if self.max_occupancy < 1:
            raise ValidationError("Max occupancy must be at least 1.")
        if self.max_adults > self.max_occupancy:
            raise ValidationError("Max adults cannot exceed max occupancy.")
        if self.base_occupancy > self.max_adults:
            raise ValidationError("Base occupancy cannot exceed max adults.")

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or "room"
            slug, i = base, 2
            while RoomType.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug, i = f"{base}-{i}", i + 1
            self.slug = slug
        super().save(*args, **kwargs)

    @property
    def primary_image(self):
        imgs = list(self.images.all())
        for i in imgs:
            if i.is_primary:
                return i
        return imgs[0] if imgs else None


class RoomImage(models.Model):
    room_type = models.ForeignKey(RoomType, on_delete=models.CASCADE, related_name="images")
    image = models.FileField(upload_to="rooms/", blank=True)
    image_url = models.CharField(max_length=300, blank=True)
    caption = models.CharField(max_length=120, blank=True)
    display_order = models.PositiveIntegerField(default=0)
    is_primary = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_primary", "display_order", "id"]

    @property
    def src(self):
        return self.image.url if self.image else self.image_url


class Room(models.Model):
    AVAILABLE, RESERVED, OCCUPIED = "AVAILABLE", "RESERVED", "OCCUPIED"
    DIRTY, CLEANING, CLEAN, INSPECTED = "DIRTY", "CLEANING", "CLEAN", "INSPECTED"
    BLOCKED, MAINTENANCE, OUT_OF_ORDER = "BLOCKED", "MAINTENANCE", "OUT_OF_ORDER"
    STATUS = [
        (AVAILABLE, "Available"), (RESERVED, "Reserved"), (OCCUPIED, "Occupied"), (DIRTY, "Dirty"),
        (CLEANING, "Cleaning"), (CLEAN, "Clean"), (INSPECTED, "Inspected"), (BLOCKED, "Blocked"),
        (MAINTENANCE, "Maintenance"), (OUT_OF_ORDER, "Out of Order"),
    ]
    # Rooms in these states are excluded from sale indefinitely (until status is changed).
    UNSELLABLE = (BLOCKED, MAINTENANCE, OUT_OF_ORDER)
    # Rooms in these states cannot receive a guest right now.
    NOT_READY = (DIRTY, CLEANING, OCCUPIED, BLOCKED, MAINTENANCE, OUT_OF_ORDER)
    HOUSEKEEPING = (DIRTY, CLEANING, CLEAN, INSPECTED)

    room_number = models.CharField(max_length=20, unique=True)
    room_type = models.ForeignKey(RoomType, on_delete=models.PROTECT, related_name="rooms")
    floor = models.SmallIntegerField(default=1)
    status = models.CharField(max_length=15, choices=STATUS, default=CLEAN, db_index=True)
    active = models.BooleanField(default=True)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["room_number"]
        indexes = [models.Index(fields=["room_type", "active", "status"])]

    def __str__(self):
        return f"Room {self.room_number}"

    @property
    def sellable(self):
        return self.active and self.status not in self.UNSELLABLE


# ---------------------------------------------------------------------------
# Rates & pricing
# ---------------------------------------------------------------------------
class RatePlan(models.Model):
    ROOM_ONLY, BREAKFAST, HALF_BOARD, FULL_BOARD = "ROOM_ONLY", "BREAKFAST", "HALF_BOARD", "FULL_BOARD"
    MEALS = [(ROOM_ONLY, "Room only"), (BREAKFAST, "Breakfast included"), (HALF_BOARD, "Half board"), (FULL_BOARD, "Full board")]
    ADJ_NONE, ADJ_PERCENT, ADJ_FIXED = "NONE", "PERCENT", "FIXED"
    ADJ = [(ADJ_NONE, "None"), (ADJ_PERCENT, "Percent of room rate"), (ADJ_FIXED, "Fixed amount per night")]

    room_type = models.ForeignKey(RoomType, on_delete=models.CASCADE, related_name="rate_plans")
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=300, blank=True)
    meal_plan = models.CharField(max_length=12, choices=MEALS, default=ROOM_ONLY)
    meal_adult_price = money(help_text="Meal charge per adult per night")
    meal_child_price = money(help_text="Meal charge per child per night")
    adjustment_type = models.CharField(max_length=8, choices=ADJ, default=ADJ_NONE)
    adjustment_value = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"), help_text="Negative = discount, e.g. -10 for 10% off")
    refundable = models.BooleanField(default=True)
    free_cancel_hours = models.PositiveIntegerField(default=24, help_text="Free cancellation until N hours before check-in (refundable plans)")
    cancel_penalty_percent = models.PositiveIntegerField(default=100, help_text="% of total charged for late cancellation / non-refundable")
    min_stay = models.PositiveSmallIntegerField(default=1)
    max_stay = models.PositiveSmallIntegerField(null=True, blank=True)
    corporate = models.BooleanField(default=False, help_text="Only offered to staff-created (corporate) bookings")
    active = models.BooleanField(default=True)
    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["room_type__display_order", "display_order", "id"]

    def __str__(self):
        return f"{self.room_type.name} — {self.name}"

    @property
    def cancellation_text(self):
        if not self.refundable:
            return "Non-refundable. No refund on cancellation or no-show."
        if self.free_cancel_hours:
            return (f"Free cancellation until {self.free_cancel_hours} hours before check-in. "
                    f"After that, {self.cancel_penalty_percent}% of the booking total is charged.")
        return f"{self.cancel_penalty_percent}% of the booking total is charged on cancellation."


class DailyRate(models.Model):
    """Explicit per-date price & restrictions. rate_plan NULL = applies to every plan of the room type."""

    room_type = models.ForeignKey(RoomType, on_delete=models.CASCADE, related_name="daily_rates")
    rate_plan = models.ForeignKey(RatePlan, null=True, blank=True, on_delete=models.CASCADE, related_name="daily_rates")
    date = models.DateField(db_index=True)
    price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    min_stay = models.PositiveSmallIntegerField(null=True, blank=True)
    closed_to_arrival = models.BooleanField(default=False)
    closed_to_departure = models.BooleanField(default=False)
    stop_sell = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["room_type", "rate_plan", "date"], name="uniq_daily_rate")]
        indexes = [models.Index(fields=["room_type", "date"])]


class PricingRule(models.Model):
    """Weekend / seasonal / holiday / event adjustments applied when no explicit DailyRate price exists."""

    WEEKEND, SEASONAL, HOLIDAY, EVENT = "WEEKEND", "SEASONAL", "HOLIDAY", "EVENT"
    KINDS = [(WEEKEND, "Weekend"), (SEASONAL, "Seasonal"), (HOLIDAY, "Holiday"), (EVENT, "Special event")]
    PERCENT, FIXED = "PERCENT", "FIXED"
    ADJ = [(PERCENT, "Percent"), (FIXED, "Fixed amount per night")]

    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=10, choices=KINDS, default=SEASONAL)
    room_type = models.ForeignKey(RoomType, null=True, blank=True, on_delete=models.CASCADE, related_name="pricing_rules", help_text="Empty = all room types")
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    weekdays = models.CharField(max_length=20, blank=True, help_text="Comma separated 0=Mon … 6=Sun (e.g. 4,5 for Fri/Sat). Empty = every day")
    adjustment_type = models.CharField(max_length=8, choices=ADJ, default=PERCENT)
    value = models.DecimalField(max_digits=10, decimal_places=2, help_text="Negative to reduce price")
    priority = models.PositiveSmallIntegerField(default=10)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["priority", "id"]

    def __str__(self):
        return self.name

    def weekday_set(self):
        return {int(x) for x in self.weekdays.replace(" ", "").split(",") if x.strip().isdigit()}

    def applies_on(self, d, room_type_id):
        if not self.active:
            return False
        if self.room_type_id and self.room_type_id != room_type_id:
            return False
        if self.start_date and d < self.start_date:
            return False
        if self.end_date and d > self.end_date:
            return False
        wd = self.weekday_set()
        return not wd or d.weekday() in wd


class Service(models.Model):
    """Optional chargeable add-on (airport pickup, extra bed …)."""

    PER_STAY, PER_NIGHT, PER_PERSON_NIGHT = "PER_STAY", "PER_NIGHT", "PER_PERSON_NIGHT"
    CHARGE = [(PER_STAY, "Per stay"), (PER_NIGHT, "Per night"), (PER_PERSON_NIGHT, "Per person per night")]
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=200, blank=True)
    price = money()
    charge_type = models.CharField(max_length=20, choices=CHARGE, default=PER_STAY)
    taxable = models.BooleanField(default=True)
    bookable_online = models.BooleanField(default=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Coupon(models.Model):
    PERCENT, FIXED = "PERCENT", "FIXED"
    TYPES = [(PERCENT, "Percent"), (FIXED, "Fixed amount")]
    code = models.CharField(max_length=30, unique=True)
    title = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    discount_type = models.CharField(max_length=8, choices=TYPES, default=PERCENT)
    value = models.DecimalField(max_digits=10, decimal_places=2)
    max_discount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    min_amount = money(help_text="Minimum room subtotal")
    min_nights = models.PositiveSmallIntegerField(default=1)
    room_types = models.ManyToManyField(RoomType, blank=True, help_text="Empty = all")
    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)
    usage_limit = models.PositiveIntegerField(null=True, blank=True)
    used_count = models.PositiveIntegerField(default=0)
    show_on_offers = models.BooleanField(default=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return self.code


# ---------------------------------------------------------------------------
# Guests & reservations
# ---------------------------------------------------------------------------
class Guest(models.Model):
    first_name = models.CharField(max_length=80)
    last_name = models.CharField(max_length=80, blank=True)
    email = models.EmailField(blank=True, db_index=True)
    phone = models.CharField(max_length=30, blank=True, db_index=True)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=80, blank=True)
    state = models.CharField(max_length=80, blank=True)
    country = models.CharField(max_length=80, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    id_type = models.CharField(max_length=40, blank=True)
    id_number = models.CharField(max_length=60, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    preferences = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["first_name", "last_name"]

    @property
    def name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def __str__(self):
        return self.name


class Reservation(models.Model):
    PENDING_PAYMENT, CONFIRMED, CHECKED_IN, CHECKED_OUT = "PENDING_PAYMENT", "CONFIRMED", "CHECKED_IN", "CHECKED_OUT"
    CANCELLED, NO_SHOW, RELEASED = "CANCELLED", "NO_SHOW", "RELEASED"
    STATUS = [
        (PENDING_PAYMENT, "Pending payment"), (CONFIRMED, "Confirmed"), (CHECKED_IN, "Checked in"),
        (CHECKED_OUT, "Checked out"), (CANCELLED, "Cancelled"), (NO_SHOW, "No show"), (RELEASED, "Released"),
    ]
    # Statuses that consume inventory (pending ones only while their hold is alive).
    HOLDING = (CONFIRMED, CHECKED_IN)

    P_PENDING, P_AUTHORIZED, P_PAID, P_FAILED = "PENDING", "AUTHORIZED", "PAID", "FAILED"
    P_PARTIAL, P_REFUNDED, P_PART_REFUNDED = "PARTIALLY_PAID", "REFUNDED", "PARTIALLY_REFUNDED"
    PAYMENT_STATUS = [
        (P_PENDING, "Pending"), (P_AUTHORIZED, "Authorized"), (P_PAID, "Paid"), (P_FAILED, "Failed"),
        (P_PARTIAL, "Partially paid"), (P_REFUNDED, "Refunded"), (P_PART_REFUNDED, "Partially refunded"),
    ]
    SOURCES = [("WEBSITE", "Website"), ("RECEPTION", "Reception"), ("WALK_IN", "Walk-in"), ("PHONE", "Phone"),
               ("EMAIL", "Email"), ("CORPORATE", "Corporate"), ("OTHER", "Other")]
    PURPOSES = ["Business", "Leisure", "Family", "Wedding/Event", "Medical", "Other"]

    booking_number = models.CharField(max_length=30, unique=True, null=True, blank=True)
    access_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    hotel = models.ForeignKey(Hotel, on_delete=models.PROTECT, related_name="reservations")
    guest = models.ForeignKey(Guest, on_delete=models.PROTECT, related_name="reservations")
    source = models.CharField(max_length=12, choices=SOURCES, default="WEBSITE", db_index=True)
    check_in = models.DateField(db_index=True)
    check_out = models.DateField(db_index=True)
    nights = models.PositiveSmallIntegerField(default=1)
    adults = models.PositiveSmallIntegerField(default=1)
    children = models.PositiveSmallIntegerField(default=0)
    room_count = models.PositiveSmallIntegerField(default=1)
    room_type = models.ForeignKey(RoomType, on_delete=models.PROTECT, related_name="reservations")
    rate_plan = models.ForeignKey(RatePlan, on_delete=models.PROTECT, related_name="reservations")
    purpose = models.CharField(max_length=80, blank=True)
    arrival_time = models.CharField(max_length=40, blank=True)
    special_requests = models.TextField(blank=True)
    internal_notes = models.TextField(blank=True)
    coupon = models.ForeignKey(Coupon, null=True, blank=True, on_delete=models.SET_NULL)
    # money (denormalised from ReservationLine rows & payments by services.reservations.recompute)
    room_charges = money()
    extra_guest_charges = money()
    service_charges = money()
    discount_total = money()
    tax_total = money()
    total = money()
    amount_paid = money()
    status = models.CharField(max_length=16, choices=STATUS, default=PENDING_PAYMENT, db_index=True)
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS, default=P_PENDING, db_index=True)
    cancellation_policy = models.TextField(blank=True)
    refundable = models.BooleanField(default=True)
    free_cancel_until = models.DateTimeField(null=True, blank=True)
    cancel_penalty_percent = models.PositiveIntegerField(default=100)
    cancellation_reason = models.CharField(max_length=255, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    hold_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    pricing_snapshot = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    checked_out_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "check_in"]),
            models.Index(fields=["status", "check_out"]),
            models.Index(fields=["room_type", "check_in", "check_out"]),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(check_out__gt=models.F("check_in")), name="res_checkout_after_checkin"),
        ]

    def __str__(self):
        return self.booking_number or f"Hold {self.access_token}"

    @property
    def balance(self):
        return max(self.total - self.amount_paid, Decimal("0.00"))

    @property
    def reference(self):
        return self.booking_number or "Pending"

    @property
    def is_hold_alive(self):
        return self.status == self.PENDING_PAYMENT and self.hold_expires_at and self.hold_expires_at > timezone.now()

    @property
    def assigned_room_numbers(self):
        return ", ".join(r.room.room_number for r in self.rooms.all() if r.room_id)

    @property
    def can_cancel(self):
        return self.status in (self.CONFIRMED,)


class ReservationRoom(models.Model):
    """One row per room booked. room_type holds inventory; room is the physical room once assigned."""

    reservation = models.ForeignKey(Reservation, on_delete=models.CASCADE, related_name="rooms")
    room_type = models.ForeignKey(RoomType, on_delete=models.PROTECT, related_name="reservation_rooms")
    room = models.ForeignKey(Room, null=True, blank=True, on_delete=models.PROTECT, related_name="reservation_rooms")
    adults = models.PositiveSmallIntegerField(default=1)
    children = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["room_type", "reservation"])]


class ReservationLine(models.Model):
    """Itemised charge. is_auto lines are regenerated by the pricing engine; manual lines persist."""

    ROOM, EXTRA_GUEST, MEAL, SERVICE, MINIBAR, LATE_CHECKOUT, OTHER, DISCOUNT, TAX = (
        "ROOM", "EXTRA_GUEST", "MEAL", "SERVICE", "MINIBAR", "LATE_CHECKOUT", "OTHER", "DISCOUNT", "TAX")
    CATS = [(ROOM, "Room"), (EXTRA_GUEST, "Extra guest"), (MEAL, "Meals"), (SERVICE, "Service"), (MINIBAR, "Minibar"),
            (LATE_CHECKOUT, "Late checkout"), (OTHER, "Other"), (DISCOUNT, "Discount"), (TAX, "Tax")]
    reservation = models.ForeignKey(Reservation, on_delete=models.CASCADE, related_name="lines")
    category = models.CharField(max_length=14, choices=CATS)
    description = models.CharField(max_length=200)
    quantity = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("1"))
    unit_price = money()
    amount = money(help_text="Signed: discounts are negative")
    is_auto = models.BooleanField(default=True)
    taxable = models.BooleanField(default=False, help_text="Manual lines only: tax is added on top using the SERVICE rule")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


class ReservationRequest(models.Model):
    TYPES = ["Early check-in", "Late checkout", "Airport pickup", "Extra bed", "Baby cot", "High floor",
             "Quiet room", "Anniversary decoration", "Birthday setup", "Other"]
    reservation = models.ForeignKey(Reservation, on_delete=models.CASCADE, related_name="requests")
    request_type = models.CharField(max_length=60)
    notes = models.CharField(max_length=300, blank=True)
    fulfilled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


class Payment(models.Model):
    PENDING, AUTHORIZED, PAID, FAILED, REFUNDED = "PENDING", "AUTHORIZED", "PAID", "FAILED", "REFUNDED"
    STATUS = [(PENDING, "Pending"), (AUTHORIZED, "Authorized"), (PAID, "Paid"), (FAILED, "Failed"), (REFUNDED, "Refunded")]
    PAYMENT, REFUND = "PAYMENT", "REFUND"
    METHODS = [("ONLINE", "Online"), ("CASH", "Cash"), ("CARD", "Card"), ("UPI", "UPI"), ("BANK_TRANSFER", "Bank transfer"), ("OTHER", "Other")]
    reservation = models.ForeignKey(Reservation, on_delete=models.PROTECT, related_name="payments")
    kind = models.CharField(max_length=8, default=PAYMENT)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    method = models.CharField(max_length=15, choices=METHODS, default="ONLINE")
    provider = models.CharField(max_length=20, blank=True)
    provider_order_id = models.CharField(max_length=80, blank=True, db_index=True)
    provider_payment_id = models.CharField(max_length=80, blank=True, db_index=True)
    transaction_id = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=12, choices=STATUS, default=PENDING, db_index=True)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="refunds")
    note = models.CharField(max_length=255, blank=True)
    raw_response = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)  # never contains card data
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["provider", "provider_payment_id"], condition=~models.Q(provider_payment_id=""),
                                    name="uniq_provider_payment"),
        ]

    @property
    def refunded_amount(self):
        return sum((r.amount for r in self.refunds.all() if r.status == self.PAID), Decimal("0"))


class RoomBlock(models.Model):
    REASONS = [("MAINTENANCE", "Maintenance"), ("RENOVATION", "Renovation"), ("OWNER_USE", "Owner use"),
               ("CLEANING", "Cleaning"), ("EMERGENCY", "Emergency"), ("OTHER", "Other")]
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name="blocks")
    start_date = models.DateField()
    end_date = models.DateField(help_text="Last blocked night (inclusive)")
    reason = models.CharField(max_length=15, choices=REASONS, default="MAINTENANCE")
    notes = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-start_date"]
        indexes = [models.Index(fields=["room", "start_date", "end_date"])]
        constraints = [models.CheckConstraint(condition=models.Q(end_date__gte=models.F("start_date")), name="block_end_after_start")]

    @property
    def active(self):
        return self.released_at is None


class MaintenanceTicket(models.Model):
    PRIORITIES = [("LOW", "Low"), ("MEDIUM", "Medium"), ("HIGH", "High"), ("URGENT", "Urgent")]
    STATUSES = [("OPEN", "Open"), ("IN_PROGRESS", "In progress"), ("RESOLVED", "Resolved"), ("CLOSED", "Closed")]
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name="tickets")
    problem = models.CharField(max_length=200)
    priority = models.CharField(max_length=8, choices=PRIORITIES, default="MEDIUM")
    status = models.CharField(max_length=12, choices=STATUSES, default="OPEN", db_index=True)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="tickets")
    reported_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    notes = models.TextField(blank=True)
    blocks_room = models.BooleanField(default=True, help_text="Take the room out of sale while open")
    previous_room_status = models.CharField(max_length=15, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_open(self):
        return self.status in ("OPEN", "IN_PROGRESS")


class HousekeepingLog(models.Model):
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name="hk_logs")
    from_status = models.CharField(max_length=15)
    to_status = models.CharField(max_length=15)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Invoice(models.Model):
    reservation = models.ForeignKey(Reservation, on_delete=models.PROTECT, related_name="invoices")
    number = models.CharField(max_length=30, unique=True)
    issued_at = models.DateTimeField(default=timezone.now)
    data = models.JSONField(default=dict, encoder=DjangoJSONEncoder)  # immutable snapshot of what was invoiced
    total = money()
    paid = money()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-issued_at"]


class Counter(models.Model):
    """Gap-free yearly sequences (booking numbers, invoice numbers). Always used inside a transaction."""

    kind = models.CharField(max_length=20)
    year = models.PositiveIntegerField()
    last = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["kind", "year"], name="uniq_counter")]


# ---------------------------------------------------------------------------
# Audit & notifications
# ---------------------------------------------------------------------------
class AuditLog(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_logs")
    actor_label = models.CharField(max_length=120, blank=True, help_text="e.g. 'Guest (website)' or 'System'")
    action = models.CharField(max_length=60, db_index=True)
    entity_type = models.CharField(max_length=40, db_index=True)
    entity_id = models.CharField(max_length=40, db_index=True)
    summary = models.CharField(max_length=255, blank=True)
    old_value = models.JSONField(null=True, blank=True)
    new_value = models.JSONField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class Notification(models.Model):
    GUEST, ADMIN = "GUEST", "ADMIN"
    EMAIL, SMS, WHATSAPP, INAPP = "EMAIL", "SMS", "WHATSAPP", "INAPP"
    SENT, FAILED, SKIPPED, QUEUED = "SENT", "FAILED", "SKIPPED", "QUEUED"
    audience = models.CharField(max_length=6, default=GUEST)
    channel = models.CharField(max_length=10, default=EMAIL)
    event = models.CharField(max_length=40, db_index=True)
    recipient = models.CharField(max_length=200, blank=True)
    subject = models.CharField(max_length=200, blank=True)
    body = models.TextField(blank=True)
    status = models.CharField(max_length=8, default=QUEUED)
    error = models.CharField(max_length=300, blank=True)
    reservation = models.ForeignKey(Reservation, null=True, blank=True, on_delete=models.CASCADE, related_name="notifications")
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class Review(models.Model):
    """Guest testimonial shown on the website (managed in the portal)."""

    name = models.CharField(max_length=80)
    location = models.CharField(max_length=80, blank=True)
    rating = models.PositiveSmallIntegerField(default=5, validators=[MinValueValidator(1)])
    text = models.TextField()
    show = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
