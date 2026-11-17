"""Seeds the demo data (exercising the seed command itself) and renders every screen as the right role."""
import json

from django.core.management import call_command
from django.test import Client, TestCase

from accounts.models import User
from hotel.models import Guest, Invoice, Payment, Reservation, Room, RoomType
from hotel.services import reports

from .base import NO_LIMITS, d


@NO_LIMITS
class SmokeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        self.admin = Client()
        self.admin.force_login(User.objects.get(email="admin@terrazzo.test"))

    def test_seed_is_realistic(self):
        self.assertEqual(RoomType.objects.count(), 5)
        self.assertGreaterEqual(Room.objects.count(), 20)
        self.assertTrue(Reservation.objects.filter(status="CHECKED_IN").exists())
        self.assertTrue(Reservation.objects.filter(status="CHECKED_OUT").exists())
        self.assertTrue(Reservation.objects.filter(status="CONFIRMED", check_in__gt=d(0)).exists())
        self.assertTrue(Reservation.objects.filter(status="CANCELLED").exists())
        self.assertTrue(Payment.objects.filter(status="PAID").exists())
        self.assertTrue(Invoice.objects.exists())

    def test_public_pages_render(self):
        c = Client()
        for url in ["/", "/rooms/", "/availability/", "/offers/", "/amenities/", "/gallery/", "/about/", "/contact/", "/my-booking/"]:
            self.assertEqual(c.get(url).status_code, 200, url)
        for rt in RoomType.objects.all():
            self.assertContains(c.get(f"/rooms/{rt.slug}/"), rt.name)
        rt = RoomType.objects.first()
        plan = rt.rate_plans.filter(corporate=False).first()
        r = c.get(f"/book/?room_type={rt.id}&rate_plan={plan.id}&check_in={d(30)}&check_out={d(32)}&adults=2&children=0&rooms=1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.get("/book/?room_type=999&rate_plan=1").status_code, 302)
        self.assertEqual(c.get("/placeholder/hero.svg").status_code, 200)
        self.assertEqual(c.get("/rooms/not-a-room/").status_code, 404)
        self.assertEqual(c.get("/pay/00000000-0000-0000-0000-000000000000/").status_code, 404)

    def test_every_staff_page_renders_for_admin(self):
        res = Reservation.objects.filter(status="CONFIRMED").first()
        guest, room = Guest.objects.first(), Room.objects.first()
        pay, inv, rt = Payment.objects.filter(status="PAID").first(), Invoice.objects.first(), RoomType.objects.first()
        urls = ["/staff/", "/staff/reservations/", "/staff/reservations/?view=all&q=Sharma", "/staff/reservations/?view=inhouse", "/staff/reservations/?view=pending", "/staff/reservations/new/",
                f"/staff/reservations/{res.id}/", f"/staff/reservations/{res.id}/edit/", f"/staff/reservations/{res.id}/print/", f"/staff/reservations/{res.id}/pdf/",
                "/staff/arrivals/", "/staff/departures/", "/staff/calendar/", "/staff/guests/", f"/staff/guests/{guest.id}/", f"/staff/guests/{guest.id}/edit/",
                "/staff/rooms/", "/staff/rooms/?effective=OCCUPIED", "/staff/rooms/new/", f"/staff/rooms/{room.id}/edit/", "/staff/blocks/", "/staff/housekeeping/", "/staff/maintenance/", "/staff/maintenance/?status=all",
                "/staff/payments/", f"/staff/payments/{pay.id}/receipt/", "/staff/invoices/", f"/staff/invoices/{inv.id}/", f"/staff/invoices/{inv.id}/pdf/", "/staff/pricing/",
                "/staff/room-types/", f"/staff/room-types/{rt.id}/", "/staff/room-types/new/", "/staff/reports/", "/staff/audit/", "/staff/notifications/", "/staff/settings/", "/staff/users/", "/staff/users/new/",
                "/staff/c/rateplans/", "/staff/c/rateplans/new/", "/staff/c/pricingrules/", "/staff/c/coupons/", "/staff/c/services/", "/staff/c/amenities/", "/staff/c/taxes/", "/staff/c/reviews/",
                "/api/staff/pulse/", f"/api/staff/calendar/?start={d(0)}&days=14", f"/api/staff/pricing/?start={d(0)}&days=14"]
        for url in urls:
            r = self.admin.get(url)
            self.assertEqual(r.status_code, 200, f"{url} -> {r.status_code}")

    def test_all_reports_and_exports(self):
        for key in reports.REPORTS:
            for fmt in ("", "&export=csv", "&export=xlsx", "&export=pdf"):
                r = self.admin.get(f"/staff/reports/?report={key}&from={d(-30)}&to={d(30)}{fmt}")
                self.assertEqual(r.status_code, 200, f"{key}{fmt}")

    def test_dashboard_numbers_come_from_the_database(self):
        s = reports.dashboard()
        self.assertEqual(s["total_rooms"], Room.objects.filter(active=True).count())
        self.assertGreater(s["occupied"], 0)
        self.assertGreater(s["blocked"] + s["maintenance"], 0)
        self.assertEqual(s["in_house"], Reservation.objects.filter(status="CHECKED_IN").count())

    def test_staff_api_booking_calendar_move_and_bulk_pricing(self):
        rt = RoomType.objects.get(name="Deluxe Room")
        plan = rt.rate_plans.get(name="Room Only")
        payload = {"room_type": rt.id, "rate_plan": plan.id, "check_in": d(60).isoformat(), "check_out": d(62).isoformat(), "adults": 2, "children": 0, "rooms": 1,
                   "guest": {"first_name": "Walk", "last_name": "In", "phone": "999"}, "source": "WALK_IN", "payment": {"amount": "1000", "method": "CASH"}}
        r = self.admin.post("/api/staff/reservations/", json.dumps(payload), content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content)
        res = Reservation.objects.get(pk=r.json()["data"]["id"])
        self.assertEqual(res.amount_paid, 1000)
        rr = res.rooms.first()
        free = Room.objects.filter(room_type=rt, status="CLEAN").first()
        m = self.admin.post(f"/api/staff/reservations/{res.id}/move/", json.dumps({"rr": rr.id, "room": free.id}), content_type="application/json")
        self.assertEqual(m.status_code, 200, m.content)
        other = Room.objects.exclude(room_type=rt).first()
        bad = self.admin.post(f"/api/staff/reservations/{res.id}/move/", json.dumps({"rr": rr.id, "room": other.id}), content_type="application/json")
        self.assertEqual(bad.status_code, 400)      # wrong room type: refused server-side
        pv = self.admin.post(f"/api/staff/reservations/{res.id}/modify/", json.dumps(dict(payload, check_out=d(63).isoformat(), preview=True)), content_type="application/json")
        self.assertEqual(pv.status_code, 200, pv.content)
        bulk = self.admin.post("/api/staff/pricing/bulk/", json.dumps({"room_types": [rt.id], "start": d(70).isoformat(), "end": d(76).isoformat(), "weekdays": [], "mode": "percent", "value": 10}),
                               content_type="application/json")
        self.assertEqual(bulk.status_code, 200, bulk.content)
        self.assertEqual(bulk.json()["data"]["changed"], 7)
