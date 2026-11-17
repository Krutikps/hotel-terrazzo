"""Public (guest) JSON API."""
import json
from decimal import Decimal

from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from hotel.models import Hotel, RatePlan, Reservation, RoomType, Service
from hotel.payments import ProviderError, get_provider
from hotel.services import availability, payments, pricing, reservations

from .common import api, as_int, body, fail, ok, parse_date, rate_limit


def room_type_dict(rt):
    return {
        "id": rt.id, "name": rt.name, "slug": rt.slug, "short_description": rt.short_description, "description": rt.description,
        "size_sqft": rt.size_sqft, "bed_type": rt.bed_type, "max_adults": rt.max_adults, "max_children": rt.max_children,
        "max_occupancy": rt.max_occupancy, "base_occupancy": rt.base_occupancy,
        "amenities": [{"name": a.name, "icon": a.icon} for a in rt.amenities.all()],
        "images": [{"src": i.src, "caption": i.caption} for i in rt.images.all()],
        "url": f"/rooms/{rt.slug}/",
    }


def plan_dict(plan, q):
    return {"id": plan.id, "name": plan.name, "description": plan.description, "meal_plan": plan.meal_plan, "meal_label": plan.get_meal_plan_display(),
            "refundable": plan.refundable, "cancellation": plan.cancellation_text, "quote": q}


def search_params(data, staff=False):
    ci = parse_date(data.get("check_in"), "check-in date")
    co = parse_date(data.get("check_out"), "check-out date")
    adults = as_int(data.get("adults"), 2, "number of adults", 1, 30)
    children = as_int(data.get("children"), 0, "number of children", 0, 30)
    rooms = as_int(data.get("rooms"), 1, "number of rooms", 1, 20)
    return ci, co, adults, children, rooms


@require_GET
@rate_limit("search")
@api
def availability_search(request):
    ci, co, adults, children, rooms = search_params(request.GET)
    results = availability.search(ci, co, adults, children, rooms)
    out = []
    for r in results:
        d = room_type_dict(r["room_type"])
        d["available"] = r["available"]
        d["plans"] = [plan_dict(p["plan"], p["quote"]) for p in r["plans"]]
        d["from_price"] = min(p["quote"]["avg_nightly"] for p in r["plans"]) if r["plans"] else None
        out.append(d)
    msg = None if out else "No rooms available for your selected dates."
    return ok({"results": out, "nights": (co - ci).days, "message": msg})


def _resolve(data):
    rt = RoomType.objects.filter(pk=as_int(data.get("room_type"), 0, "room"), active=True).first()
    plan = RatePlan.objects.filter(pk=as_int(data.get("rate_plan"), 0, "rate plan"), active=True).first()
    if not rt or not plan:
        raise pricing.PricingError("This room is no longer available. Please select another room.")
    return rt, plan


@require_POST
@rate_limit("search")
@api
def quote(request):
    data = body(request)
    rt, plan = _resolve(data)
    ci, co, adults, children, rooms = search_params(data)
    info = availability.room_type_availability(rt.id, ci, co)
    if info["available"] < rooms:
        raise reservations.BookingError("This room is no longer available. Please select another room.", "unavailable")
    sids = [int(x) for x in (data.get("services") or [])]
    q = pricing.quote(rt, plan, ci, co, adults, children, rooms, coupon_code=(data.get("coupon") or "").strip() or None, service_ids=sids)
    return ok({"quote": q, "available": info["available"]})


@require_POST
@rate_limit("booking")
@api
def create_hold(request):
    """Guest finished checkout: create the temporary hold + payment order. Backend remains the source of truth."""
    data = body(request)
    rt, plan = _resolve(data)
    ci, co, adults, children, rooms = search_params(data)
    g = data.get("guest") or {}
    if not (g.get("first_name") and g.get("email") and g.get("phone")):
        raise pricing.PricingError("Please provide your name, email and phone number.")
    if "@" not in g["email"]:
        raise pricing.PricingError("Please enter a valid email address.", "email")
    hotel = Hotel.get()
    if hotel.require_id_online and not (g.get("id_type") and g.get("id_number")):
        raise pricing.PricingError("Please provide your ID details.")
    reqs = data.get("request_types") or []
    reservations.expire_holds()
    with transaction.atomic():
        res = reservations.create_reservation(
            room_type=rt, rate_plan=plan, check_in=ci, check_out=co, adults=adults, children=children, rooms=rooms, guest_data=g,
            source="WEBSITE", purpose=(data.get("purpose") or "")[:80], arrival_time=data.get("arrival_time") or "",
            special_requests=data.get("special_requests") or "", request_types=reqs, coupon_code=(data.get("coupon") or "").strip() or None,
            service_ids=[int(x) for x in (data.get("services") or [])], online_hold=True, request=request)
        pay = payments.start_online_payment(res, request=request)
    return ok({"token": str(res.access_token), "expires_at": res.hold_expires_at, "amount": pay["amount"], "order": pay["order"]["client"],
               "total": res.total, "payment_url": f"/pay/{res.access_token}/"}, status=201)


def _by_token(token):
    res = Reservation.objects.filter(access_token=token).select_related("hotel", "guest", "room_type", "rate_plan").first()
    if not res:
        raise reservations.BookingError("Booking not found.", "not_found")
    return res


def reservation_public(res):
    h = res.hotel
    return {"token": str(res.access_token), "booking_number": res.booking_number, "status": res.status, "payment_status": res.payment_status,
            "hold_expires_at": res.hold_expires_at, "seconds_left": max(int((res.hold_expires_at - timezone.now()).total_seconds()), 0) if res.is_hold_alive else 0,
            "total": res.total, "amount_paid": res.amount_paid, "balance": res.balance, "currency_symbol": h.currency_symbol}


@require_GET
@api
def hold_status(request, token):
    return ok(reservation_public(_by_token(token)))


@require_POST
@rate_limit("booking")
@api
def verify_payment(request):
    data = body(request)
    pay, res, outcome = payments.verify_checkout(data.get("order_id", ""), data.get("payment_id", ""), data.get("signature", ""), request=request)
    return ok({"outcome": outcome, **reservation_public(res)})


@require_POST
@rate_limit("booking")
@api
def payment_failed(request):
    data = body(request)
    out = payments.fail_checkout(data.get("order_id", ""), (data.get("reason") or "Payment cancelled or failed")[:200], request=request)
    return ok({"released": bool(out)})


@require_POST
@api
def release(request, token):
    res = _by_token(token)
    reservations.release_hold(res.id, "Guest left checkout")
    return ok({"released": True})


@require_POST
@rate_limit("booking")
@api
def restart(request, token):
    """After a failed / expired payment: build a fresh hold from the same details (if still available)."""
    old = _by_token(token)
    if old.status not in (Reservation.RELEASED, Reservation.PENDING_PAYMENT):
        raise reservations.BookingError("This booking can't be restarted.")
    if old.status == Reservation.PENDING_PAYMENT:
        reservations.release_hold(old.id, "Restarted by guest")
    g = old.guest
    gd = {f: getattr(g, f) for f in ("first_name", "last_name", "email", "phone", "address", "city", "state", "country", "postal_code", "id_type", "id_number")}
    with transaction.atomic():
        res = reservations.create_reservation(
            room_type=old.room_type, rate_plan=old.rate_plan, check_in=old.check_in, check_out=old.check_out, adults=old.adults, children=old.children,
            rooms=old.room_count, guest_data=gd, source="WEBSITE", purpose=old.purpose, arrival_time=old.arrival_time,
            special_requests=old.special_requests, request_types=[r.request_type for r in old.requests.all()],
            coupon_code=old.coupon.code if old.coupon_id else None, service_ids=old.pricing_snapshot.get("service_ids", []), online_hold=True, request=request)
        pay = payments.start_online_payment(res, request=request)
    return ok({"token": str(res.access_token), "payment_url": f"/pay/{res.access_token}/", "order": pay["order"]["client"], "amount": pay["amount"]}, status=201)


@csrf_exempt
@require_POST
def webhook(request, provider):
    """Provider → us. Signature-verified; idempotent; independent of the browser."""
    try:
        outcome = payments.handle_webhook(provider, request.body, request.headers)
    except ProviderError:
        return HttpResponse(status=400)
    except Exception:  # noqa: BLE001
        return HttpResponse(status=500)
    return HttpResponse(outcome, status=200)


@require_POST
@rate_limit("lookup")
@api
def lookup_booking(request):
    data = body(request)
    ref = (data.get("booking_number") or "").strip().upper()
    contact = (data.get("contact") or "").strip().lower()
    res = Reservation.objects.filter(booking_number__iexact=ref).select_related("guest").first() if ref else None
    digits = lambda x: "".join(ch for ch in x if ch.isdigit())
    match = bool(res and contact and (contact == res.guest.email.lower() or (len(digits(contact)) >= 7 and digits(contact)[-10:] == digits(res.guest.phone)[-10:])))
    if not match:
        return fail("We couldn't find a booking with those details. Please check the booking number and email/phone.", 404, "not_found")
    return ok({"url": f"/my-booking/{res.access_token}/"})


@require_POST
@rate_limit("lookup")
@api
def cancel_booking(request, token):
    res = _by_token(token)
    data = body(request)
    fee, refund = reservations.cancellation_quote(res)
    if data.get("preview"):
        return ok({"fee": fee, "refund": refund, "currency_symbol": res.hotel.currency_symbol})
    reservations.cancel(res.id, reason=(data.get("reason") or "Cancelled by guest")[:200], by_guest=True, request=request)
    res.refresh_from_db()
    return ok({"status": res.status, "fee": fee, "refund": refund})


@require_POST
@api
def mock_gateway_result(request, order_id):
    """Sandbox gateway callback: signs the result server-side, exactly as a hosted gateway would."""
    from hotel.payments.mock import sign
    import uuid

    data = body(request)
    if get_provider().name != "mock":
        return fail("Not available.", 404)
    if data.get("result") == "success":
        pid = f"mock_pay_{uuid.uuid4().hex[:16]}"
        pay, res, outcome = payments.verify_checkout(order_id, pid, sign(order_id, pid, "PAID"), request=request)
        return ok({"outcome": outcome, **reservation_public(res)})
    out = payments.fail_checkout(order_id, "Payment declined or cancelled", request=request)
    return ok({"outcome": "failed"})
