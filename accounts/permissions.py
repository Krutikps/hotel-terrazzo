"""Permission catalogue + default role definitions. The single source of truth for RBAC."""

PERMISSIONS = {
    "dashboard.view": "View dashboard",
    "reservations.view": "View reservations",
    "reservations.create": "Create reservations / walk-ins",
    "reservations.edit": "Modify reservations & assign rooms",
    "reservations.cancel": "Cancel reservations",
    "checkin.manage": "Check guests in",
    "checkout.manage": "Check guests out",
    "payments.view": "View payments",
    "payments.add": "Record payments",
    "payments.refund": "Issue refunds",
    "invoices.view": "View / generate invoices",
    "guests.view": "View guests",
    "guests.edit": "Edit guests",
    "availability.view": "View availability & calendar",
    "rooms.view": "View rooms",
    "rooms.manage": "Manage rooms & room types",
    "rooms.block": "Block / unblock rooms",
    "rooms.status": "Change room status",
    "housekeeping.manage": "Housekeeping tasks",
    "maintenance.report": "Report maintenance issues",
    "maintenance.manage": "Manage maintenance tickets",
    "pricing.view": "View pricing",
    "pricing.manage": "Manage rates, prices, coupons",
    "reports.view": "View & export reports",
    "settings.manage": "Hotel settings",
    "users.manage": "Manage users & roles",
    "audit.view": "View audit log",
}
ALL_PERMISSIONS = list(PERMISSIONS)

_front_desk = [
    "dashboard.view", "reservations.view", "reservations.create", "reservations.edit", "reservations.cancel",
    "checkin.manage", "checkout.manage", "payments.view", "payments.add", "invoices.view",
    "guests.view", "guests.edit", "availability.view", "rooms.view", "rooms.block", "rooms.status",
    "housekeeping.manage", "maintenance.report",
]

DEFAULT_ROLES = {
    "super_admin": ("Super Admin", ["*"]),
    "hotel_manager": (
        "Hotel Manager",
        [p for p in ALL_PERMISSIONS if p not in ("users.manage", "settings.manage")] + ["settings.manage"],
    ),
    "receptionist": ("Receptionist", _front_desk),
    "housekeeping": (
        "Housekeeping",
        ["dashboard.view", "rooms.view", "rooms.status", "housekeeping.manage", "maintenance.report"],
    ),
    "accountant": (
        "Accountant",
        ["dashboard.view", "reservations.view", "guests.view", "payments.view", "payments.add",
         "payments.refund", "invoices.view", "reports.view"],
    ),
}


def ensure_default_roles():
    """Create the system roles if missing (never overwrites customised permissions)."""
    from .models import Role

    for code, (name, perms) in DEFAULT_ROLES.items():
        Role.objects.get_or_create(code=code, defaults={"name": name, "permissions": perms, "is_system": True})
