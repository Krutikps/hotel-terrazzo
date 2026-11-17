"""Reservation engine: creation, holds, confirmation, modification, cancellation, room assignment,
check-in/out, blocks, maintenance. Every inventory-changing operation runs in one transaction that first
locks the room type and re-validates availability server-side.
"""
import logging
from datetime import datetime, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from hotel.models import (Counter, Coupon, Guest, HousekeepingLog, Hotel, Invoice, MaintenanceTicket, Payment, RatePlan,
                          Reservation, ReservationLine, ReservationRequest, ReservationRoom, Room, RoomBlock, RoomType, TaxRule)
from . import audit, availability, notifications, pricing

log = logging.getLogger("hotel.reservations")
ZERO = Decimal("0")


class BookingError(Exception):
    def __init__(self, message, code="error"):
        super().__init__(message)
        self.message = message
        self.code = code


class HoldExpired(BookingError):
    pass


# --------------------------------------------------------------------------- helpers
@transaction.atomic  # SELECT ... FOR UPDATE needs an open transaction on PostgreSQL (SQLite ignores it, which hid this in dev)
def next_number(kind, prefix):
    year = timezone.localdate().year
    counter, _ = Counter.objects.select_for_update().get_or_create(kind=kind, year=year)
    Counter.objects.filter(pk=counter.pk).update(last=F("last") + 1)
    counter.refresh_from_db()
    return f"{prefix}-{year}-{counter.last:06d}"


def recompute(res):
    """Re-derive money fields + payment status from lines and payments."""
    agg = {k: ZERO for k in ("ROOM", "EXTRA_GUEST", "SERVICE", "DISCOUNT", "TAX")}
    for ln in res.lines.all():
        if ln.category in ("ROOM", "EXTRA_GUEST", "DISCOUNT", "TAX"):
            agg[ln.category] += ln.amount
        else:
            agg["SERVICE"] += ln.amount
    res.room_charges = agg["ROOM"]
    res.extra_guest_charges = agg["EXTRA_GUEST"]
    res.service_charges = agg["SERVICE"]
    res.discount_total = abs(agg["DISCOUNT"])
    res.tax_total = agg["TAX"]
    res.total = agg["ROOM"] + agg["EXTRA_GUEST"] + agg["SERVICE"] + agg["DISCOUNT"] + agg["TAX"]
    pays = list(res.payments.all())
    gross = sum((p.amount for p in pays if p.kind == Payment.PAYMENT and p.status == Payment.PAID), ZERO)
    refunded = sum((p.amount for p in pays if p.kind == Payment.REFUND and p.status == Payment.PAID), ZERO)
    net = gross - refunded
    res.amount_paid = net
    if refunded > 0:
        res.payment_status = Reservation.P_REFUNDED if net <= 0 else Reservation.P_PART_REFUNDED
    elif gross > 0 and gross >= res.total:
        res.payment_status = Reservation.P_PAID
    elif gross > 0:
        res.payment_status = Reservation.P_PARTIAL
    elif any(p.kind == Payment.PAYMENT and p.status == Payment.AUTHORIZED for p in pays):
        res.payment_status = Reservation.P_AUTHORIZED
    elif pays and all(p.status == Payment.FAILED for p in pays if p.kind == Payment.PAYMENT) and any(p.kind == Payment.PAYMENT for p in pays):
        res.payment_status = Reservation.P_FAILED
    else:
        res.payment_status = Reservation.P_PENDING
    res.save(update_fields=["room_charges", "extra_guest_charges", "service_charges", "discount_total", "tax_total",
                            "total", "amount_paid", "payment_status", "updated_at"])
    return res


def _write_quote_lines(res, quote):
    res.lines.filter(is_auto=True).delete()
    for ln in quote["lines"]:
        ReservationLine.objects.create(reservation=res, category=ln["category"], description=ln["description"][:200],
                                       quantity=ln["quantity"], unit_price=ln["unit_price"], amount=ln["amount"], is_auto=True)
    for t in quote["tax_lines"]:
        ReservationLine.objects.create(reservation=res, category="TAX", description=f"{t['name']} @ {t['rate']}%",
                                       quantity=1, unit_price=t["amount"], amount=t["amount"], is_auto=True)


def _snapshot(quote, service_ids):
    snap = {k: v for k, v in quote.items() if k not in ("lines", "tax_lines")}
    snap["service_ids"] = list(service_ids or [])
    return snap


def _cancellation_terms(plan, check_in, hotel):
    deadline = None
    if plan.refundable and plan.free_cancel_hours:
        arrival = timezone.make_aware(datetime.combine(check_in, hotel.check_in_time))
        deadline = arrival - timedelta(hours=plan.free_cancel_hours)
    return {"cancellation_policy": plan.cancellation_text, "refundable": plan.refundable,
            "free_cancel_until": deadline, "cancel_penalty_percent": plan.cancel_penalty_percent}


def _upsert_guest(data):
    email = (data.get("email") or "").strip().lower()
    phone = (data.get("phone") or "").strip()
    guest = None
    if email:
        guest = Guest.objects.filter(email__iexact=email).first()
    if not guest and phone and not email:
        guest = Guest.objects.filter(phone=phone).first()
    fields = ["first_name", "last_name", "phone", "address", "city", "state", "country", "postal_code", "id_type", "id_number"]
    if guest is None:
        guest = Guest(email=email)
    for f in fields:
        v = (data.get(f) or "").strip()
        if v:
            setattr(guest, f, v)
    if email:
        guest.email = email
    if data.get("date_of_birth"):
        guest.date_of_birth = data["date_of_birth"]
    if not guest.first_name:
        raise BookingError("Guest first name is required.")
    guest.save()
    return guest


# --------------------------------------------------------------------------- creation
@transaction.atomic
def create_reservation(*, room_type, rate_plan, check_in, check_out, adults, children=0, rooms=1, guest_data, source="WEBSITE",
                       purpose="", arrival_time="", special_requests="", request_types=(), coupon_code=None, service_ids=None,
                       online_hold=False, user=None, room_ids=None, internal_notes="", request=None):
    """Create a reservation. online_hold=True -> PENDING_PAYMENT with expiry; otherwise CONFIRMED immediately."""
    staff = not online_hold
    hotel = Hotel.get()
    availability.lock_inventory([room_type.id])
    pricing.validate_dates(check_in, check_out, staff=staff)
    pricing.validate_occupancy(room_type, adults, children, rooms)
    if not room_type.active or rate_plan.room_type_id != room_type.id:
        raise BookingError("This room is no longer available. Please select another room.", "unavailable")
    try:
        availability.assert_available(room_type.id, check_in, check_out, rooms)
    except availability.Unavailable as e:
        raise BookingError(str(e), "unavailable")
    quote = pricing.quote(room_type, rate_plan, check_in, check_out, adults, children, rooms, coupon_code=coupon_code,
                          service_ids=service_ids, staff=staff)
    guest = _upsert_guest(guest_data)
    now = timezone.now()
    res = Reservation.objects.create(
        hotel=hotel, guest=guest, source=source, check_in=check_in, check_out=check_out, nights=quote["nights"],
        adults=adults, children=children, room_count=rooms, room_type=room_type, rate_plan=rate_plan,
        purpose=(purpose or "")[:80], arrival_time=(arrival_time or "")[:40], special_requests=(special_requests or "")[:2000],
        internal_notes=internal_notes, created_by=user if getattr(user, "is_authenticated", False) else None,
        coupon=Coupon.objects.filter(code__iexact=quote["coupon"]).first() if quote["coupon"] else None,
        pricing_snapshot={}, status=Reservation.PENDING_PAYMENT if online_hold else Reservation.CONFIRMED,
        hold_expires_at=now + timedelta(minutes=hotel.hold_minutes) if online_hold else None,
        **_cancellation_terms(rate_plan, check_in, hotel),
    )
    res.pricing_snapshot = _snapshot(quote, service_ids)
    res.save(update_fields=["pricing_snapshot"])
    for i, (a, c) in enumerate(pricing.split_guests(adults, children, rooms)):
        ReservationRoom.objects.create(reservation=res, room_type=room_type, adults=a, children=c)
    for t in request_types or []:
        if t:
            ReservationRequest.objects.create(reservation=res, request_type=str(t)[:60])
    _write_quote_lines(res, quote)
    if room_ids:
        for rr, rid in zip(res.rooms.all(), room_ids):
            _assign(res, rr, Room.objects.get(pk=rid), user, audit_it=False)
    if not online_hold:
        res.booking_number = next_number("booking", hotel.booking_prefix)
        res.confirmed_at = now
        res.save(update_fields=["booking_number", "confirmed_at"])
        if res.coupon_id:
            Coupon.objects.filter(pk=res.coupon_id).update(used_count=F("used_count") + 1)
    recompute(res)
    audit.log("reservation.created", res, user=user, request=request, new={"status": res.status, "total": res.total, "source": source},
              summary=f"{'Hold' if online_hold else 'Reservation'} created for {guest.name} ({check_in}→{check_out})",
              actor_label="" if user else "Guest (website)")
    if not online_hold:
        notifications.notify_after_commit("BOOKING_CONFIRMED", res)
        notifications.notify_after_commit("NEW_BOOKING", res)
        if res.special_requests or res.requests.exists():
            notifications.notify_after_commit("SPECIAL_REQUEST", res)
    return res


def _hold_state(res):
    return res.status == Reservation.PENDING_PAYMENT and res.hold_expires_at and res.hold_expires_at > timezone.now()


@transaction.atomic
def confirm_after_payment(reservation_id, request=None):
    """PENDING_PAYMENT -> CONFIRMED. Called only after server-side payment verification."""
    res = Reservation.objects.select_for_update().get(pk=reservation_id)
    if res.status in (Reservation.CONFIRMED, Reservation.CHECKED_IN, Reservation.CHECKED_OUT):
        return res
    if res.status not in (Reservation.PENDING_PAYMENT, Reservation.RELEASED):
        raise BookingError("This booking can no longer be confirmed.", "invalid_state")
    if not _hold_state(res):  # hold lapsed (maybe released by the sweeper): re-check inventory before reviving
        availability.lock_inventory([res.room_type_id])
        try:
            availability.assert_available(res.room_type_id, res.check_in, res.check_out, res.room_count, exclude_reservation_id=res.id)
        except availability.Unavailable:
            raise HoldExpired("Your booking session has expired and the room is no longer available.", "hold_expired")
    hotel = res.hotel
    res.status = Reservation.CONFIRMED
    res.hold_expires_at = None
    res.confirmed_at = timezone.now()
    if not res.booking_number:
        res.booking_number = next_number("booking", hotel.booking_prefix)
    res.save()
    if res.coupon_id:
        Coupon.objects.filter(pk=res.coupon_id).update(used_count=F("used_count") + 1)
    audit.log("reservation.confirmed", res, request=request, actor_label="Payment verified",
              new={"status": res.status, "booking_number": res.booking_number}, summary=f"Booking {res.booking_number} confirmed")
    notifications.notify_after_commit("BOOKING_CONFIRMED", res)
    notifications.notify_after_commit("PAYMENT_CONFIRMED", res)
    notifications.notify_after_commit("NEW_BOOKING", res)
    if res.special_requests or res.requests.exists():
        notifications.notify_after_commit("SPECIAL_REQUEST", res)
    return res


@transaction.atomic
def release_hold(reservation_id, reason="Payment not completed"):
    res = Reservation.objects.select_for_update().get(pk=reservation_id)
    if res.status != Reservation.PENDING_PAYMENT:
        return res
    res.status = Reservation.RELEASED
    res.hold_expires_at = timezone.now()
    res.save(update_fields=["status", "hold_expires_at", "updated_at"])
    res.payments.filter(status=Payment.PENDING).update(status=Payment.FAILED, note=reason[:255])
    recompute(res)
    audit.log("reservation.released", res, actor_label="System", summary=reason, new={"status": res.status})
    return res


def expire_holds():
    """Release every lapsed hold. Safe to run as often as you like (cron / management command / sweeper)."""
    ids = list(Reservation.objects.filter(status=Reservation.PENDING_PAYMENT, hold_expires_at__lte=timezone.now()).values_list("id", flat=True))
    for rid in ids:
        release_hold(rid, "Payment hold expired")
    if ids:
        log.info("Released %s expired hold(s)", len(ids))
    return len(ids)


# --------------------------------------------------------------------------- modification
@transaction.atomic
def modify(reservation_id, user, *, check_in=None, check_out=None, adults=None, children=None, rooms=None, room_type=None,
           rate_plan=None, coupon_code=None, request=None):
    res = Reservation.objects.select_for_update().get(pk=reservation_id)
    if res.status not in (Reservation.CONFIRMED, Reservation.CHECKED_IN):
        raise BookingError("Only confirmed or checked-in bookings can be modified.")
    new = dict(check_in=check_in or res.check_in, check_out=check_out or res.check_out,
               adults=res.adults if adults is None else adults, children=res.children if children is None else children,
               rooms=rooms or res.room_count, room_type=room_type or res.room_type, rate_plan=rate_plan or res.rate_plan)
    if rate_plan and not room_type and rate_plan.room_type_id != res.room_type_id:
        new["room_type"] = rate_plan.room_type
    if new["rate_plan"].room_type_id != new["room_type"].id:  # room type changed, plan not -> pick same-named / first plan
        cand = new["room_type"].rate_plans.filter(active=True, name=res.rate_plan.name).first() or new["room_type"].rate_plans.filter(active=True).first()
        if not cand:
            raise BookingError("The selected room has no active rate plan.")
        new["rate_plan"] = cand
    if res.status == Reservation.CHECKED_IN and (new["check_in"] != res.check_in or new["room_type"] != res.room_type or new["rooms"] != res.room_count):
        raise BookingError("A checked-in booking can only have its check-out date, guests or rate changed.")
    old = {"check_in": res.check_in, "check_out": res.check_out, "adults": res.adults, "children": res.children,
           "rooms": res.room_count, "room_type": res.room_type.name, "rate_plan": res.rate_plan.name, "total": res.total}
    availability.lock_inventory([res.room_type_id, new["room_type"].id])
    pricing.validate_dates(new["check_in"], new["check_out"], staff=True)
    pricing.validate_occupancy(new["room_type"], new["adults"], new["children"], new["rooms"])
    try:
        availability.assert_available(new["room_type"].id, new["check_in"], new["check_out"], new["rooms"], exclude_reservation_id=res.id)
    except availability.Unavailable as e:
        raise BookingError(str(e), "unavailable")
    code = coupon_code if coupon_code is not None else (res.coupon.code if res.coupon_id else None)
    sids = res.pricing_snapshot.get("service_ids", [])
    try:
        q = pricing.quote(new["room_type"], new["rate_plan"], new["check_in"], new["check_out"], new["adults"], new["children"],
                          new["rooms"], coupon_code=code, service_ids=sids, staff=True)
    except pricing.CouponError:
        q = pricing.quote(new["room_type"], new["rate_plan"], new["check_in"], new["check_out"], new["adults"], new["children"],
                          new["rooms"], coupon_code=None, service_ids=sids, staff=True)
    # rebuild room rows, keeping physical assignments that are still valid
    existing = list(res.rooms.select_related("room"))
    dist = pricing.split_guests(new["adults"], new["children"], new["rooms"])
    for i, (a, c) in enumerate(dist):
        rr = existing[i] if i < len(existing) else ReservationRoom(reservation=res)
        keep = rr.room if rr.room_id and rr.room.room_type_id == new["room_type"].id and not availability.room_conflicts(
            rr.room, new["check_in"], new["check_out"], exclude_reservation_id=res.id) else None
        if rr.room_id and not keep and res.status == Reservation.CHECKED_IN:
            keep = rr.room
        rr.room_type, rr.room, rr.adults, rr.children = new["room_type"], keep, a, c
        rr.save()
    for rr in existing[len(dist):]:
        rr.delete()
    res.check_in, res.check_out, res.nights = new["check_in"], new["check_out"], q["nights"]
    res.adults, res.children, res.room_count = new["adults"], new["children"], new["rooms"]
    res.room_type, res.rate_plan = new["room_type"], new["rate_plan"]
    res.coupon = Coupon.objects.filter(code__iexact=q["coupon"]).first() if q["coupon"] else None
    res.pricing_snapshot = _snapshot(q, sids)
    for k, v in _cancellation_terms(new["rate_plan"], new["check_in"], res.hotel).items():
        setattr(res, k, v)
    res.save()
    _write_quote_lines(res, q)
    recompute(res)
    audit.log("reservation.modified", res, user=user, request=request, old=old,
              new={"check_in": res.check_in, "check_out": res.check_out, "adults": res.adults, "children": res.children,
                   "rooms": res.room_count, "room_type": res.room_type.name, "rate_plan": res.rate_plan.name, "total": res.total},
              summary=f"Booking {res.reference} modified")
    notifications.notify_after_commit("MODIFIED", res)
    return res


def update_details(res, user, *, purpose=None, arrival_time=None, special_requests=None, internal_notes=None, source=None, request=None):
    old = {"purpose": res.purpose, "arrival_time": res.arrival_time, "special_requests": res.special_requests,
           "internal_notes": res.internal_notes, "source": res.source}
    for k, v in dict(purpose=purpose, arrival_time=arrival_time, special_requests=special_requests, internal_notes=internal_notes, source=source).items():
        if v is not None:
            setattr(res, k, v)
    res.save()
    audit.log("reservation.updated", res, user=user, request=request, old=old,
              new={"purpose": res.purpose, "arrival_time": res.arrival_time, "special_requests": res.special_requests,
                   "internal_notes": res.internal_notes, "source": res.source}, summary=f"Details updated for {res.reference}")
    return res


# --------------------------------------------------------------------------- cancellation
def cancellation_quote(res, waive_fee=False):
    """What a cancellation would cost right now -> (fee, refund)."""
    paid = res.amount_paid
    if waive_fee:
        fee = ZERO
    elif res.refundable and res.free_cancel_until and timezone.now() <= res.free_cancel_until:
        fee = ZERO
    else:
        fee = (res.total * res.cancel_penalty_percent / Decimal("100")).quantize(Decimal("0.01")) if res.refundable else res.total
    fee = min(fee, max(paid, res.total))
    refund = max(paid - fee, ZERO)
    return fee, refund


@transaction.atomic
def cancel(reservation_id, *, user=None, reason="", waive_fee=False, by_guest=False, request=None):
    from . import payments

    res = Reservation.objects.select_for_update().get(pk=reservation_id)
    if res.status == Reservation.PENDING_PAYMENT:
        return release_hold(res.id, reason or "Cancelled by guest")
    if res.status != Reservation.CONFIRMED:
        raise BookingError("This booking can no longer be cancelled online." if by_guest else "Only confirmed bookings can be cancelled.")
    if by_guest:
        if not res.hotel.allow_guest_cancellation:
            raise BookingError("Online cancellation is not available. Please contact the hotel.")
        if timezone.localdate() >= res.check_in:
            raise BookingError("This booking can no longer be cancelled online. Please contact the hotel.")
    fee, refund = cancellation_quote(res, waive_fee=waive_fee and not by_guest)
    old = {"status": res.status}
    res.status = Reservation.CANCELLED
    res.cancelled_at = timezone.now()
    res.cancellation_reason = (reason or "")[:255]
    res.rooms.update(room=None)
    res.save()
    if refund > 0:
        payments.refund_reservation(res, refund, user=user, reason=f"Cancellation of {res.reference}", request=request)
    recompute(res)
    audit.log("reservation.cancelled", res, user=user, request=request, old=old, actor_label="" if user else "Guest (website)",
              new={"status": res.status, "fee": fee, "refund": refund, "reason": reason}, summary=f"Booking {res.reference} cancelled (fee {fee}, refund {refund})")
    notifications.notify_after_commit("CANCELLED", res, extra={"fee": fee, "refund": refund})
    return res


# --------------------------------------------------------------------------- rooms
def _assign(res, rr, room, user, audit_it=True, request=None):
    if room.room_type_id != rr.room_type_id:
        raise BookingError(f"Room {room.room_number} is a {room.room_type.name}; this booking is for a {rr.room_type.name}. Use 'Edit reservation' to change room type.")
    problems = availability.room_conflicts(room, res.check_in, res.check_out, exclude_reservation_id=res.id)
    if problems:
        raise BookingError(f"Room {room.room_number} can't be assigned: {' '.join(problems)}")
    if res.status == Reservation.CHECKED_IN:
        if room.status in Room.NOT_READY:
            raise BookingError(f"Room {room.room_number} is not ready ({room.get_status_display()}).")
        if rr.room_id:
            old = rr.room
            old.status = Room.DIRTY
            old.save(update_fields=["status"])
            HousekeepingLog.objects.create(room=old, from_status=Room.OCCUPIED, to_status=Room.DIRTY, user=user, notes="Guest moved rooms")
        room.status = Room.OCCUPIED
        room.save(update_fields=["status"])
    old_no = rr.room.room_number if rr.room_id else None
    rr.room = room
    rr.save(update_fields=["room"])
    if audit_it:
        audit.log("reservation.room_assigned", res, user=user, request=request, old={"room": old_no}, new={"room": room.room_number},
                  summary=f"Room {room.room_number} assigned to {res.reference}")
    return rr


@transaction.atomic
def assign_room(reservation_id, reservation_room_id, room_id, user, request=None):
    res = Reservation.objects.select_for_update().get(pk=reservation_id)
    if res.status not in (Reservation.CONFIRMED, Reservation.CHECKED_IN):
        raise BookingError("Rooms can only be assigned on confirmed or checked-in bookings.")
    availability.lock_inventory([res.room_type_id])
    rr = res.rooms.select_for_update().get(pk=reservation_room_id)
    room = Room.objects.select_for_update().get(pk=room_id)
    _assign(res, rr, room, user, request=request)
    notifications.notify_after_commit("MODIFIED", res, guest=False)
    return rr


@transaction.atomic
def unassign_room(reservation_id, reservation_room_id, user, request=None):
    res = Reservation.objects.select_for_update().get(pk=reservation_id)
    if res.status != Reservation.CONFIRMED:
        raise BookingError("Only rooms on confirmed (not yet checked-in) bookings can be unassigned.")
    rr = res.rooms.get(pk=reservation_room_id)
    old = rr.room.room_number if rr.room_id else None
    rr.room = None
    rr.save(update_fields=["room"])
    audit.log("reservation.room_unassigned", res, user=user, request=request, old={"room": old}, summary=f"Room unassigned from {res.reference}")


def _auto_assign(res):
    for rr in res.rooms.filter(room__isnull=True):
        cand = availability.assignable_rooms(rr.room_type_id, res.check_in, res.check_out, exclude_reservation_id=res.id)
        cand = cand.exclude(status__in=Room.NOT_READY).order_by("room_number").first()
        if not cand:
            raise BookingError(f"No ready {rr.room_type.name} is available to assign. Assign a room manually (rooms may still be dirty).")
        rr.room = cand
        rr.save(update_fields=["room"])


# --------------------------------------------------------------------------- check-in / out
@transaction.atomic
def check_in(reservation_id, user, *, collect_amount=ZERO, method="CASH", reference="", notes="", id_type="", id_number="", request=None):
    from . import payments

    res = Reservation.objects.select_for_update().get(pk=reservation_id)
    if res.status != Reservation.CONFIRMED:
        raise BookingError(f"Only confirmed bookings can be checked in (this one is {res.get_status_display().lower()}).")
    today = timezone.localdate()
    if res.check_in > today:
        raise BookingError(f"This booking arrives on {res.check_in:%d %b %Y}; it cannot be checked in earlier.")
    if res.check_out <= today:
        raise BookingError("The stay has already ended; modify the dates first.")
    availability.lock_inventory([res.room_type_id])
    _auto_assign(res)
    rooms = list(res.rooms.select_related("room"))
    for rr in rooms:
        if rr.room.status in Room.NOT_READY:
            raise BookingError(f"Room {rr.room.room_number} is {rr.room.get_status_display().lower()} and cannot be given to the guest yet. Reassign or wait for housekeeping.")
    g = res.guest
    if id_type:
        g.id_type = id_type
    if id_number:
        g.id_number = id_number
    g.save()
    if not (g.id_type and g.id_number):
        raise BookingError("Verify the guest's identity: ID type and number are required for check-in.")
    if collect_amount and collect_amount > 0:
        payments.record_offline(res, collect_amount, method, user=user, reference=reference, note="Collected at check-in", request=request)
    for rr in rooms:
        prev = rr.room.status
        rr.room.status = Room.OCCUPIED
        rr.room.save(update_fields=["status"])
        HousekeepingLog.objects.create(room=rr.room, from_status=prev, to_status=Room.OCCUPIED, user=user, notes=f"Check-in {res.reference}")
    if notes:
        res.internal_notes = (res.internal_notes + "\n" if res.internal_notes else "") + f"[Check-in {timezone.localtime():%d %b %H:%M}] {notes}"
    res.status = Reservation.CHECKED_IN
    res.checked_in_at = timezone.now()
    res.save()
    audit.log("reservation.checked_in", res, user=user, request=request, new={"rooms": res.assigned_room_numbers}, summary=f"{res.reference} checked in")
    return res


def add_charge(res, category, description, amount, user=None, taxable=False, quantity=1, request=None):
    """Manual extra charge (minibar, late checkout…). Adds tax with the SERVICE rule if taxable."""
    amount = Decimal(amount)
    if amount <= 0:
        raise BookingError("Charge amount must be greater than zero.")
    ReservationLine.objects.create(reservation=res, category=category, description=description[:200], quantity=quantity,
                                   unit_price=amount / Decimal(quantity), amount=amount, is_auto=False, taxable=taxable)
    if taxable:
        for t in TaxRule.objects.filter(active=True, applies_to=TaxRule.SERVICE):
            tax = pricing.q2(amount * t.rate / Decimal("100"))
            if tax:
                ReservationLine.objects.create(reservation=res, category="TAX", description=f"{t.name} @ {t.rate}% on {description[:60]}",
                                               amount=tax, unit_price=tax, is_auto=False)
    recompute(res)
    audit.log("reservation.charge_added", res, user=user, request=request, new={"category": category, "description": description, "amount": amount},
              summary=f"Charge {description} {amount} added to {res.reference}")


@transaction.atomic
def check_out(reservation_id, user, *, charges=(), collect_amount=ZERO, method="CASH", reference="", allow_unpaid=False, request=None):
    from . import payments

    res = Reservation.objects.select_for_update().get(pk=reservation_id)
    if res.status != Reservation.CHECKED_IN:
        raise BookingError("Only checked-in bookings can be checked out.")
    for c in charges:
        add_charge(res, c["category"], c["description"], c["amount"], user=user, taxable=c.get("taxable", True), request=request)
    if collect_amount and Decimal(collect_amount) > 0:
        payments.record_offline(res, Decimal(collect_amount), method, user=user, reference=reference, note="Collected at check-out", request=request)
    res.refresh_from_db()
    if res.balance > 0 and not allow_unpaid:
        raise BookingError(f"An outstanding balance of {res.hotel.money(res.balance)} must be collected before check-out.", "balance_due")
    res.status = Reservation.CHECKED_OUT
    res.checked_out_at = timezone.now()
    res.save()
    for rr in res.rooms.select_related("room"):
        if rr.room_id:
            rr.room.status = Room.DIRTY
            rr.room.save(update_fields=["status"])
            HousekeepingLog.objects.create(room=rr.room, from_status=Room.OCCUPIED, to_status=Room.DIRTY, user=user, notes=f"Check-out {res.reference}")
    inv = create_invoice(res, user)
    audit.log("reservation.checked_out", res, user=user, request=request, new={"invoice": inv.number, "balance": res.balance}, summary=f"{res.reference} checked out")
    return res, inv


@transaction.atomic
def create_invoice(res, user=None):
    last = res.invoices.first()
    if last and last.total == res.total and last.paid == res.amount_paid:
        return last
    hotel = res.hotel
    number = next_number("invoice", "INV")
    data = invoice_data(res)
    inv = Invoice.objects.create(reservation=res, number=number, data=data, total=res.total, paid=res.amount_paid, created_by=user)
    audit.log("invoice.created", inv, user=user, summary=f"Invoice {number} for {res.reference}")
    return inv


def invoice_data(res):
    h = res.hotel
    return {
        "hotel": {"name": h.name, "address": h.address, "city": h.city, "state": h.state, "postal_code": h.postal_code,
                  "phone": h.phone, "email": h.email, "gstin": h.gstin, "currency_symbol": h.currency_symbol},
        "booking_number": res.reference,
        "guest": {"name": res.guest.name, "email": res.guest.email, "phone": res.guest.phone,
                  "address": ", ".join(x for x in [res.guest.address, res.guest.city, res.guest.state, res.guest.postal_code, res.guest.country] if x),
                  "id": f"{res.guest.id_type} {res.guest.id_number}".strip()},
        "stay": {"check_in": res.check_in.isoformat(), "check_out": res.check_out.isoformat(), "nights": res.nights,
                 "adults": res.adults, "children": res.children, "rooms": res.assigned_room_numbers or "Unassigned",
                 "room_type": res.room_type.name, "rate_plan": res.rate_plan.name},
        "lines": [{"category": l.category, "description": l.description, "quantity": l.quantity, "unit_price": l.unit_price, "amount": l.amount}
                  for l in res.lines.all()],
        "room_charges": res.room_charges, "extra_guest_charges": res.extra_guest_charges, "service_charges": res.service_charges,
        "discount": res.discount_total, "tax": res.tax_total, "total": res.total, "paid": res.amount_paid,
        "balance": res.balance, "payment_status": res.get_payment_status_display(),
        "payments": [{"date": p.created_at.isoformat(), "method": p.get_method_display(), "kind": p.kind, "amount": p.amount,
                      "reference": p.transaction_id or p.provider_payment_id} for p in res.payments.all() if p.status == Payment.PAID],
    }


def mark_no_shows(today=None):
    today = today or timezone.localdate()
    n = 0
    for res in Reservation.objects.filter(status=Reservation.CONFIRMED, check_in__lt=today):
        with transaction.atomic():
            res.status = Reservation.NO_SHOW
            res.save(update_fields=["status", "updated_at"])
            res.rooms.update(room=None)
            audit.log("reservation.no_show", res, actor_label="System", summary=f"{res.reference} marked no-show")
            n += 1
    return n


# --------------------------------------------------------------------------- room blocking
@transaction.atomic
def block_room(room, start_date, end_date, reason, notes, user, request=None):
    if end_date < start_date:
        raise BookingError("End date must be on or after the start date.")
    availability.lock_inventory([room.room_type_id])
    check_out = end_date + timedelta(days=1)
    clash = (ReservationRoom.objects.filter(room=room, reservation__check_in__lt=check_out, reservation__check_out__gt=start_date)
             .filter(availability.holding_q()).select_related("reservation"))
    if clash.exists():
        refs = ", ".join(sorted({c.reservation.reference for c in clash}))
        raise BookingError(f"Room {room.room_number} has guests during this period ({refs}). Move those bookings first.")
    block = RoomBlock.objects.create(room=room, start_date=start_date, end_date=end_date, reason=reason, notes=notes, created_by=user)
    info = availability.room_type_availability(room.room_type_id, start_date, check_out)
    bad = [d for d, n in info["raw"].items() if n < 0]
    if bad:
        raise BookingError(f"Blocking this room would oversell {room.room_type.name} on {min(bad):%d %b}. Reassign or cancel bookings first.")
    audit.log("room.blocked", block, user=user, request=request, new={"room": room.room_number, "start": start_date, "end": end_date, "reason": reason},
              summary=f"Room {room.room_number} blocked {start_date:%d %b}–{end_date:%d %b} ({reason})")
    return block


@transaction.atomic
def unblock_room(block, user, request=None):
    if block.released_at:
        raise BookingError("This block has already been released.")
    block.released_at = timezone.now()
    block.save(update_fields=["released_at"])
    audit.log("room.unblocked", block, user=user, request=request, old={"room": block.room.room_number}, summary=f"Room {block.room.room_number} unblocked")
    return block


# --------------------------------------------------------------------------- housekeeping / maintenance
@transaction.atomic
def set_room_status(room, status, user, notes="", request=None):
    valid = dict(Room.STATUS)
    if status not in valid:
        raise BookingError("Invalid room status.")
    room = Room.objects.select_for_update().get(pk=room.pk)
    if room.status == status:
        return room
    if status in Room.UNSELLABLE and room.status == Room.OCCUPIED:
        raise BookingError("An occupied room cannot be taken out of service until the guest checks out.")
    if room.status == Room.OCCUPIED and status != Room.OCCUPIED and RoomHasActiveStay(room):
        raise BookingError("A guest is checked in to this room; use Check-out or Change room instead.")
    old = room.status
    room.status = status
    room.save(update_fields=["status"])
    HousekeepingLog.objects.create(room=room, from_status=old, to_status=status, user=user, notes=notes[:255])
    audit.log("room.status_changed", room, user=user, request=request, old={"status": old}, new={"status": status},
              summary=f"Room {room.room_number}: {old} → {status}")
    return room


def RoomHasActiveStay(room):
    return ReservationRoom.objects.filter(room=room, reservation__status=Reservation.CHECKED_IN).exists()


@transaction.atomic
def create_ticket(room, problem, priority, user, assigned_to=None, notes="", blocks_room=True, request=None):
    room = Room.objects.select_for_update().get(pk=room.pk)
    if blocks_room:
        availability.lock_inventory([room.room_type_id])
        if room.status == Room.OCCUPIED:
            raise BookingError("This room is occupied. Check the guest out or move them before taking the room out of service.")
    t = MaintenanceTicket.objects.create(room=room, problem=problem, priority=priority, assigned_to=assigned_to, reported_by=user,
                                         notes=notes, blocks_room=blocks_room, previous_room_status=room.status)
    if blocks_room:
        room.status = Room.MAINTENANCE
        room.save(update_fields=["status"])
        # future assignments to this room are released (inventory stays at category level)
        affected = ReservationRoom.objects.filter(room=room, reservation__status=Reservation.CONFIRMED, reservation__check_out__gt=timezone.localdate())
        n = affected.update(room=None)
        if n:
            notes_extra = f" {n} upcoming assignment(s) were cleared — please reassign."
            t.notes = (t.notes + notes_extra).strip()
            t.save(update_fields=["notes"])
    audit.log("maintenance.created", t, user=user, request=request, new={"room": room.room_number, "problem": problem, "priority": priority},
              summary=f"Maintenance ticket for room {room.room_number}: {problem}")
    notifications.notify_after_commit("MAINTENANCE_ALERT", None, extra={"ticket": t})
    return t


@transaction.atomic
def update_ticket(ticket, user, *, status=None, assigned_to=None, notes=None, request=None):
    old = {"status": ticket.status, "assigned_to": ticket.assigned_to_id}
    if status:
        ticket.status = status
    if assigned_to is not None:
        ticket.assigned_to = assigned_to or None
    if notes is not None:
        ticket.notes = notes
    if status in ("RESOLVED", "CLOSED") and not ticket.resolved_at:
        ticket.resolved_at = timezone.now()
    ticket.save()
    room = Room.objects.select_for_update().get(pk=ticket.room_id)
    if not ticket.is_open and ticket.blocks_room and room.status == Room.MAINTENANCE:
        still = MaintenanceTicket.objects.filter(room=room, blocks_room=True, status__in=("OPEN", "IN_PROGRESS")).exclude(pk=ticket.pk).exists()
        if not still:
            room.status = Room.DIRTY  # back in service, but needs a clean before sale to a guest
            room.save(update_fields=["status"])
            HousekeepingLog.objects.create(room=room, from_status=Room.MAINTENANCE, to_status=Room.DIRTY, user=user, notes="Maintenance resolved")
    audit.log("maintenance.updated", ticket, user=user, request=request, old=old, new={"status": ticket.status, "assigned_to": ticket.assigned_to_id},
              summary=f"Ticket #{ticket.pk} → {ticket.status}")
    return ticket
