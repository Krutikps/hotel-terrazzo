"""Centralised pricing engine. The frontend never computes a price — it only displays quote() output.

Per night, per room:
  room rate   = explicit DailyRate (plan row > room-type row) else RoomType.base_rate + PricingRules,
                then the rate plan's adjustment (explicit plan rows are final)
  extra guest = extra adults beyond base occupancy * extra_adult_charge + children * child_charge
Then meals (per adult / child per night), selected services, coupon discount (on room + extra-guest
charges) and taxes (slab-based on the discounted per-room-night amount; meals & services have own rules).
"""
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from hotel.models import Coupon, DailyRate, Hotel, PricingRule, RatePlan, Service, TaxRule

ZERO = Decimal("0")


def q2(x):
    return Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class PricingError(Exception):
    def __init__(self, message, field=None):
        super().__init__(message)
        self.message = message
        self.field = field


class CouponError(PricingError):
    pass


# --------------------------------------------------------------------------- validation
def validate_dates(check_in, check_out, staff=False):
    today = timezone.localdate()
    if not check_in or not check_out:
        raise PricingError("Please select check-in and check-out dates.")
    if check_out <= check_in:
        raise PricingError("Check-out date must be after check-in date.")
    if check_in < today and not staff:
        raise PricingError("Check-in date cannot be in the past.")
    if (check_out - check_in).days > 90:
        raise PricingError("Stays longer than 90 nights must be arranged with the hotel directly.")
    hotel = Hotel.get()
    if not staff and hotel and (check_in - today).days > hotel.max_advance_days:
        raise PricingError(f"Bookings can only be made up to {hotel.max_advance_days} days in advance.")


def split_guests(adults, children, rooms):
    """Spread guests evenly across rooms -> [(adults, children), ...]."""
    out = []
    for i in range(rooms):
        a = adults // rooms + (1 if i < adults % rooms else 0)
        c = children // rooms + (1 if i < children % rooms else 0)
        out.append((a, c))
    return out


def validate_occupancy(room_type, adults, children, rooms):
    if rooms < 1:
        raise PricingError("Please select at least one room.")
    hotel = Hotel.get()
    if hotel and rooms > hotel.max_rooms_per_booking:
        raise PricingError(f"You can book at most {hotel.max_rooms_per_booking} rooms at once.")
    if adults < 1:
        raise PricingError("At least one adult is required.")
    if adults < rooms:
        raise PricingError("Each room needs at least one adult.")
    for a, c in split_guests(adults, children, rooms):
        if a > room_type.max_adults or c > room_type.max_children or a + c > room_type.max_occupancy:
            raise PricingError("Number of guests exceeds room capacity.")


# --------------------------------------------------------------------------- rate lookup
def _apply(price, kind, value):
    return price * (Decimal("1") + value / Decimal("100")) if kind == "PERCENT" else price + value


def _load(room_type, plan, nights_list):
    rows = DailyRate.objects.filter(room_type=room_type, date__in=nights_list).filter(
        rate_plan__isnull=True) | DailyRate.objects.filter(room_type=room_type, date__in=nights_list, rate_plan=plan)
    by = {}
    for r in rows:
        by[(r.rate_plan_id, r.date)] = r
    rules = list(PricingRule.objects.filter(active=True).filter(room_type__isnull=True) | PricingRule.objects.filter(active=True, room_type=room_type))
    rules.sort(key=lambda r: (r.priority, r.id))
    return by, rules


def nightly_rate(room_type, plan, d, rows, rules):
    """Room-only nightly rate for one date (before extra guests / meals)."""
    plan_row = rows.get((plan.id, d))
    if plan_row and plan_row.price is not None:
        return q2(max(plan_row.price, ZERO))
    base_row = rows.get((None, d))
    if base_row and base_row.price is not None:
        price = base_row.price
    else:
        price = room_type.base_rate
        for rule in rules:
            if rule.applies_on(d, room_type.id):
                price = _apply(price, rule.adjustment_type, rule.value)
    if plan.adjustment_type == RatePlan.ADJ_PERCENT:
        price = price * (Decimal("1") + plan.adjustment_value / Decimal("100"))
    elif plan.adjustment_type == RatePlan.ADJ_FIXED:
        price = price + plan.adjustment_value
    return q2(max(price, ZERO))


def check_restrictions(room_type, plan, check_in, check_out, rows, staff=False):
    nights = (check_out - check_in).days
    if not plan.active:
        raise PricingError("This rate plan is not available.")
    if plan.valid_from and check_in < plan.valid_from or plan.valid_to and check_out - timedelta(days=1) > plan.valid_to:
        raise PricingError("This rate is not valid for the selected dates.")
    if nights < plan.min_stay:
        raise PricingError(f"Minimum stay for this rate is {plan.min_stay} nights.")
    if plan.max_stay and nights > plan.max_stay:
        raise PricingError(f"Maximum stay for this rate is {plan.max_stay} nights.")
    if staff:
        return  # reception may override channel restrictions
    day = check_in
    while day < check_out:
        for key in ((None, day), (plan.id, day)):
            r = rows.get(key)
            if r and r.stop_sell:
                raise PricingError("This room is not on sale for some of the selected dates.")
        day += timedelta(days=1)
    for key in ((None, check_in), (plan.id, check_in)):
        r = rows.get(key)
        if r:
            if r.closed_to_arrival:
                raise PricingError("Arrivals are closed on the selected check-in date.")
            if r.min_stay and nights < r.min_stay:
                raise PricingError(f"A minimum stay of {r.min_stay} nights applies to this check-in date.")
    for key in ((None, check_out), (plan.id, check_out)):
        r = rows.get(key)
        if r and r.closed_to_departure:
            raise PricingError("Departures are closed on the selected check-out date.")


# --------------------------------------------------------------------------- coupons
def validate_coupon(code, room_type, nights, room_subtotal):
    code = (code or "").strip().upper()
    c = Coupon.objects.filter(code__iexact=code, active=True).first()
    if not c:
        raise CouponError("This coupon code is not valid.", "coupon")
    today = timezone.localdate()
    if (c.valid_from and today < c.valid_from) or (c.valid_to and today > c.valid_to):
        raise CouponError("This coupon has expired or is not active yet.", "coupon")
    if c.usage_limit is not None and c.used_count >= c.usage_limit:
        raise CouponError("This coupon has reached its usage limit.", "coupon")
    if nights < c.min_nights:
        raise CouponError(f"This coupon needs a stay of at least {c.min_nights} nights.", "coupon")
    if room_subtotal < c.min_amount:
        raise CouponError("The booking value is too low for this coupon.", "coupon")
    ids = list(c.room_types.values_list("id", flat=True))
    if ids and room_type.id not in ids:
        raise CouponError("This coupon does not apply to the selected room.", "coupon")
    return c


# --------------------------------------------------------------------------- the quote
def quote(room_type, plan, check_in, check_out, adults, children, rooms, *, coupon_code=None, service_ids=None, staff=False):
    """Full price breakdown. Raises PricingError for invalid requests."""
    validate_dates(check_in, check_out, staff=staff)
    validate_occupancy(room_type, adults, children, rooms)
    if plan.room_type_id != room_type.id:
        raise PricingError("This rate plan does not belong to the selected room.")
    if plan.corporate and not staff:
        raise PricingError("This rate plan is not available.")
    hotel = Hotel.get()
    nights_list = [check_in + timedelta(days=i) for i in range((check_out - check_in).days)]
    nights = len(nights_list)
    rows, rules = _load(room_type, plan, nights_list + [check_out])
    check_restrictions(room_type, plan, check_in, check_out, rows, staff=staff)

    per_night = []
    room_total = extra_adult_total = child_total = ZERO
    night_taxable = []  # per night, per room: taxable room amount
    distribution = split_guests(adults, children, rooms)
    for d in nights_list:
        rate = nightly_rate(room_type, plan, d, rows, rules)
        per_night.append({"date": d.isoformat(), "rate": rate})
        for a, c in distribution:
            extra_a = max(a - room_type.base_occupancy, 0) * room_type.extra_adult_charge
            extra_c = c * room_type.child_charge
            room_total += rate
            extra_adult_total += extra_a
            child_total += extra_c
            night_taxable.append(rate + extra_a + extra_c)

    lines = []
    lines.append({"category": "ROOM", "description": f"{room_type.name} — {plan.name}",
                  "quantity": Decimal(nights * rooms), "unit_price": q2(room_total / (nights * rooms)),
                  "amount": q2(room_total), "detail": f"{nights} night{'s' if nights > 1 else ''} × {rooms} room{'s' if rooms > 1 else ''}"})
    if extra_adult_total:
        extra_adults = sum(max(a - room_type.base_occupancy, 0) for a, _ in distribution)
        lines.append({"category": "EXTRA_GUEST", "description": "Extra adult charge", "quantity": Decimal(extra_adults * nights),
                      "unit_price": room_type.extra_adult_charge, "amount": q2(extra_adult_total),
                      "detail": f"{extra_adults} extra adult(s) × {nights} nights"})
    if child_total:
        lines.append({"category": "EXTRA_GUEST", "description": "Child charge", "quantity": Decimal(children * nights),
                      "unit_price": room_type.child_charge, "amount": q2(child_total),
                      "detail": f"{children} child(ren) × {nights} nights"})
    meal_total = q2(adults * plan.meal_adult_price * nights + children * plan.meal_child_price * nights)
    if meal_total:
        lines.append({"category": "MEAL", "description": f"{plan.get_meal_plan_display()} meals",
                      "quantity": Decimal((adults + children) * nights), "unit_price": plan.meal_adult_price, "amount": meal_total,
                      "detail": f"{adults} adult(s) × {plan.meal_adult_price:,.0f}"
                                + (f" + {children} child(ren) × {plan.meal_child_price:,.0f}" if children else "") + f" × {nights} nights"})
    service_total = taxable_service_total = ZERO
    guests = adults + children
    for s in Service.objects.filter(pk__in=list(service_ids or []), active=True):
        if s.charge_type == Service.PER_NIGHT:
            qty = Decimal(nights)
        elif s.charge_type == Service.PER_PERSON_NIGHT:
            qty = Decimal(nights * guests)
        else:
            qty = Decimal(1)
        amt = q2(s.price * qty)
        service_total += amt
        if s.taxable:
            taxable_service_total += amt
        lines.append({"category": "SERVICE", "description": s.name, "quantity": qty, "unit_price": s.price, "amount": amt,
                      "detail": s.get_charge_type_display(), "service_id": s.id})

    room_side = q2(room_total + extra_adult_total + child_total)
    discount = ZERO
    coupon = None
    if coupon_code:
        coupon = validate_coupon(coupon_code, room_type, nights, room_side)
        discount = room_side * coupon.value / Decimal("100") if coupon.discount_type == Coupon.PERCENT else coupon.value
        if coupon.max_discount is not None:
            discount = min(discount, coupon.max_discount)
        discount = q2(min(discount, room_side))
        if discount:
            lines.append({"category": "DISCOUNT", "description": f"Coupon {coupon.code} — {coupon.title}", "quantity": Decimal(1),
                          "unit_price": -discount, "amount": -discount, "detail": ""})

    # taxes ---------------------------------------------------------------
    tax_acc = {}
    rules_by = {k: [] for k in ("ROOM", "MEAL", "SERVICE")}
    for t in TaxRule.objects.filter(active=True):
        rules_by[t.applies_to].append(t)
    room_taxable_sum = sum(night_taxable) or Decimal("1")
    for amt in night_taxable:
        net = amt - (discount * amt / room_taxable_sum)
        for t in rules_by["ROOM"]:
            if net > t.min_amount and (t.max_amount is None or net <= t.max_amount):
                tax_acc.setdefault((t.name, t.rate), ZERO)
                tax_acc[(t.name, t.rate)] += net * t.rate / Decimal("100")
    for t in rules_by["MEAL"]:
        tax_acc.setdefault((t.name, t.rate), ZERO)
        tax_acc[(t.name, t.rate)] += meal_total * t.rate / Decimal("100")
    for t in rules_by["SERVICE"]:
        tax_acc.setdefault((t.name, t.rate), ZERO)
        tax_acc[(t.name, t.rate)] += taxable_service_total * t.rate / Decimal("100")
    tax_lines = [{"name": n, "rate": r, "amount": q2(a)} for (n, r), a in tax_acc.items() if a]
    tax_total = sum((t["amount"] for t in tax_lines), ZERO)

    subtotal = q2(room_side + meal_total + service_total)
    total = q2(subtotal - discount + tax_total)
    return {
        "room_type_id": room_type.id, "rate_plan_id": plan.id, "check_in": check_in.isoformat(), "check_out": check_out.isoformat(),
        "nights": nights, "adults": adults, "children": children, "rooms": rooms, "per_night": per_night,
        "lines": lines, "room_charges": q2(room_total), "extra_guest_charges": q2(extra_adult_total + child_total),
        "meal_charges": meal_total, "service_charges": q2(meal_total + service_total), "subtotal": subtotal,
        "discount": discount, "coupon": coupon.code if coupon else "", "tax_lines": tax_lines, "tax_total": tax_total, "total": total,
        "avg_nightly": q2(room_total / (nights * rooms)), "currency": hotel.currency if hotel else "INR",
        "currency_symbol": hotel.currency_symbol if hotel else "₹",
        "cancellation_policy": plan.cancellation_text, "refundable": plan.refundable,
    }
