import json

from django.test import Client, TestCase

from accounts.models import Role
from hotel.models import Reservation

from .base import NO_LIMITS, d, make_user
from .test_reservations import Base


@NO_LIMITS
class PermissionTests(Base):
    def client_for(self, role):
        c = Client()
        user = self.staff if role == "receptionist" else make_user(role, f"{role}2@test.example")
        c.force_login(user)
        return c

    def test_anonymous_is_redirected_from_every_staff_page(self):
        c = Client()
        for url in ["/staff/", "/staff/reservations/", "/staff/rooms/", "/staff/payments/", "/staff/reports/", "/staff/settings/", "/staff/users/", "/staff/audit/", "/staff/c/rateplans/"]:
            r = c.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("/staff/login/", r["Location"])

    def test_anonymous_api_is_rejected(self):
        c = Client()
        for url in ["/api/staff/pulse/", "/api/staff/calendar/", "/api/staff/pricing/"]:
            self.assertEqual(c.get(url).status_code, 401, url)
        r = c.post("/api/staff/reservations/", "{}", content_type="application/json")
        self.assertEqual(r.status_code, 401)

    def test_receptionist_matrix(self):
        c = self.client_for("receptionist")
        ok = ["/staff/", "/staff/reservations/", "/staff/arrivals/", "/staff/departures/", "/staff/calendar/", "/staff/rooms/", "/staff/housekeeping/", "/staff/guests/", "/staff/payments/", "/staff/invoices/", "/staff/blocks/", "/staff/maintenance/"]
        for url in ok:
            self.assertEqual(c.get(url).status_code, 200, url)
        denied = ["/staff/settings/", "/staff/users/", "/staff/reports/", "/staff/audit/", "/staff/pricing/", "/staff/c/rateplans/new/", "/staff/room-types/new/", "/staff/rooms/new/"]
        for url in denied:
            self.assertEqual(c.get(url).status_code, 403, url)
        r = c.post("/api/staff/pricing/update/", json.dumps({"room_type": self.rt.id, "date": d(5).isoformat(), "price": 1}), content_type="application/json")
        self.assertEqual(r.status_code, 403)
        refund = c.post("/staff/payments/1/refund/", {"amount": "1"})
        self.assertEqual(refund.status_code, 403)

    def test_housekeeping_can_only_do_housekeeping(self):
        c = self.client_for("housekeeping")
        self.assertEqual(c.get("/staff/housekeeping/").status_code, 200)
        for url in ["/staff/reservations/", "/staff/payments/", "/staff/reports/", "/staff/guests/", "/staff/calendar/"]:
            self.assertEqual(c.get(url).status_code, 403, url)
        room = self.rt.rooms.first()
        self.assertEqual(c.post(f"/staff/rooms/{room.id}/status/", {"status": "CLEANING"}).status_code, 302)
        room.refresh_from_db()
        self.assertEqual(room.status, "CLEANING")
        # cannot block rooms or cancel bookings
        r = self.book()
        self.assertEqual(c.post(f"/staff/reservations/{r.id}/cancel/", {"reason": "x"}).status_code, 403)
        self.assertEqual(c.post("/staff/blocks/new/", {"room": room.id}).status_code, 403)

    def test_accountant_sees_money_not_operations(self):
        c = self.client_for("accountant")
        for url in ["/staff/payments/", "/staff/invoices/", "/staff/reports/", "/staff/reservations/"]:
            self.assertEqual(c.get(url).status_code, 200, url)
        for url in ["/staff/rooms/", "/staff/arrivals/", "/staff/housekeeping/", "/staff/reservations/new/"]:
            self.assertEqual(c.get(url).status_code, 403, url)

    def test_manager_has_operations_and_super_admin_everything(self):
        m = self.client_for("hotel_manager")
        for url in ["/staff/reports/", "/staff/pricing/", "/staff/c/rateplans/", "/staff/room-types/", "/staff/audit/", "/staff/settings/"]:
            self.assertEqual(m.get(url).status_code, 200, url)
        self.assertEqual(m.get("/staff/users/").status_code, 403)
        a = self.client_for("super_admin")
        self.assertEqual(a.get("/staff/users/").status_code, 200)

    def test_inactive_and_roleless_users_cannot_sign_in(self):
        from accounts.models import User

        u = make_user("receptionist", "gone@test.example")
        u.is_active = False
        u.save()
        c = Client()
        r = c.post("/staff/login/", {"email": "gone@test.example", "password": "Passw0rd!x"})
        self.assertContains(r, "Incorrect email or password")
        norole = User.objects.create_user("norole@test.example", "Passw0rd!x", first_name="No")
        r = c.post("/staff/login/", {"email": "norole@test.example", "password": "Passw0rd!x"})
        self.assertContains(r, "Incorrect email or password")
        r = c.post("/staff/login/", {"email": self.staff.email, "password": "Passw0rd!x"})
        self.assertEqual(r.status_code, 302)

    def test_role_permissions_are_editable(self):
        role = Role.objects.get(code="receptionist")
        role.permissions = [p for p in role.permissions if p != "payments.view"]
        role.save()
        c = Client()
        c.force_login(self.staff.__class__.objects.get(pk=self.staff.pk))
        self.assertEqual(c.get("/staff/payments/").status_code, 403)

    def test_audit_log_records_actions(self):
        from hotel.models import AuditLog

        r = self.book()
        self.assertTrue(AuditLog.objects.filter(action="reservation.created", entity_id=str(r.id)).exists())
        c = Client()
        c.force_login(self.staff)
        c.post(f"/staff/reservations/{r.id}/cancel/", {"reason": "guest asked"})
        log = AuditLog.objects.get(action="reservation.cancelled")
        self.assertEqual(log.user_id, self.staff.id)
        self.assertEqual(log.old_value, {"status": "CONFIRMED"})
        self.assertEqual(log.new_value["reason"], "guest asked")
