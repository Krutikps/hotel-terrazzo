from .models import Hotel, Notification

# (label, url name or path, permission, section)
NAV = [
    ("Front desk", [
        ("Dashboard", "/staff/", "dashboard.view"),
        ("Upcoming bookings", "/staff/reservations/", "reservations.view"),
        ("New booking", "/staff/reservations/new/", "reservations.create"),
        ("Arrivals", "/staff/arrivals/", "checkin.manage"),
        ("Departures", "/staff/departures/", "checkout.manage"),
        ("Calendar", "/staff/calendar/", "availability.view"),
        ("Guests", "/staff/guests/", "guests.view"),
    ]),
    ("Rooms", [
        ("Rooms", "/staff/rooms/", "rooms.view"),
        ("Housekeeping", "/staff/housekeeping/", "housekeeping.manage"),
        ("Maintenance", "/staff/maintenance/", "maintenance.report"),
        ("Room blocks", "/staff/blocks/", "rooms.view"),
    ]),
    ("Money", [
        ("Payments", "/staff/payments/", "payments.view"),
        ("Invoices", "/staff/invoices/", "invoices.view"),
        ("Reports", "/staff/reports/", "reports.view"),
    ]),
    ("Rates", [
        ("Pricing calendar", "/staff/pricing/", "pricing.view"),
        ("Rate plans", "/staff/c/rateplans/", "pricing.view"),
        ("Pricing rules", "/staff/c/pricingrules/", "pricing.view"),
        ("Coupons & offers", "/staff/c/coupons/", "pricing.view"),
        ("Services", "/staff/c/services/", "pricing.view"),
    ]),
    ("Admin", [
        ("Room types", "/staff/room-types/", "rooms.view"),
        ("Amenities", "/staff/c/amenities/", "rooms.view"),
        ("Website reviews", "/staff/c/reviews/", "settings.manage"),
        ("Settings", "/staff/settings/", "settings.manage"),
        ("Users & roles", "/staff/users/", "users.manage"),
        ("Audit log", "/staff/audit/", "audit.view"),
        ("Notifications", "/staff/notifications/", None),
    ]),
]


def hotel(request):
    h = Hotel.get()
    ctx = {"hotel": h, "currency_symbol": h.currency_symbol if h else "₹"}
    user = getattr(request, "user", None)
    if request.path.startswith("/staff/") and user is not None and user.is_authenticated:
        ctx["nav"] = [(sec, [(l, u, u == request.path or (u != "/staff/" and request.path.startswith(u))) for l, u, p in items if not p or user.can(p)]) for sec, items in NAV]
        ctx["nav"] = [(s, i) for s, i in ctx["nav"] if i]
        ctx["unread_count"] = Notification.objects.filter(audience="ADMIN", read_at__isnull=True).count()
    return ctx
