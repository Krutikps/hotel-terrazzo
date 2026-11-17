"""Staff JSON API used by the portal's interactive screens (booking wizard, calendar, pricing grid, live updates)."""
import hashlib
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from hotel.models import (DailyRate, Notification, RatePlan, Reservation, ReservationRoom, Room, RoomBlock, RoomType)
from hotel.services import audit, availability, payments, pricing, reports, reservations

from .common import as_int, body, fail, ok, parse_date, staff_api
from .guest import plan_dict, room_type_dict, search_params


@require_GET
@staff_api()
def pulse(request):
    """Cheap polling endpoint: the portal refreshes itself when `version` changes."""
    reservations.expire_holds()  # opportunistic sweep so lapsed holds are released even without cron
    last_res = Reservation.objects.aggregate(m=Max("updated_at"))["m"]
    today = timezone.localdate()
    fingerprint = "|".join([
        str(last_res.timestamp() if last_res else 0),
        ",".join(Room.objects.order_by("id").values_list("status", flat=True)),
        str(Notification.objects.filter(audience="ADMIN").count()),
        str(RoomBlock.objects.filter(released_at__isnull=True).count()),
    ])
    return ok({
        "version": hashlib.md5(fingerprint.encode()).hexdigest()[:12],
        "unread": Notification.objects.filter(audience="ADMIN", read_at__isnull=True).count(),
        "latest": list(Notification.objects.filter(audience="ADMIN").values("id", "subject", "event", "created_at")[:5]),
        "arrivals": Reservation.objects.filter(check_in=today, status=Reservation.CONFIRMED).count(),
        "departures": Reservation.objects.filter(check_out=today, status=Reservation.CHECKED_IN).count(),
        "holds": Reservation.objects.filter(status=Reservation.PENDING_PAYMENT, hold_expires_at__gt=timezone.now()).count(),
    })


@require_GET
@staff_api("availability.view")
def availability_search(request):
    ci, co, adults, children, rooms = search_params(request.GET)
    results = availability.search(ci, co, adults, children, rooms, staff=True)
    out = []
    for r in results:
        d = room_type_dict(r["room_type"])
        d["available"] = r["available"]
        d["plans"] = [plan_dict(p["plan"], p["quote"]) for p in r["plans"]]
        d["rooms"] = [{"id": x.id, "number": x.room_number, "floor": x.floor, "status": x.get_status_display(), "ready": x.status not in Room.NOT_READY}
                      for x in availability.assignable_rooms(r["room_type"].id, ci, co).order_by("room_number")]
        out.append(d)
    return ok({"results": out, "nights": (co - ci).days, "message": None if out else "No rooms available for the selected dates."})


@require_POST
@staff_api("reservations.view")
def quote(request):
    data = body(request)
    rt = RoomType.objects.get(pk=as_int(data.get("room_type"), 0, "room"))
    plan = RatePlan.objects.get(pk=as_int(data.get("rate_plan"), 0, "rate plan"))
    ci, co, adults, children, rooms = search_params(data)
    exclude = data.get("exclude_reservation")
    info = availability.room_type_availability(rt.id, ci, co, exclude_reservation_id=int(exclude) if exclude else None)
    if info["available"] < rooms:
        raise reservations.BookingError("This room is no longer available. Please select another room.", "unavailable")
    q = pricing.quote(rt, plan, ci, co, adults, children, rooms, coupon_code=(data.get("coupon") or "").strip() or None,
                      service_ids=[int(x) for x in (data.get("services") or [])], staff=True)
    return ok({"quote": q, "available": info["available"]})


@require_POST
@staff_api("reservations.create")
def create_reservation(request):
    data = body(request)
    rt = RoomType.objects.get(pk=as_int(data.get("room_type"), 0, "room"))
    plan = RatePlan.objects.get(pk=as_int(data.get("rate_plan"), 0, "rate plan"))
    ci, co, adults, children, rooms = search_params(data)
    g = data.get("guest") or {}
    if not g.get("first_name"):
        raise pricing.PricingError("Guest first name is required.", "first_name")
    source = data.get("source") if data.get("source") in dict(Reservation.SOURCES) else "RECEPTION"
    pay = data.get("payment") or {}
    with transaction.atomic():
        res = reservations.create_reservation(
            room_type=rt, rate_plan=plan, check_in=ci, check_out=co, adults=adults, children=children, rooms=rooms, guest_data=g, source=source,
            purpose=(data.get("purpose") or "")[:80], arrival_time=data.get("arrival_time") or "", special_requests=data.get("special_requests") or "",
            request_types=data.get("request_types") or [], coupon_code=(data.get("coupon") or "").strip() or None,
            service_ids=[int(x) for x in (data.get("services") or [])], user=request.user, room_ids=[int(x) for x in (data.get("room_ids") or []) if x],
            internal_notes=data.get("internal_notes") or "", request=request)
        amount = Decimal(str(pay.get("amount") or 0))
        if amount > 0:
            payments.record_offline(res, amount, pay.get("method") or "CASH", user=request.user, reference=pay.get("reference") or "", note="Taken at booking", request=request)
    return ok({"id": res.id, "booking_number": res.booking_number, "url": f"/staff/reservations/{res.id}/"}, status=201)


@require_POST
@staff_api("reservations.edit")
def modify_reservation(request, pk):
    data = body(request)
    res = Reservation.objects.get(pk=pk)
    kw = dict(check_in=parse_date(data["check_in"], "check-in date"), check_out=parse_date(data["check_out"], "check-out date"),
              adults=as_int(data.get("adults"), res.adults, "adults", 1, 30), children=as_int(data.get("children"), res.children, "children", 0, 30),
              rooms=as_int(data.get("rooms"), res.room_count, "rooms", 1, 20))
    rt = RoomType.objects.filter(pk=data.get("room_type")).first() or res.room_type
    plan = RatePlan.objects.filter(pk=data.get("rate_plan")).first() or res.rate_plan
    if data.get("preview"):
        if plan.room_type_id != rt.id:
            plan = rt.rate_plans.filter(active=True, name=res.rate_plan.name).first() or rt.rate_plans.filter(active=True).first()
        info = availability.room_type_availability(rt.id, kw["check_in"], kw["check_out"], exclude_reservation_id=res.id)
        if info["available"] < kw["rooms"]:
            raise reservations.BookingError("No availability for those dates / room type.", "unavailable")
        q = pricing.quote(rt, plan, kw["check_in"], kw["check_out"], kw["adults"], kw["children"], kw["rooms"],
                          coupon_code=res.coupon.code if res.coupon_id else None, service_ids=res.pricing_snapshot.get("service_ids", []), staff=True)
        return ok({"quote": q, "old_total": res.total, "paid": res.amount_paid, "plan_id": plan.id})
    res = reservations.modify(pk, request.user, room_type=rt, rate_plan=plan, request=request, **kw)
    return ok({"id": res.id, "total": res.total, "balance": res.balance})


@require_GET
@staff_api("availability.view")
def calendar(request):
    start = parse_date(request.GET.get("start") or timezone.localdate().isoformat(), "start date")
    n = as_int(request.GET.get("days"), 14, "days", 3, 62)
    end = start + timedelta(days=n)
    rooms = list(Room.objects.filter(active=True).select_related("room_type").order_by("room_number"))
    items = []
    rrs = (ReservationRoom.objects.filter(reservation__check_in__lt=end, reservation__check_out__gt=start,
                                          reservation__status__in=[Reservation.CONFIRMED, Reservation.CHECKED_IN, Reservation.CHECKED_OUT, Reservation.PENDING_PAYMENT])
           .filter(availability.holding_q() | Q(reservation__status=Reservation.CHECKED_OUT)).select_related("reservation__guest", "room"))
    unassigned = []
    for rr in rrs:
        r = rr.reservation
        d = {"rr": rr.id, "res": r.id, "ref": r.reference, "guest": r.guest.name, "start": r.check_in.isoformat(), "end": r.check_out.isoformat(),
             "status": r.status, "pay": r.payment_status, "balance": r.balance, "type": rr.room_type_id, "room": rr.room_id,
             "tip": f"{r.reference} · {r.guest.name} · {r.nights}n · {r.adults}A/{r.children}C · due {r.balance}"}
        (items if rr.room_id else unassigned).append(d)
    blocks = [{"id": b.id, "room": b.room_id, "start": b.start_date.isoformat(), "end": (b.end_date + timedelta(days=1)).isoformat(), "reason": b.get_reason_display(), "notes": b.notes}
              for b in RoomBlock.objects.filter(released_at__isnull=True, start_date__lt=end, end_date__gte=start)]
    return ok({"start": start.isoformat(), "days": n,
               "rooms": [{"id": r.id, "number": r.room_number, "type": r.room_type_id, "type_name": r.room_type.name, "floor": r.floor, "status": r.status,
                          "out": r.status in (Room.MAINTENANCE, Room.OUT_OF_ORDER, Room.BLOCKED)} for r in rooms],
               "items": items, "unassigned": unassigned, "blocks": blocks})


@require_POST
@staff_api("reservations.edit")
def move_booking(request, pk):
    """Calendar drag-and-drop: assign / change room. All validation happens server-side."""
    data = body(request)
    reservations.assign_room(pk, as_int(data.get("rr"), 0, "booking room"), as_int(data.get("room"), 0, "room"), request.user, request=request)
    return ok({"moved": True})


# --------------------------------------------------------------------------- pricing calendar
def _eff(rt, d, rows, rules):
    class P:  # price without any plan adjustment
        id, adjustment_type, adjustment_value = None, "NONE", Decimal("0")
    return pricing.nightly_rate(rt, P, d, rows, rules)


@require_GET
@staff_api("pricing.view")
def pricing_grid(request):
    start = parse_date(request.GET.get("start") or timezone.localdate().isoformat(), "start date")
    n = as_int(request.GET.get("days"), 14, "days", 3, 62)
    dates = [start + timedelta(days=i) for i in range(n)]
    out = []
    for rt in RoomType.objects.filter(active=True):
        rows, rules = pricing._load(rt, RatePlan(id=0), dates)
        cells = []
        for d in dates:
            r = rows.get((None, d))
            applied = [x.get_kind_display() for x in rules if x.applies_on(d, rt.id)] if not (r and r.price is not None) else []
            cells.append({"date": d.isoformat(), "price": _eff(rt, d, rows, rules), "explicit": bool(r and r.price is not None), "rules": applied,
                          "min_stay": r.min_stay if r else None, "cta": bool(r and r.closed_to_arrival), "ctd": bool(r and r.closed_to_departure), "stop": bool(r and r.stop_sell)})
        out.append({"id": rt.id, "name": rt.name, "base": rt.base_rate, "cells": cells})
    return ok({"dates": [d.isoformat() for d in dates], "types": out})


def _snap(r):
    return None if r is None else {"price": r.price, "min_stay": r.min_stay, "cta": r.closed_to_arrival, "ctd": r.closed_to_departure, "stop": r.stop_sell}


def _apply_row(rt, plan, d, data, request):
    row = DailyRate.objects.filter(room_type=rt, rate_plan=plan, date=d).first()
    old = _snap(row)
    row = row or DailyRate(room_type=rt, rate_plan=plan, date=d)
    if "price" in data:
        row.price = Decimal(str(data["price"])) if data["price"] not in (None, "") else None
        if row.price is not None and row.price < 0:
            raise pricing.PricingError("Price cannot be negative.", "price")
    if "min_stay" in data:
        row.min_stay = int(data["min_stay"]) if data["min_stay"] not in (None, "", 0) else None
    for k, f in (("cta", "closed_to_arrival"), ("ctd", "closed_to_departure"), ("stop", "stop_sell")):
        if data.get(k) is not None:
            setattr(row, f, bool(data[k]))
    empty = row.price is None and not row.min_stay and not (row.closed_to_arrival or row.closed_to_departure or row.stop_sell)
    if empty:
        if row.pk:
            row.delete()
    else:
        row.save()
    return old, _snap(None if empty else row)


@require_POST
@staff_api("pricing.manage")
def pricing_update(request):
    data = body(request)
    rt = RoomType.objects.get(pk=int(data["room_type"]))
    plan = RatePlan.objects.get(pk=int(data["rate_plan"])) if data.get("rate_plan") else None
    d = parse_date(data["date"], "date")
    try:
        old, new = _apply_row(rt, plan, d, data, request)
    except (InvalidOperation, ValueError):
        raise pricing.PricingError("Please enter a valid price.", "price")
    audit.log("price.changed", "DailyRate", f"{rt.id}:{d}", user=request.user, request=request, old=old, new=new, summary=f"{rt.name} {d}: price/restrictions changed")
    return ok({"saved": True})


@require_POST
@staff_api("pricing.manage")
@transaction.atomic
def pricing_bulk(request):
    data = body(request)
    start, end = parse_date(data["start"], "start date"), parse_date(data["end"], "end date")
    if end < start or (end - start).days > 400:
        raise pricing.PricingError("Please choose a valid date range (max 400 days).")
    types = RoomType.objects.filter(pk__in=[int(x) for x in data.get("room_types") or []])
    if not types:
        raise pricing.PricingError("Select at least one room type.")
    weekdays = {int(x) for x in data.get("weekdays") or []}
    mode, value = data.get("mode", "set"), data.get("value")
    changed = 0
    for rt in types:
        d = start
        while d <= end:
            if not weekdays or d.weekday() in weekdays:
                payload = {k: data[k] for k in ("min_stay", "cta", "ctd", "stop") if k in data}
                if mode == "clear":
                    payload["price"] = None
                elif value not in (None, ""):
                    val = Decimal(str(value))
                    if mode == "set":
                        payload["price"] = val
                    else:
                        existing = DailyRate.objects.filter(room_type=rt, rate_plan=None, date=d).first()
                        base = existing.price if existing and existing.price is not None else rt.base_rate
                        payload["price"] = max(base * (1 + val / 100) if mode == "percent" else base + val, Decimal("0")).quantize(Decimal("0.01"))
                _apply_row(rt, None, d, payload, request)
                changed += 1
            d += timedelta(days=1)
    audit.log("price.bulk_updated", "DailyRate", "bulk", user=request.user, request=request, new={k: data.get(k) for k in ("start", "end", "mode", "value", "room_types", "weekdays")},
              summary=f"Bulk price update: {changed} room-nights ({start} → {end})")
    return ok({"changed": changed})
