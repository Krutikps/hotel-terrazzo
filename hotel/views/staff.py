"""Staff (HMS) portal views. Every view is permission-gated; business logic lives in hotel.services."""
import functools
import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout, get_user_model
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import models as dj
from django.db.models import Count, F, Q, Sum
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import Role
from hotel import forms as F_
from hotel.api.common import rate_limit
from hotel.models import (Amenity, AuditLog, Coupon, Guest, Hotel, HousekeepingLog, Invoice, MaintenanceTicket, Notification, Payment, PricingRule,
                          RatePlan, Reservation, ReservationLine, Review, ReservationRequest, ReservationRoom, Room, RoomBlock, RoomImage, RoomType, Service, TaxRule)
from hotel.services import audit, availability, documents, notifications, payments, pricing, reports, reservations as rs

User = get_user_model()


# --------------------------------------------------------------------------- plumbing
def staff_view(perm=None):
    def deco(view):
        @functools.wraps(view)
        def inner(request, *a, **kw):
            if not request.user.is_authenticated:
                from django.contrib.auth.views import redirect_to_login

                return redirect_to_login(request.get_full_path(), "/staff/login/")
            if perm and not request.user.can(perm):
                return render(request, "staff/forbidden.html", {"perm": perm}, status=403)
            return view(request, *a, **kw)
        return inner
    return deco


def safe(request, fn, success=None, redirect_to=None):
    """Run a service call; flash friendly errors instead of raising."""
    try:
        out = fn()
        if success:
            messages.success(request, success)
        return out, True
    except (rs.BookingError, pricing.PricingError) as e:
        messages.error(request, e.message)
    except ValidationError as e:
        messages.error(request, " ".join(e.messages))
    except (InvalidOperation, ValueError):
        messages.error(request, "Please check the values you entered.")
    return None, False


def back(request, default="staff:dashboard", **kw):
    nxt = request.POST.get("next") or request.GET.get("next")
    if nxt and nxt.startswith("/staff/"):
        return redirect(nxt)
    return redirect(default, **kw)


def dec(v, default="0"):
    try:
        return Decimal(str(v).strip() or default)
    except InvalidOperation:
        raise pricing.PricingError("Please enter a valid amount.")


def pdate(v, default=None):
    from datetime import datetime

    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- auth
@rate_limit("login")
def login_view(request):
    if request.user.is_authenticated:
        return redirect("staff:dashboard")
    error = ""
    if request.method == "POST":
        user = authenticate(request, username=request.POST.get("email", "").strip().lower(), password=request.POST.get("password", ""))
        if user and user.is_active and (user.role or user.is_superuser):
            auth_login(request, user)
            audit.log("auth.login", user, user=user, request=request, summary=f"{user.email} signed in")
            nxt = request.POST.get("next") or request.GET.get("next") or ""
            return redirect(nxt if nxt.startswith("/staff/") else "staff:dashboard")
        error = "Incorrect email or password."
        audit.log("auth.login_failed", "User", request.POST.get("email", "")[:40], request=request, actor_label="Anonymous", summary="Failed sign-in attempt")
    return render(request, "staff/login.html", {"error": error, "next": request.GET.get("next", "")})


@require_POST
def logout_view(request):
    if request.user.is_authenticated:
        audit.log("auth.logout", request.user, request=request, summary=f"{request.user.email} signed out")
    auth_logout(request)
    return redirect("staff:login")


# --------------------------------------------------------------------------- dashboard
@staff_view("dashboard.view")
def dashboard(request):
    today = timezone.localdate()
    stats = reports.dashboard(today)
    arrivals = (Reservation.objects.filter(check_in=today, status__in=(Reservation.CONFIRMED, Reservation.CHECKED_IN)).select_related("guest", "room_type")
                .prefetch_related("rooms__room"))
    departures = (Reservation.objects.filter(check_out=today, status__in=(Reservation.CHECKED_IN, Reservation.CHECKED_OUT)).select_related("guest", "room_type")
                  .prefetch_related("rooms__room"))
    upcoming = (Reservation.objects.filter(check_in__gt=today, status=Reservation.CONFIRMED).select_related("guest", "room_type").prefetch_related("rooms__room").order_by("check_in")[:8])
    ctx = {"s": stats, "arrivals": arrivals, "departures": departures, "upcoming": upcoming, "charts": json.dumps(reports.dashboard_charts(today)),
           "holds": Reservation.objects.filter(status=Reservation.PENDING_PAYMENT, hold_expires_at__gt=timezone.now()).count(),
           "recent": Notification.objects.filter(audience="ADMIN")[:6], "today": today}
    return render(request, "staff/dashboard.html", ctx)


# --------------------------------------------------------------------------- reservations
def _res_qs():
    return Reservation.objects.select_related("guest", "room_type", "rate_plan").prefetch_related("rooms__room", "requests")


@staff_view("reservations.view")
def reservations_list(request):
    g = request.GET
    today = timezone.localdate()
    view = g.get("view", "upcoming")
    qs = _res_qs().exclude(status__in=(Reservation.RELEASED,))
    if view == "upcoming":
        qs = qs.filter(check_in__gte=today, status__in=(Reservation.CONFIRMED, Reservation.PENDING_PAYMENT)).order_by("check_in", "id")
    elif view == "inhouse":
        qs = qs.filter(status=Reservation.CHECKED_IN).order_by("check_out")
    elif view == "pending":
        qs = qs.filter(status__in=(Reservation.CONFIRMED, Reservation.CHECKED_IN)).filter(total__gt=F("amount_paid")).order_by("check_in")
    else:
        qs = qs.order_by("-created_at")
    q = g.get("q", "").strip()
    if q:
        qs = qs.filter(Q(booking_number__icontains=q) | Q(guest__first_name__icontains=q) | Q(guest__last_name__icontains=q) | Q(guest__phone__icontains=q) |
                       Q(guest__email__icontains=q) | Q(rooms__room__room_number__iexact=q)).distinct()
    for key, field in (("status", "status"), ("payment", "payment_status"), ("source", "source"), ("room_type", "room_type_id")):
        if g.get(key):
            qs = qs.filter(**{field: g[key]})
    if pdate(g.get("from")):
        qs = qs.filter(check_in__gte=pdate(g["from"]))
    if pdate(g.get("to")):
        qs = qs.filter(check_in__lte=pdate(g["to"]))
    page = Paginator(qs, 25).get_page(g.get("page"))
    return render(request, "staff/reservations.html", {
        "page_obj": page, "view": view, "q": q, "f": g, "statuses": Reservation.STATUS, "pay_statuses": Reservation.PAYMENT_STATUS, "sources": Reservation.SOURCES,
        "room_types": RoomType.objects.all(), "query": "&".join(f"{k}={v}" for k, v in g.items() if k != "page" and v)})


@staff_view("reservations.view")
def reservation_detail(request, pk):
    res = get_object_or_404(_res_qs().prefetch_related("lines", "payments__refunds", "invoices"), pk=pk)
    history = AuditLog.objects.filter(entity_type="Reservation", entity_id=str(res.pk)).select_related("user")[:60]
    free = {}
    for rr in res.rooms.all():
        if res.status in (Reservation.CONFIRMED, Reservation.CHECKED_IN):
            free[rr.id] = availability.assignable_rooms(rr.room_type_id, res.check_in, res.check_out, exclude_reservation_id=res.id).order_by("room_number")
    fee, refund = rs.cancellation_quote(res) if res.status == Reservation.CONFIRMED else (None, None)
    return render(request, "staff/reservation_detail.html", {
        "res": res, "history": history, "free_rooms": free, "fee": fee, "refund": refund, "today": timezone.localdate(),
        "methods": [m for m in Payment.METHODS if m[0] != "ONLINE"], "notifs": res.notifications.all()[:10],
        "charge_cats": [("MINIBAR", "Minibar"), ("LATE_CHECKOUT", "Late checkout"), ("SERVICE", "Service"), ("OTHER", "Other")],
        "request_types": ReservationRequest.TYPES, "purposes": Reservation.PURPOSES, "sources": Reservation.SOURCES, "guests_total": res.adults + res.children})


@staff_view("reservations.create")
def reservation_new(request):
    t = timezone.localdate()
    return render(request, "staff/reservation_new.html", {
        "defaults": {"check_in": t.isoformat(), "check_out": (t + timedelta(days=1)).isoformat()}, "sources": Reservation.SOURCES,
        "purposes": Reservation.PURPOSES, "request_types": ReservationRequest.TYPES, "services": Service.objects.filter(active=True),
        "methods": [m for m in Payment.METHODS if m[0] != "ONLINE"]})


@staff_view("reservations.edit")
def reservation_edit(request, pk):
    res = get_object_or_404(_res_qs(), pk=pk)
    if res.status not in (Reservation.CONFIRMED, Reservation.CHECKED_IN):
        messages.error(request, "Only confirmed or checked-in bookings can be modified.")
        return redirect("staff:reservation", pk=pk)
    return render(request, "staff/reservation_edit.html", {"res": res, "room_types": RoomType.objects.filter(active=True), "plans": RatePlan.objects.filter(active=True).select_related("room_type")})


def _res_post(perm, success=None):
    """Decorator for POST-only reservation actions."""
    def deco(fn):
        @staff_view(perm)
        @require_POST
        @functools.wraps(fn)
        def inner(request, pk, *a, **kw):
            res = get_object_or_404(Reservation, pk=pk)
            safe(request, lambda: fn(request, res, *a, **kw), success)
            return back(request, "staff:reservation", pk=pk)
        return inner
    return deco


@_res_post("reservations.edit", "Details saved.")
def res_details(request, res):
    p = request.POST
    rs.update_details(res, request.user, purpose=p.get("purpose", res.purpose), arrival_time=p.get("arrival_time", res.arrival_time),
                      special_requests=p.get("special_requests", res.special_requests), internal_notes=p.get("internal_notes", res.internal_notes),
                      source=p.get("source", res.source), request=request)


@_res_post("reservations.edit", "Request added.")
def res_add_request(request, res):
    t = request.POST.get("request_type", "").strip()
    if not t:
        raise rs.BookingError("Choose a request type.")
    ReservationRequest.objects.create(reservation=res, request_type=t[:60], notes=request.POST.get("notes", "")[:300])
    audit.log("reservation.request_added", res, user=request.user, request=request, new={"request": t}, summary=f"Special request '{t}' added")


@_res_post("reservations.edit", "Request updated.")
def res_toggle_request(request, res, rid):
    r = get_object_or_404(ReservationRequest, pk=rid, reservation=res)
    r.fulfilled = not r.fulfilled
    r.save(update_fields=["fulfilled"])


@_res_post("reservations.edit", "Room assigned.")
def res_assign(request, res):
    rs.assign_room(res.id, int(request.POST["rr"]), int(request.POST["room"]), request.user, request=request)


@_res_post("reservations.edit", "Room unassigned.")
def res_unassign(request, res):
    rs.unassign_room(res.id, int(request.POST["rr"]), request.user, request=request)


@_res_post("payments.add", "Payment recorded.")
def res_add_payment(request, res):
    payments.record_offline(res, dec(request.POST.get("amount")), request.POST.get("method", "CASH"), user=request.user, reference=request.POST.get("reference", ""),
                            note=request.POST.get("note", ""), request=request)


@_res_post("reservations.edit", "Charge added.")
def res_add_charge(request, res):
    if res.status not in (Reservation.CONFIRMED, Reservation.CHECKED_IN):
        raise rs.BookingError("Charges can only be added to active bookings.")
    cat = request.POST.get("category", "OTHER")
    rs.add_charge(res, cat if cat in dict(ReservationLine.CATS) else "OTHER", request.POST.get("description", "").strip() or "Additional charge",
                  dec(request.POST.get("amount")), user=request.user, taxable=bool(request.POST.get("taxable")), request=request)


@_res_post("reservations.cancel")
def res_cancel(request, res):
    waive = bool(request.POST.get("waive_fee")) and request.user.can("payments.refund")
    out = rs.cancel(res.id, user=request.user, reason=request.POST.get("reason", "Cancelled by staff"), waive_fee=waive, request=request)
    messages.success(request, f"Booking {res.reference} cancelled.")


@_res_post("checkin.manage")
def res_checkin(request, res):
    p = request.POST
    rs.check_in(res.id, request.user, collect_amount=dec(p.get("collect")), method=p.get("method", "CASH"), reference=p.get("reference", ""), notes=p.get("notes", ""),
                id_type=p.get("id_type", ""), id_number=p.get("id_number", ""), request=request)
    messages.success(request, f"{res.guest.name} checked in.")


@_res_post("checkout.manage")
def res_checkout(request, res):
    p = request.POST
    charges = []
    for i in range(1, 4):
        amt = p.get(f"charge_amount_{i}", "").strip()
        if amt:
            cat = p.get(f"charge_cat_{i}", "OTHER")
            charges.append({"category": cat if cat in dict(ReservationLine.CATS) else "OTHER", "description": p.get(f"charge_desc_{i}", "").strip() or dict(ReservationLine.CATS).get(cat, "Charge"),
                            "amount": dec(amt), "taxable": True})
    r, inv = rs.check_out(res.id, request.user, charges=charges, collect_amount=dec(p.get("collect")), method=p.get("method", "CASH"), reference=p.get("reference", ""),
                          allow_unpaid=bool(p.get("allow_unpaid")) and request.user.can("payments.refund"), request=request)
    messages.success(request, f"{res.guest.name} checked out. Invoice {inv.number} generated.")


@staff_view("reservations.view")
@require_POST
def res_email(request, pk):
    res = get_object_or_404(Reservation, pk=pk)
    if not res.booking_number:
        messages.error(request, "This booking is not confirmed yet.")
    else:
        notifications.send("BOOKING_CONFIRMED", res.id)
        audit.log("reservation.emailed", res, user=request.user, request=request, summary=f"Confirmation emailed to {res.guest.email}")
        messages.success(request, f"Confirmation emailed to {res.guest.email or 'guest'}.")
    return back(request, "staff:reservation", pk=pk)


@staff_view("reservations.view")
def res_print(request, pk):
    res = get_object_or_404(_res_qs().prefetch_related("lines", "payments"), pk=pk)
    return render(request, "staff/print_reservation.html", {"res": res})


@staff_view("reservations.view")
def res_pdf(request, pk):
    res = get_object_or_404(Reservation, pk=pk)
    r = HttpResponse(documents.confirmation_pdf(res), content_type="application/pdf")
    r["Content-Disposition"] = f'inline; filename="booking-{res.reference}.pdf"'
    return r


# --------------------------------------------------------------------------- front desk
@staff_view("checkin.manage")
def arrivals(request):
    day = pdate(request.GET.get("date"), timezone.localdate())
    qs = _res_qs().filter(check_in=day, status__in=(Reservation.CONFIRMED, Reservation.CHECKED_IN)).order_by("status", "guest__first_name")
    ready = {}
    for r in qs:
        ready[r.id] = [(rr, rr.room) for rr in r.rooms.all()]
    return render(request, "staff/arrivals.html", {"items": qs, "day": day, "today": timezone.localdate(), "methods": [m for m in Payment.METHODS if m[0] != "ONLINE"],
                                                   "id_types": ["Aadhaar", "Passport", "Driving licence", "Voter ID", "Other"]})


@staff_view("checkout.manage")
def departures(request):
    day = pdate(request.GET.get("date"), timezone.localdate())
    qs = _res_qs().filter(check_out=day, status__in=(Reservation.CHECKED_IN, Reservation.CHECKED_OUT)).order_by("status", "guest__first_name")
    overdue = _res_qs().filter(check_out__lt=timezone.localdate(), status=Reservation.CHECKED_IN)
    return render(request, "staff/departures.html", {"items": qs, "overdue": overdue, "day": day, "methods": [m for m in Payment.METHODS if m[0] != "ONLINE"],
                                                     "charge_cats": [("MINIBAR", "Minibar"), ("LATE_CHECKOUT", "Late checkout"), ("SERVICE", "Service"), ("OTHER", "Other")]})


@staff_view("availability.view")
def calendar_view(request):
    start = pdate(request.GET.get("start"), timezone.localdate() - timedelta(days=1))
    return render(request, "staff/calendar.html", {"start": start, "can_edit": request.user.can("reservations.edit"), "can_block": request.user.can("rooms.block"), "block_form": F_.BlockForm()})


# --------------------------------------------------------------------------- guests
@staff_view("guests.view")
def guests_list(request):
    g = request.GET
    qs = Guest.objects.annotate(stays=Count("reservations", filter=Q(reservations__status__in=("CONFIRMED", "CHECKED_IN", "CHECKED_OUT"))),
                                spend=Sum("reservations__amount_paid"))
    for f, field in (("name", None), ("phone", "phone__icontains"), ("email", "email__icontains")):
        v = g.get(f, "").strip()
        if v:
            qs = qs.filter(Q(first_name__icontains=v) | Q(last_name__icontains=v)) if f == "name" else qs.filter(**{field: v})
    page = Paginator(qs.order_by("-created_at"), 25).get_page(g.get("page"))
    return render(request, "staff/guests.html", {"page_obj": page, "f": g})


@staff_view("guests.view")
def guest_detail(request, pk):
    guest = get_object_or_404(Guest, pk=pk)
    res = guest.reservations.select_related("room_type").prefetch_related("rooms__room").order_by("-check_in")
    pays = Payment.objects.filter(reservation__guest=guest).select_related("reservation")
    return render(request, "staff/guest_detail.html", {
        "guest": guest, "current": res.filter(status=Reservation.CHECKED_IN), "upcoming": res.filter(status=Reservation.CONFIRMED, check_in__gte=timezone.localdate()),
        "history": res.filter(status__in=(Reservation.CHECKED_OUT, Reservation.NO_SHOW)), "cancelled": res.filter(status=Reservation.CANCELLED), "payments": pays,
        "total_stays": res.filter(status=Reservation.CHECKED_OUT).count(), "spend": sum((p.amount if p.kind == "PAYMENT" else -p.amount for p in pays if p.status == "PAID"), Decimal("0"))})


@staff_view("guests.edit")
def guest_edit(request, pk):
    guest = get_object_or_404(Guest, pk=pk)
    form = F_.GuestForm(request.POST or None, instance=guest)
    if request.method == "POST" and form.is_valid():
        old = {k: str(v) for k, v in guest.__dict__.items() if not k.startswith("_")}
        form.save()
        audit.log("guest.updated", guest, user=request.user, request=request, old=old, new=form.cleaned_data, summary=f"Guest {guest.name} updated")
        messages.success(request, "Guest saved.")
        return redirect("staff:guest", pk=pk)
    return render(request, "staff/form.html", {"form": form, "title": f"Edit guest — {guest.name}", "back": f"/staff/guests/{pk}/"})


# --------------------------------------------------------------------------- rooms
@staff_view("rooms.view")
def rooms_list(request):
    g = request.GET
    today = timezone.localdate()
    states, blocked = reports.today_room_states(today)
    qs = Room.objects.select_related("room_type").order_by("room_number")
    if g.get("q"):
        qs = qs.filter(room_number__icontains=g["q"].strip())
    for k, field in (("type", "room_type_id"), ("floor", "floor"), ("status", "status")):
        if g.get(k):
            qs = qs.filter(**{field: g[k]})
    occ = {rr.room_id: rr.reservation for rr in ReservationRoom.objects.filter(reservation__status=Reservation.CHECKED_IN, room__isnull=False).select_related("reservation__guest")}
    rows = [{"room": r, "state": states.get(r.id, "INACTIVE"), "stay": occ.get(r.id), "blocked": r.id in blocked} for r in qs]
    if g.get("effective"):
        rows = [x for x in rows if x["state"] == g["effective"]]
    return render(request, "staff/rooms.html", {"rows": rows, "room_types": RoomType.objects.all(), "f": g, "statuses": Room.STATUS, "floors": sorted(set(Room.objects.values_list("floor", flat=True))),
                                               "reasons": RoomBlock.REASONS, "today": today, "can_manage": request.user.can("rooms.manage"), "can_block": request.user.can("rooms.block"),
                                               "can_status": request.user.can("rooms.status"), "block_form": F_.BlockForm()})


@staff_view("rooms.status")
@require_POST
def room_status(request, pk):
    room = get_object_or_404(Room, pk=pk)
    safe(request, lambda: rs.set_room_status(room, request.POST.get("status", ""), request.user, request.POST.get("notes", ""), request=request), f"Room {room.room_number} updated.")
    return back(request, "staff:rooms")


@staff_view("rooms.block")
@require_POST
def room_block(request):
    form = F_.BlockForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        safe(request, lambda: rs.block_room(d["room"], d["start_date"], d["end_date"], d["reason"], d["notes"], request.user, request=request), f"Room {d['room'].room_number} blocked.")
    else:
        messages.error(request, "Please choose a room and valid dates.")
    return back(request, "staff:blocks")


@staff_view("rooms.block")
@require_POST
def room_unblock(request, pk):
    block = get_object_or_404(RoomBlock, pk=pk)
    safe(request, lambda: rs.unblock_room(block, request.user, request=request), f"Room {block.room.room_number} unblocked.")
    return back(request, "staff:blocks")


@staff_view("rooms.view")
def blocks_list(request):
    return render(request, "staff/blocks.html", {"active": RoomBlock.objects.filter(released_at__isnull=True).select_related("room__room_type", "created_by").order_by("start_date"),
                                                "past": RoomBlock.objects.filter(released_at__isnull=False).select_related("room", "created_by")[:20], "form": F_.BlockForm(),
                                                "can_block": request.user.can("rooms.block")})


@staff_view("housekeeping.manage")
def housekeeping(request):
    today = timezone.localdate()
    states, _ = reports.today_room_states(today)
    rooms = list(Room.objects.filter(active=True).select_related("room_type").order_by("room_number"))
    cols = {k: [] for k in ("DIRTY", "CLEANING", "CLEAN", "INSPECTED")}
    for r in rooms:
        if r.status in cols:
            cols[r.status].append(r)
    other = [r for r in rooms if r.status not in cols]
    arriving = set(ReservationRoom.objects.filter(reservation__check_in=today, reservation__status=Reservation.CONFIRMED, room__isnull=False).values_list("room_id", flat=True))
    logs = HousekeepingLog.objects.select_related("room", "user")[:15]
    return render(request, "staff/housekeeping.html", {"cols": cols, "other": other, "arriving": arriving, "logs": logs, "states": states})


@staff_view("rooms.manage")
def room_form(request, pk=None):
    inst = get_object_or_404(Room, pk=pk) if pk else None
    form = F_.RoomForm(request.POST or None, instance=inst)
    if request.method == "POST" and form.is_valid():
        old = {"room": inst.room_number, "type": inst.room_type_id, "status": inst.status, "active": inst.active} if inst else None
        obj = form.save()
        audit.log("room.updated" if inst else "room.created", obj, user=request.user, request=request, old=old, new=form.cleaned_data, summary=f"Room {obj.room_number} saved")
        messages.success(request, "Room saved.")
        return redirect("staff:rooms")
    return render(request, "staff/form.html", {"form": form, "title": "Edit room" if inst else "Add room", "back": "/staff/rooms/"})


# --------------------------------------------------------------------------- maintenance
@staff_view("maintenance.report")
def maintenance(request):
    st = request.GET.get("status", "open")
    qs = MaintenanceTicket.objects.select_related("room", "assigned_to", "reported_by")
    qs = qs.filter(status__in=("OPEN", "IN_PROGRESS")) if st == "open" else qs.filter(status__in=("RESOLVED", "CLOSED")) if st == "done" else qs
    return render(request, "staff/maintenance.html", {"tickets": qs, "st": st, "form": F_.TicketForm(), "can_manage": request.user.can("maintenance.manage"),
                                                     "staff_users": User.objects.filter(is_active=True), "statuses": MaintenanceTicket.STATUSES})


@staff_view("maintenance.report")
@require_POST
def ticket_create(request):
    form = F_.TicketForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        safe(request, lambda: rs.create_ticket(d["room"], d["problem"], d["priority"], request.user, assigned_to=d["assigned_to"], notes=d["notes"], blocks_room=d["blocks_room"], request=request),
             "Ticket created.")
    else:
        messages.error(request, "Please complete the ticket form.")
    return redirect("staff:maintenance")


@staff_view("maintenance.manage")
@require_POST
def ticket_update(request, pk):
    t = get_object_or_404(MaintenanceTicket, pk=pk)
    uid = request.POST.get("assigned_to")
    safe(request, lambda: rs.update_ticket(t, request.user, status=request.POST.get("status") or None, assigned_to=User.objects.filter(pk=uid).first() if uid is not None else None,
                                           notes=request.POST.get("notes") if "notes" in request.POST else None, request=request), "Ticket updated.")
    return redirect("staff:maintenance")


# --------------------------------------------------------------------------- payments & invoices
@staff_view("payments.view")
def payments_list(request):
    g = request.GET
    qs = Payment.objects.select_related("reservation__guest").order_by("-created_at")
    for k, field in (("status", "status"), ("method", "method"), ("kind", "kind")):
        if g.get(k):
            qs = qs.filter(**{field: g[k]})
    if g.get("q"):
        qs = qs.filter(Q(reservation__booking_number__icontains=g["q"]) | Q(transaction_id__icontains=g["q"]) | Q(provider_payment_id__icontains=g["q"]) | Q(reservation__guest__first_name__icontains=g["q"]) | Q(reservation__guest__last_name__icontains=g["q"]))
    if pdate(g.get("from")):
        qs = qs.filter(created_at__date__gte=pdate(g["from"]))
    if pdate(g.get("to")):
        qs = qs.filter(created_at__date__lte=pdate(g["to"]))
    page = Paginator(qs, 30).get_page(g.get("page"))
    return render(request, "staff/payments.html", {"page_obj": page, "f": g, "statuses": Payment.STATUS, "methods": Payment.METHODS,
                                                  "can_refund": request.user.can("payments.refund"),
                                                  "pending": Reservation.objects.filter(status__in=("CONFIRMED", "CHECKED_IN"), total__gt=F("amount_paid")).count()})


@staff_view("payments.refund")
@require_POST
def payment_refund(request, pk):
    p = get_object_or_404(Payment, pk=pk)

    def go():
        ref = payments.refund_payment(p, dec(request.POST.get("amount")), user=request.user, reason=request.POST.get("reason", "Refund"), request=request)
        if ref.status == Payment.FAILED:
            raise rs.BookingError(ref.note)
        return ref

    safe(request, go, "Refund issued.")
    return back(request, "staff:payments")


@staff_view("payments.view")
def payment_receipt(request, pk):
    p = get_object_or_404(Payment.objects.select_related("reservation__guest", "reservation__hotel"), pk=pk)
    return render(request, "staff/receipt.html", {"p": p, "res": p.reservation})


@staff_view("invoices.view")
def invoices_list(request):
    q = request.GET.get("q", "").strip()
    qs = Invoice.objects.select_related("reservation__guest")
    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(reservation__booking_number__icontains=q) | Q(reservation__guest__first_name__icontains=q) | Q(reservation__guest__last_name__icontains=q))
    page = Paginator(qs, 30).get_page(request.GET.get("page"))
    return render(request, "staff/invoices.html", {"page_obj": page, "q": q})


@staff_view("invoices.view")
@require_POST
def invoice_generate(request, pk):
    res = get_object_or_404(Reservation, pk=pk)
    if res.status not in (Reservation.CONFIRMED, Reservation.CHECKED_IN, Reservation.CHECKED_OUT):
        messages.error(request, "Invoices can only be generated for active or completed bookings.")
        return back(request, "staff:reservation", pk=pk)
    inv = rs.create_invoice(res, request.user)
    return redirect("staff:invoice", pk=inv.pk)


@staff_view("invoices.view")
def invoice_detail(request, pk):
    inv = get_object_or_404(Invoice.objects.select_related("reservation__guest"), pk=pk)
    return render(request, "staff/invoice.html", {"inv": inv, "d": inv.data})


@staff_view("invoices.view")
def invoice_pdf(request, pk):
    inv = get_object_or_404(Invoice, pk=pk)
    r = HttpResponse(documents.invoice_pdf(inv), content_type="application/pdf")
    r["Content-Disposition"] = f'inline; filename="{inv.number}.pdf"'
    return r


@staff_view("invoices.view")
@require_POST
def invoice_email(request, pk):
    from django.conf import settings
    from django.core.mail import EmailMessage

    inv = get_object_or_404(Invoice.objects.select_related("reservation__guest"), pk=pk)
    to = inv.reservation.guest.email
    if not to:
        messages.error(request, "This guest has no email address.")
    else:
        m = EmailMessage(f"Invoice {inv.number} – {inv.data['hotel']['name']}", f"Dear {inv.reservation.guest.name},\n\nPlease find your invoice attached.\n\n{inv.data['hotel']['name']}",
                         settings.DEFAULT_FROM_EMAIL, [to])
        m.attach(f"{inv.number}.pdf", documents.invoice_pdf(inv), "application/pdf")
        try:
            m.send()
            audit.log("invoice.emailed", inv, user=request.user, request=request, summary=f"Invoice {inv.number} emailed to {to}")
            messages.success(request, f"Invoice emailed to {to}.")
        except Exception:  # noqa: BLE001
            messages.error(request, "The email could not be sent. Check the email settings.")
    return redirect("staff:invoice", pk=pk)


# --------------------------------------------------------------------------- pricing
@staff_view("pricing.view")
def pricing_calendar(request):
    start = pdate(request.GET.get("start"), timezone.localdate())
    return render(request, "staff/pricing_calendar.html", {"start": start, "room_types": RoomType.objects.filter(active=True), "can_edit": request.user.can("pricing.manage")})


# --------------------------------------------------------------------------- reports
@staff_view("reports.view")
def reports_view(request):
    g = request.GET
    key = g.get("report", "occupancy")
    if key not in reports.REPORTS:
        raise Http404()
    today = timezone.localdate()
    start = pdate(g.get("from"), today - timedelta(days=29))
    end = pdate(g.get("to"), today)
    if end < start:
        start, end = end, start
    if (end - start).days > 400:
        end = start + timedelta(days=400)
    rt = g.get("room_type") or None
    rep = reports.run(key, start, end, rt)
    fmt = g.get("export")
    if fmt:
        name = f"{key}_{start}_{end}"
        audit.log("report.exported", "Report", key, user=request.user, request=request, new={"format": fmt, "from": start, "to": end}, summary=f"{key} report exported as {fmt}")
        if fmt == "csv":
            r = HttpResponse(reports.to_csv(rep), content_type="text/csv; charset=utf-8")
        elif fmt == "xlsx":
            r = HttpResponse(reports.to_xlsx(rep), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        elif fmt == "pdf":
            r = HttpResponse(reports.to_pdf(rep, f"{start} → {end}"), content_type="application/pdf")
        else:
            raise Http404()
        r["Content-Disposition"] = f'attachment; filename="{name}.{fmt}"'
        return r
    return render(request, "staff/reports.html", {"rep": rep, "key": key, "reports": [(k, v[0]) for k, v in reports.REPORTS.items()], "start": start, "end": end,
                                                 "room_types": RoomType.objects.all(), "rt": rt, "qs": f"from={start}&to={end}&room_type={rt or ''}"})


# --------------------------------------------------------------------------- audit & notifications
@staff_view("audit.view")
def audit_log(request):
    g = request.GET
    qs = AuditLog.objects.select_related("user")
    if g.get("q"):
        qs = qs.filter(Q(summary__icontains=g["q"]) | Q(entity_id__icontains=g["q"]) | Q(action__icontains=g["q"]))
    for k, field in (("action", "action__startswith"), ("user", "user_id"), ("entity", "entity_type")):
        if g.get(k):
            qs = qs.filter(**{field: g[k]})
    page = Paginator(qs, 40).get_page(g.get("page"))
    return render(request, "staff/audit.html", {"page_obj": page, "f": g, "users": User.objects.all(), "entities": AuditLog.objects.values_list("entity_type", flat=True).distinct()})


@staff_view()
def notifications_view(request):
    if request.method == "POST":
        Notification.objects.filter(audience="ADMIN", read_at__isnull=True).update(read_at=timezone.now())
        return redirect("staff:notifications")
    items = Notification.objects.filter(audience="ADMIN").select_related("reservation")[:80]
    guest_log = Notification.objects.filter(audience="GUEST").select_related("reservation")[:50] if request.user.can("settings.manage") else []
    return render(request, "staff/notifications.html", {"items": items, "guest_log": guest_log})


# --------------------------------------------------------------------------- settings & users
@staff_view("settings.manage")
def settings_view(request):
    hotel = Hotel.get()
    form = F_.HotelForm(request.POST or None, request.FILES or None, instance=hotel)
    if request.method == "POST" and form.is_valid():
        old = {f: str(getattr(hotel, f)) for f in form.changed_data}
        form.save()
        audit.log("settings.updated", hotel, user=request.user, request=request, old=old, new={f: str(form.cleaned_data[f]) for f in form.changed_data}, summary="Hotel settings updated")
        messages.success(request, "Settings saved.")
        return redirect("staff:settings")
    from django.conf import settings as dj

    return render(request, "staff/settings.html", {"form": form, "tax_rules": TaxRule.objects.all(), "env": {
        "razorpay": bool(dj.RAZORPAY_KEY_ID and dj.RAZORPAY_KEY_SECRET), "webhook": bool(dj.RAZORPAY_WEBHOOK_SECRET), "twilio": bool(dj.TWILIO_ACCOUNT_SID and dj.TWILIO_AUTH_TOKEN),
        "email": dj.EMAIL_BACKEND.split(".")[-2] if "." in dj.EMAIL_BACKEND else dj.EMAIL_BACKEND, "site_url": dj.SITE_URL}})


@staff_view("users.manage")
def users_list(request):
    return render(request, "staff/users.html", {"users": User.objects.select_related("role"), "roles": Role.objects.annotate(n=Count("users"))})


@staff_view("users.manage")
def user_form(request, pk=None):
    inst = get_object_or_404(User, pk=pk) if pk else None
    form = F_.UserForm(request.POST or None, instance=inst)
    if request.method == "POST" and form.is_valid():
        if inst and inst.pk == request.user.pk and not form.cleaned_data["is_active"]:
            messages.error(request, "You can't deactivate your own account.")
        else:
            u = form.save()
            audit.log("user.updated" if inst else "user.created", u, user=request.user, request=request, new={"email": u.email, "role": str(u.role), "active": u.is_active}, summary=f"User {u.email} saved")
            messages.success(request, "User saved.")
            return redirect("staff:users")
    return render(request, "staff/form.html", {"form": form, "title": "Edit user" if inst else "Add user", "back": "/staff/users/"})


@staff_view("users.manage")
def role_form(request, pk):
    role = get_object_or_404(Role, pk=pk)
    if role.code == "super_admin":
        messages.info(request, "The Super Admin role always has every permission.")
        return redirect("staff:users")
    form = F_.RoleForm(request.POST or None, instance=role, initial={"permissions": role.permissions})
    if request.method == "POST" and form.is_valid():
        old = list(role.permissions)
        r = form.save(commit=False)
        r.permissions = form.cleaned_data["permissions"]
        r.save()
        audit.log("role.updated", r, user=request.user, request=request, old=old, new=r.permissions, summary=f"Role {r.name} permissions changed")
        messages.success(request, "Role permissions saved.")
        return redirect("staff:users")
    return render(request, "staff/form.html", {"form": form, "title": f"Permissions — {role.name}", "back": "/staff/users/"})


# --------------------------------------------------------------------------- room types & photos
@staff_view("rooms.manage")
def roomtype_form(request, pk=None):
    inst = get_object_or_404(RoomType, pk=pk) if pk else None
    form = F_.RoomTypeForm(request.POST or None, instance=inst)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.hotel = Hotel.get()
        obj.save()
        form.save_m2m()
        audit.log("roomtype.updated" if inst else "roomtype.created", obj, user=request.user, request=request, new={"name": obj.name, "base_rate": obj.base_rate}, summary=f"Room type {obj.name} saved")
        messages.success(request, "Room type saved.")
        return redirect("staff:roomtype", pk=obj.pk)
    return render(request, "staff/roomtype.html", {"form": form, "inst": inst, "images": inst.images.all() if inst else [], "rooms": inst.rooms.all() if inst else []})


@staff_view("rooms.manage")
@require_POST
def roomtype_image_add(request, pk):
    rt = get_object_or_404(RoomType, pk=pk)
    f = request.FILES.get("image")
    url = request.POST.get("image_url", "").strip()
    if f and f.content_type not in ("image/jpeg", "image/png", "image/webp"):
        messages.error(request, "Please upload a JPG, PNG or WebP image.")
    elif f and f.size > 8 * 1024 * 1024:
        messages.error(request, "Image is too large (max 8 MB).")
    elif f or url:
        first = not rt.images.exists()
        RoomImage.objects.create(room_type=rt, image=f or "", image_url="" if f else url, caption=request.POST.get("caption", "")[:120], is_primary=first, display_order=rt.images.count())
        audit.log("roomtype.photo_added", rt, user=request.user, request=request, summary=f"Photo added to {rt.name}")
        messages.success(request, "Photo added.")
    else:
        messages.error(request, "Choose a file or paste an image URL.")
    return redirect("staff:roomtype", pk=pk)


@staff_view("rooms.manage")
@require_POST
def roomtype_image_action(request, pk, image_id):
    img = get_object_or_404(RoomImage, pk=image_id, room_type_id=pk)
    if request.POST.get("action") == "primary":
        RoomImage.objects.filter(room_type_id=pk).update(is_primary=False)
        img.is_primary = True
        img.save(update_fields=["is_primary"])
    elif request.POST.get("action") == "delete":
        img.delete()
    return redirect("staff:roomtype", pk=pk)


# --------------------------------------------------------------------------- generic CRUD for configuration tables
CRUD = {}


def register_crud(slug, model, form, title, columns, perm, perm_view=None, order=None, hint=""):
    CRUD[slug] = dict(model=model, form=form, title=title, columns=columns, perm=perm, perm_view=perm_view or perm, order=order, hint=hint)


def _cell(obj, spec):
    v = getattr(obj, spec) if isinstance(spec, str) else spec(obj)
    if callable(v):
        v = v()
    if v is True:
        return "Yes"
    if v is False:
        return "No"
    return "—" if v in (None, "") else v


def crud_list(request, slug):
    c = CRUD.get(slug) or None
    if not c:
        raise Http404()

    @staff_view(c["perm_view"])
    def view(request):
        qs = c["model"].objects.all()
        if c["order"]:
            qs = qs.order_by(*c["order"])
        rows = [{"pk": o.pk, "cells": [{"h": h, "v": _cell(o, spec)} for h, spec in c["columns"]]} for o in qs]
        return render(request, "staff/crud_list.html", {"c": c, "slug": slug, "headers": [h for h, _ in c["columns"]], "rows": rows, "can_edit": request.user.can(c["perm"])})
    return view(request)


def crud_edit(request, slug, pk=None):
    c = CRUD.get(slug)
    if not c:
        raise Http404()

    @staff_view(c["perm"])
    def view(request):
        inst = get_object_or_404(c["model"], pk=pk) if pk else None
        form = c["form"](request.POST or None, instance=inst)
        if request.method == "POST" and form.is_valid():
            obj = form.save(commit=False)
            if hasattr(obj, "hotel_id") and not obj.hotel_id:
                obj.hotel = Hotel.get()
            obj.save()
            form.save_m2m()
            audit.log(f"{slug}.{'updated' if inst else 'created'}", obj, user=request.user, request=request, new={k: str(v) for k, v in form.cleaned_data.items()}, old={k: str(form.initial.get(k)) for k in form.changed_data} if inst else None,
                      summary=f"{c['title']}: {obj} saved")
            messages.success(request, "Saved.")
            return redirect("staff:crud_list", slug=slug)
        return render(request, "staff/form.html", {"form": form, "title": f"{'Edit' if inst else 'Add'} — {c['title']}", "back": f"/staff/c/{slug}/", "hint": c["hint"],
                                                  "delete_url": f"/staff/c/{slug}/{pk}/delete/" if inst else None})
    return view(request)


@require_POST
def crud_delete(request, slug, pk):
    c = CRUD.get(slug)
    if not c:
        raise Http404()

    @staff_view(c["perm"])
    def view(request):
        obj = get_object_or_404(c["model"], pk=pk)
        try:
            label = str(obj)
            obj.delete()
            audit.log(f"{slug}.deleted", c["title"], pk, user=request.user, request=request, old={"name": label}, summary=f"{c['title']}: {label} deleted")
            messages.success(request, "Deleted.")
        except dj.ProtectedError:
            messages.error(request, "This item is in use and can't be deleted. Deactivate it instead.")
        return redirect("staff:crud_list", slug=slug)
    return view(request)


register_crud("rateplans", RatePlan, F_.RatePlanForm, "Rate plans", [("Room type", "room_type"), ("Plan", "name"), ("Meals", lambda o: o.get_meal_plan_display()), ("Adjustment", lambda o: f"{o.adjustment_value}% " if o.adjustment_type == "PERCENT" else (o.adjustment_value if o.adjustment_type == "FIXED" else "—")),
                                                                    ("Refundable", "refundable"), ("Min stay", "min_stay"), ("Valid", lambda o: f"{o.valid_from or '…'} → {o.valid_to or '…'}"), ("Active", "active")], "pricing.manage", "pricing.view", order=["room_type__display_order", "display_order"],
              hint="Create unlimited rate plans per room type: meal inclusion, cancellation terms, stay limits and validity dates.")
register_crud("pricingrules", PricingRule, F_.PricingRuleForm, "Pricing rules", [("Name", "name"), ("Kind", lambda o: o.get_kind_display()), ("Room type", "room_type"), ("From", "start_date"), ("To", "end_date"), ("Weekdays", "weekdays"),
                                                                                 ("Change", lambda o: f"{o.value:+}{'%' if o.adjustment_type == 'PERCENT' else ''}"), ("Priority", "priority"), ("Active", "active")], "pricing.manage", "pricing.view", order=["priority"],
              hint="Weekend / seasonal / holiday / event adjustments. They apply on dates where you haven't set an explicit price in the pricing calendar.")
register_crud("coupons", Coupon, F_.CouponForm, "Coupons & offers", [("Code", "code"), ("Title", "title"), ("Discount", lambda o: f"{o.value}%" if o.discount_type == "PERCENT" else f"₹{o.value}"), ("Min nights", "min_nights"),
                                                                    ("Valid to", "valid_to"), ("Used", lambda o: f"{o.used_count}/{o.usage_limit or '∞'}"), ("On offers page", "show_on_offers"), ("Active", "active")], "pricing.manage", "pricing.view")
register_crud("services", Service, F_.ServiceForm, "Additional services", [("Service", "name"), ("Price", "price"), ("Charged", lambda o: o.get_charge_type_display()), ("Taxable", "taxable"), ("Bookable online", "bookable_online"), ("Active", "active")], "pricing.manage", "pricing.view")
register_crud("amenities", Amenity, F_.AmenityForm, "Amenities", [("Icon", "icon"), ("Name", "name")], "rooms.manage", "rooms.view", order=["name"])
register_crud("reviews", Review, F_.ReviewForm, "Website reviews", [("Guest", "name"), ("Location", "location"), ("Rating", "rating"), ("Review", lambda o: o.text[:80]), ("Shown", "show")], "settings.manage", hint="Testimonials shown on the homepage. Only add reviews from real guests.")
register_crud("taxes", TaxRule, F_.TaxRuleForm, "Tax rules", [("Name", "name"), ("Rate %", "rate"), ("Applies to", lambda o: o.get_applies_to_display()), ("From (>)", "min_amount"), ("Up to (≤)", "max_amount"), ("Active", "active")], "settings.manage",
              hint="Room slabs apply per room per night after discounts (e.g. 12% up to ₹7,500, 18% above). Meal and service rules apply to those charges.")


@staff_view("rooms.view")
def roomtypes_list(request):
    return render(request, "staff/roomtypes.html", {"types": RoomType.objects.annotate(n=Count("rooms")).prefetch_related("images"), "can_manage": request.user.can("rooms.manage")})
