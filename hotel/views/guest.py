"""Public website views. Pages are server-rendered; live data (availability, pricing, payment) comes from the JSON API."""
import hashlib
import json
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.core.mail import EmailMessage
from django.http import Http404, HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from hotel.api.common import rate_limit
from django.core.exceptions import ObjectDoesNotExist

from hotel.models import Amenity, Coupon, Hotel, Payment, RatePlan, Reservation, ReservationRequest, Review, RoomImage, RoomType, Service
from hotel.services import documents, notifications, pricing, reservations as rs


def _tomorrow(n=1):
    return timezone.localdate() + timedelta(days=n)


def _from_price(rt):
    """Lowest public nightly rate for the next night, computed by the pricing engine."""
    best = None
    for plan in rt.rate_plans.filter(active=True, corporate=False):
        try:
            q = pricing.quote(rt, plan, _tomorrow(), _tomorrow(2), min(rt.base_occupancy, rt.max_adults), 0, 1)
        except pricing.PricingError:
            continue
        best = q["avg_nightly"] if best is None else min(best, q["avg_nightly"])
    return best if best is not None else rt.base_rate


def _types():
    types = list(RoomType.objects.filter(active=True).prefetch_related("images", "amenities"))
    for t in types:
        t.from_price = _from_price(t)
    return types


def _search_defaults(request):
    g = request.GET
    return {"check_in": g.get("check_in") or _tomorrow().isoformat(), "check_out": g.get("check_out") or _tomorrow(3).isoformat(),
            "adults": g.get("adults", "2"), "children": g.get("children", "0"), "rooms": g.get("rooms", "1")}


def home(request):
    types = _types()
    return render(request, "guest/home.html", {"types": types, "featured": types[:3], "search": _search_defaults(request), "offers": Coupon.objects.filter(active=True, show_on_offers=True)[:3], "reviews": Review.objects.filter(show=True)[:3],
                                               "amenities": Amenity.objects.all()[:8], "page": "home"})


def rooms(request):
    return render(request, "guest/rooms.html", {"types": _types(), "search": _search_defaults(request), "page": "rooms"})


def room_detail(request, slug):
    rt = get_object_or_404(RoomType.objects.prefetch_related("images", "amenities", "rate_plans"), slug=slug, active=True)
    rt.from_price = _from_price(rt)
    plans = [p for p in rt.rate_plans.all() if p.active and not p.corporate]
    return render(request, "guest/room_detail.html", {"rt": rt, "plans": plans, "search": _search_defaults(request), "others": [t for t in _types() if t.id != rt.id][:3], "page": "rooms"})


def availability_page(request):
    return render(request, "guest/availability.html", {"search": _search_defaults(request), "page": "availability",
                                                       "preselect": request.GET.get("room", "")})


def offers(request):
    return render(request, "guest/offers.html", {"offers": Coupon.objects.filter(active=True, show_on_offers=True), "search": _search_defaults(request), "page": "offers"})


def amenities(request):
    return render(request, "guest/amenities.html", {"amenities": Amenity.objects.all(), "page": "amenities"})


def gallery(request):
    imgs = RoomImage.objects.select_related("room_type").order_by("room_type__display_order", "display_order")
    return render(request, "guest/gallery.html", {"images": imgs, "page": "gallery"})


def about(request):
    return render(request, "guest/about.html", {"page": "about"})


@rate_limit("contact")
def contact(request):
    sent = False
    if request.method == "POST":
        name, email, msg = request.POST.get("name", "").strip(), request.POST.get("email", "").strip(), request.POST.get("message", "").strip()
        if not (name and "@" in email and msg) or request.POST.get("website"):  # 'website' = honeypot
            messages.error(request, "Please fill in your name, a valid email and a message.")
        else:
            from hotel.models import Notification

            Notification.objects.create(audience="ADMIN", channel="INAPP", event="CONTACT", recipient="staff", subject=f"Enquiry from {name}",
                                        body=f"{name} <{email}>\n\n{msg}"[:3000], status="SENT")
            h = Hotel.get()
            to = h.admin_notification_email or h.email
            if to and h.notify_email:
                try:
                    EmailMessage(f"[Website enquiry] {name}", f"{name} <{email}>\n\n{msg}", settings.DEFAULT_FROM_EMAIL, [to], reply_to=[email]).send(fail_silently=True)
                except Exception:  # noqa: BLE001
                    pass
            sent = True
    return render(request, "guest/contact.html", {"sent": sent, "page": "contact"})


# --------------------------------------------------------------------------- booking flow
def checkout(request):
    """Multi-step checkout shell. All state lives server-side once the hold is created; the URL only carries the search."""
    try:
        rt = RoomType.objects.get(pk=int(request.GET.get("room_type", 0)), active=True)
        plan = RatePlan.objects.get(pk=int(request.GET.get("rate_plan", 0)), room_type=rt, active=True, corporate=False)
    except (ValueError, ObjectDoesNotExist):
        messages.error(request, "That room is no longer available. Please search again.")
        return redirect("guest:availability")
    from hotel.api.guest import plan_dict, room_type_dict

    s = _search_defaults(request)
    rt_d = room_type_dict(rt)
    plan_d = {"id": plan.id, "name": plan.name, "cancellation": plan.cancellation_text, "meal": plan.get_meal_plan_display()}
    safe_json = lambda d: json.dumps(d).replace("<", "\u003c").replace(">", "\u003e").replace("&", "\u0026")
    return render(request, "guest/checkout.html", {
        "rt": rt_d, "plan": plan_d, "rt_json": safe_json(rt_d), "plan_json": safe_json(plan_d),
        "search": s, "services": Service.objects.filter(active=True, bookable_online=True),
        "purposes": Reservation.PURPOSES, "request_types": ReservationRequest.TYPES,
        "coupon": request.GET.get("coupon", ""), "page": "book"})


def _payment_client(res):
    pay = res.payments.filter(kind=Payment.PAYMENT, status=Payment.PENDING).order_by("-created_at").first()
    if not pay:
        return None
    if pay.provider == "razorpay":
        return {"provider": "razorpay", "key": settings.RAZORPAY_KEY_ID, "order_id": pay.provider_order_id, "amount": int(pay.amount * 100), "currency": res.hotel.currency}
    return {"provider": "mock", "order_id": pay.provider_order_id, "amount": int(pay.amount * 100), "currency": res.hotel.currency, "checkout_url": f"/pay/mock/{pay.provider_order_id}/"}


def _res_by_token(token):
    res = Reservation.objects.filter(access_token=token).select_related("hotel", "guest", "room_type", "rate_plan").first()
    if not res:
        raise Http404()
    return res


def pay(request, token):
    res = _res_by_token(token)
    if res.status in (Reservation.CONFIRMED, Reservation.CHECKED_IN, Reservation.CHECKED_OUT):
        return redirect("guest:confirmation", token=token)
    client = _payment_client(res) if res.is_hold_alive else None
    pending = res.payments.filter(status=Payment.PENDING).order_by("-created_at").first()
    return render(request, "guest/pay.html", {"res": res, "client": client, "client_json": json.dumps(client) if client else "null",
                                              "amount": pending.amount if pending else None, "lines": res.lines.all(), "page": "book",
                                              "status_json": json.dumps({"state": res.status, "seconds": max(int((res.hold_expires_at - timezone.now()).total_seconds()), 0) if res.is_hold_alive else 0})})


def mock_gateway(request, order_id):
    from hotel.payments import get_provider

    if get_provider().name != "mock":
        raise Http404()
    pay_ = Payment.objects.filter(provider_order_id=order_id, provider="mock").select_related("reservation__hotel", "reservation__guest").first()
    if not pay_:
        raise Http404()
    return render(request, "guest/mock_gateway.html", {"pay": pay_, "res": pay_.reservation})


def confirmation(request, token):
    res = _res_by_token(token)
    if res.status in (Reservation.PENDING_PAYMENT, Reservation.RELEASED):
        return redirect("guest:pay", token=token)
    return render(request, "guest/confirmation.html", {"res": res, "lines": res.lines.all(), "requests": res.requests.all(), "page": "book"})


def confirmation_pdf(request, token):
    res = _res_by_token(token)
    if not res.booking_number:
        raise Http404()
    r = HttpResponse(documents.confirmation_pdf(res), content_type="application/pdf")
    r["Content-Disposition"] = f'inline; filename="booking-{res.booking_number}.pdf"'
    return r


@require_POST
@rate_limit("lookup")
def confirmation_email(request, token):
    res = _res_by_token(token)
    if res.booking_number:
        notifications.send("BOOKING_CONFIRMED", res.id)
        messages.success(request, f"Confirmation sent to {res.guest.email}.")
    return redirect("guest:confirmation" if res.status != Reservation.CANCELLED else "guest:booking", token=token)


def my_booking(request):
    return render(request, "guest/my_booking.html", {"page": "booking", "ref": request.GET.get("ref", "")})


def booking(request, token):
    res = _res_by_token(token)
    if not res.booking_number:
        raise Http404()
    fee, refund = rs.cancellation_quote(res) if res.status == Reservation.CONFIRMED else (None, None)
    can_cancel = res.status == Reservation.CONFIRMED and res.hotel.allow_guest_cancellation and timezone.localdate() < res.check_in
    return render(request, "guest/booking.html", {"res": res, "lines": res.lines.all(), "requests": res.requests.all(), "fee": fee, "refund": refund,
                                                  "can_cancel": can_cancel, "payments": res.payments.filter(status="PAID"), "page": "booking"})


# --------------------------------------------------------------------------- generated imagery
PALETTES = [("#c9825b", "#7a3b24"), ("#b7a48a", "#5c4a36"), ("#8fa08a", "#3f5342"), ("#d4b08c", "#8a5a3c"), ("#a9b4b9", "#44575f"), ("#c7a7a0", "#6e4a45")]


def placeholder(request, name):
    """Elegant generated SVG used until real photography is uploaded (Admin → Room types → Photos)."""
    h = int(hashlib.md5(name.encode()).hexdigest(), 16)
    c1, c2 = PALETTES[h % len(PALETTES)]
    label = (request.GET.get("t") or name.replace("-", " ")).replace("+", " ")[:60]
    dots = "".join(f'<circle cx="{(h >> (i * 3)) % 1200}" cy="{(h >> (i * 5 + 1)) % 800}" r="{4 + (h >> i) % 14}" fill="#fff" fill-opacity="0.{1 + (h >> i) % 3}"/>' for i in range(40))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 800" preserveAspectRatio="xMidYMid slice"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
           f'<stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/></linearGradient></defs><rect width="1200" height="800" fill="url(#g)"/>{dots}'
           f'<text x="600" y="420" text-anchor="middle" font-family="Georgia,serif" font-size="46" fill="#fff" fill-opacity="0.92">{label.replace("&", "and").replace("<", "")}</text></svg>')
    r = HttpResponse(svg, content_type="image/svg+xml")
    r["Cache-Control"] = "public, max-age=86400"
    return r
