"""Availability engine — the one place inventory is computed.

available(room type, night) = sellable physical rooms
                              - rooms blocked that night
                              - reservation rooms that hold inventory that night
where "hold inventory" = CONFIRMED / CHECKED_IN, or PENDING_PAYMENT with an unexpired hold.
Rooms that are inactive or in MAINTENANCE / OUT_OF_ORDER / BLOCKED status are never sellable.
"""
from collections import defaultdict
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from hotel.models import Reservation, ReservationRoom, Room, RoomBlock, RoomType


def nights_between(check_in, check_out):
    d = check_in
    while d < check_out:
        yield d
        d += timedelta(days=1)


def holding_q(now=None, prefix="reservation__"):
    """Q selecting reservations that currently consume inventory."""
    now = now or timezone.now()
    return Q(**{f"{prefix}status__in": Reservation.HOLDING}) | Q(
        **{f"{prefix}status": Reservation.PENDING_PAYMENT, f"{prefix}hold_expires_at__gt": now}
    )


def sellable_rooms(room_type_id):
    return Room.objects.filter(room_type_id=room_type_id, active=True).exclude(status__in=Room.UNSELLABLE)


def booked_per_night(room_type_id, check_in, check_out, exclude_reservation_id=None):
    qs = (
        ReservationRoom.objects.filter(room_type_id=room_type_id, reservation__check_in__lt=check_out,
                                       reservation__check_out__gt=check_in)
        .filter(holding_q())
    )
    if exclude_reservation_id:
        qs = qs.exclude(reservation_id=exclude_reservation_id)
    used = defaultdict(int)
    for ci, co in qs.values_list("reservation__check_in", "reservation__check_out"):
        for d in nights_between(max(ci, check_in), min(co, check_out)):
            used[d] += 1
    return used


def blocked_per_night(room_ids, check_in, check_out):
    blocked = defaultdict(set)
    for b in RoomBlock.objects.filter(room_id__in=room_ids, released_at__isnull=True, start_date__lt=check_out,
                                      end_date__gte=check_in):
        for d in nights_between(max(b.start_date, check_in), min(b.end_date + timedelta(days=1), check_out)):
            blocked[d].add(b.room_id)
    return blocked


def room_type_availability(room_type_id, check_in, check_out, exclude_reservation_id=None):
    """Return {'by_date': {date: n}, 'raw': {date: n (may be negative = oversold)}, 'available': min, 'capacity': n}."""
    room_ids = list(sellable_rooms(room_type_id).values_list("id", flat=True))
    blocked = blocked_per_night(room_ids, check_in, check_out)
    used = booked_per_night(room_type_id, check_in, check_out, exclude_reservation_id)
    by_date, raw = {}, {}
    for d in nights_between(check_in, check_out):
        cap = len(room_ids) - len(blocked.get(d, ()))
        raw[d] = cap - used.get(d, 0)
        by_date[d] = max(raw[d], 0)
    return {"by_date": by_date, "raw": raw, "available": min(by_date.values()) if by_date else 0,
            "capacity": len(room_ids)}


def lock_inventory(room_type_ids):
    """Serialise concurrent bookings for the same room types.

    PostgreSQL: row locks via SELECT ... FOR UPDATE on the RoomType rows.
    SQLite: the connection runs BEGIN IMMEDIATE (see settings), so writers are already serialised.
    Must be called inside transaction.atomic().
    """
    list(RoomType.objects.select_for_update().filter(pk__in=sorted(set(room_type_ids))).order_by("pk"))


class Unavailable(Exception):
    pass


def assert_available(room_type_id, check_in, check_out, rooms=1, exclude_reservation_id=None):
    """Server-side revalidation used inside every inventory-changing transaction."""
    info = room_type_availability(room_type_id, check_in, check_out, exclude_reservation_id)
    if info["available"] < rooms:
        raise Unavailable("This room is no longer available. Please select another room.")
    return info


def assignable_rooms(room_type_id, check_in, check_out, exclude_reservation_id=None):
    """Physical rooms of the type that are free (sellable, unblocked, not already assigned) for the stay."""
    taken = ReservationRoom.objects.filter(room__isnull=False, reservation__check_in__lt=check_out,
                                           reservation__check_out__gt=check_in).filter(holding_q())
    if exclude_reservation_id:
        taken = taken.exclude(reservation_id=exclude_reservation_id)
    blocked = RoomBlock.objects.filter(released_at__isnull=True, start_date__lt=check_out, end_date__gte=check_in)
    return (
        sellable_rooms(room_type_id)
        .exclude(id__in=taken.values("room_id"))
        .exclude(id__in=blocked.values("room_id"))
    )


def room_conflicts(room, check_in, check_out, exclude_reservation_id=None):
    """Why this physical room can't take the stay (empty list = free)."""
    problems = []
    if not room.active:
        problems.append("Room is inactive.")
    if room.status in Room.UNSELLABLE:
        problems.append(f"Room is {room.get_status_display().lower()}.")
    q = ReservationRoom.objects.filter(room=room, reservation__check_in__lt=check_out,
                                       reservation__check_out__gt=check_in).filter(holding_q())
    if exclude_reservation_id:
        q = q.exclude(reservation_id=exclude_reservation_id)
    other = q.select_related("reservation").first()
    if other:
        problems.append(f"Room is already assigned to booking {other.reservation.reference}.")
    blk = RoomBlock.objects.filter(room=room, released_at__isnull=True, start_date__lt=check_out, end_date__gte=check_in).first()
    if blk:
        problems.append(f"Room is blocked ({blk.get_reason_display()}) {blk.start_date:%d %b}–{blk.end_date:%d %b}.")
    return problems


def search(check_in, check_out, adults, children, rooms, *, staff=False, coupon_code=None):
    """Guest-facing availability search: room types with real inventory + priced rate plans."""
    from . import pricing  # local import: pricing imports this module's helpers lazily too

    pricing.validate_dates(check_in, check_out, staff=staff)
    results = []
    for rt in RoomType.objects.filter(active=True).prefetch_related("images", "amenities"):
        try:
            pricing.validate_occupancy(rt, adults, children, rooms)
        except pricing.PricingError:
            continue
        info = room_type_availability(rt.id, check_in, check_out)
        if info["available"] < rooms:
            continue
        plans = []
        for plan in rt.rate_plans.filter(active=True):
            if plan.corporate and not staff:
                continue
            try:
                q = pricing.quote(rt, plan, check_in, check_out, adults, children, rooms, coupon_code=None, staff=staff)
            except pricing.PricingError:
                continue
            plans.append({"plan": plan, "quote": q})
        if plans:
            results.append({"room_type": rt, "available": info["available"], "plans": plans})
    return results


@transaction.atomic
def todays_inventory(day=None):
    """Counts for the dashboard: rooms by effective status on a given day."""
    day = day or timezone.localdate()
    rooms = list(Room.objects.filter(active=True).select_related("room_type"))
    blocked_ids = set(RoomBlock.objects.filter(released_at__isnull=True, start_date__lte=day, end_date__gte=day)
                      .values_list("room_id", flat=True))
    return rooms, blocked_ids
