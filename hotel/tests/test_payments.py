import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from hotel.models import Payment, Reservation
from hotel.payments import ProviderError, get_provider
from hotel.payments.mock import sign
from hotel.payments.razorpay import RazorpayProvider
from hotel.services import payments, reservations as rs

from .base import NO_LIMITS, d, make_hotel, make_plan, make_room_type, make_user
from .test_reservations import Base


@NO_LIMITS
class PaymentFlowTests(Base):
    def hold(self, **kw):
        r = self.book(hold=True, **kw)
        info = payments.start_online_payment(r)
        return r, info["order"]["order_id"]

    def test_verified_payment_confirms_booking(self):
        r, order = self.hold()
        pay, res, outcome = payments.verify_checkout(order, "pay_1", sign(order, "pay_1"))
        self.assertEqual(outcome, "confirmed")
        res.refresh_from_db()
        self.assertEqual(res.status, Reservation.CONFIRMED)
        self.assertTrue(res.booking_number)
        self.assertEqual(res.payment_status, Reservation.P_PAID)
        self.assertEqual(res.amount_paid, res.total)
        self.assertEqual(self.avail(), 4)

    def test_forged_signature_never_confirms(self):
        r, order = self.hold()
        with self.assertRaises(ProviderError):
            payments.verify_checkout(order, "pay_1", "deadbeef")
        with self.assertRaises(ProviderError):
            payments.verify_checkout(order, "pay_1", sign(order, "pay_OTHER"))   # signature for a different payment id
        r.refresh_from_db()
        self.assertEqual(r.status, Reservation.PENDING_PAYMENT)
        self.assertIsNone(r.booking_number)

    def test_payment_is_idempotent(self):
        r, order = self.hold()
        payments.verify_checkout(order, "pay_1", sign(order, "pay_1"))
        _, _, outcome = payments.verify_checkout(order, "pay_1", sign(order, "pay_1"))
        self.assertEqual(outcome, "already_paid")
        self.assertEqual(Payment.objects.filter(reservation=r, status="PAID").count(), 1)

    def test_failed_payment_releases_inventory_and_creates_no_booking(self):
        r, order = self.hold(rooms=5)
        self.assertEqual(self.avail(), 0)
        payments.fail_checkout(order, "declined")
        r.refresh_from_db()
        self.assertEqual(r.status, Reservation.RELEASED)
        self.assertIsNone(r.booking_number)
        self.assertEqual(r.payment_status, Reservation.P_FAILED)
        self.assertEqual(self.avail(), 5)
        self.assertEqual(Payment.objects.get(reservation=r).status, Payment.FAILED)

    def test_payment_after_expiry_revives_if_room_still_free(self):
        r, order = self.hold()
        Reservation.objects.filter(pk=r.pk).update(hold_expires_at=timezone.now() - timedelta(minutes=1))
        rs.expire_holds()
        _, res, outcome = payments.verify_checkout(order, "pay_late", sign(order, "pay_late"))
        self.assertEqual(outcome, "confirmed")
        self.assertEqual(res.status, Reservation.CONFIRMED)

    def test_payment_after_expiry_is_refunded_if_room_was_taken(self):
        r, order = self.hold(rooms=5)
        Reservation.objects.filter(pk=r.pk).update(hold_expires_at=timezone.now() - timedelta(minutes=1))
        rs.expire_holds()
        self.book(rooms=5)                                   # someone else books everything
        _, res, outcome = payments.verify_checkout(order, "pay_late", sign(order, "pay_late"))
        self.assertEqual(outcome, "expired_refunded")
        res.refresh_from_db()
        self.assertNotEqual(res.status, Reservation.CONFIRMED)
        self.assertEqual(res.amount_paid, Decimal("0.00"))   # auto-refunded
        self.assertEqual(Payment.objects.filter(reservation=r, kind="REFUND", status="PAID").count(), 1)

    def test_offline_payments_and_status_transitions(self):
        r = self.book()
        p = payments.record_offline(r, Decimal("1000"), "CASH", user=self.staff)
        r.refresh_from_db()
        self.assertEqual(r.payment_status, Reservation.P_PARTIAL)
        with self.assertRaises(rs.BookingError):
            payments.record_offline(r, Decimal("999999"), "CASH", user=self.staff)   # more than the balance
        with self.assertRaises(rs.BookingError):
            payments.record_offline(r, Decimal("10"), "ONLINE", user=self.staff)     # online is gateway only
        payments.record_offline(r, r.balance, "UPI", user=self.staff, reference="UTR1")
        r.refresh_from_db()
        self.assertEqual(r.payment_status, Reservation.P_PAID)
        payments.refund_payment(p, Decimal("400"), user=self.staff, reason="goodwill")
        r.refresh_from_db()
        self.assertEqual(r.payment_status, Reservation.P_PART_REFUNDED)
        with self.assertRaises(rs.BookingError):
            payments.refund_payment(p, Decimal("700"), user=self.staff)              # only 600 left to refund on that payment

    def test_amount_mismatch_is_rejected(self):
        r, order = self.hold()
        with self.assertRaises(ProviderError):
            payments.apply_result("mock", order, "pay_x", "PAID", Decimal("1.00"), {})
        r.refresh_from_db()
        self.assertEqual(r.status, Reservation.PENDING_PAYMENT)

    def test_pending_hold_cannot_take_offline_payment(self):
        r = self.book(hold=True)
        with self.assertRaises(rs.BookingError):
            payments.record_offline(r, Decimal("10"), "CASH", user=self.staff)


@NO_LIMITS
class RazorpayProviderTests(TestCase):
    @override_settings(RAZORPAY_KEY_ID="rzp_test_x", RAZORPAY_KEY_SECRET="sekret", RAZORPAY_WEBHOOK_SECRET="whsec")
    def test_signature_and_server_side_fetch(self):
        p = RazorpayProvider()
        good = hmac.new(b"sekret", b"order_1|pay_1", hashlib.sha256).hexdigest()
        with self.assertRaises(ProviderError):
            p.verify_payment("order_1", "pay_1", "bad", Decimal("100"))
        fetched = {"id": "pay_1", "order_id": "order_1", "status": "captured", "amount": 10000, "currency": "INR"}
        with mock.patch.object(RazorpayProvider, "_call", return_value=fetched):
            v = p.verify_payment("order_1", "pay_1", good, Decimal("100"))
            self.assertEqual((v.status, v.amount), ("PAID", Decimal("100")))
            with self.assertRaises(ProviderError):    # amount differs from what we expect
                p.verify_payment("order_1", "pay_1", good, Decimal("250"))
        with mock.patch.object(RazorpayProvider, "_call", return_value=dict(fetched, order_id="order_OTHER")):
            with self.assertRaises(ProviderError):    # valid signature but payment belongs to another order
                p.verify_payment("order_1", "pay_1", good, Decimal("100"))

    @override_settings(RAZORPAY_KEY_ID="rzp_test_x", RAZORPAY_KEY_SECRET="sekret", RAZORPAY_WEBHOOK_SECRET="whsec")
    def test_webhook_signature(self):
        p = RazorpayProvider()
        body = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_1", "order_id": "order_1", "amount": 5000}}}}).encode()
        sig = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
        v = p.parse_webhook(body, {"X-Razorpay-Signature": sig})
        self.assertEqual((v.order_id, v.status, v.amount), ("order_1", "PAID", Decimal("50")))
        with self.assertRaises(ProviderError):
            p.parse_webhook(body, {"X-Razorpay-Signature": "nope"})

    def test_unconfigured_razorpay_and_production_mock_are_refused(self):
        with override_settings(RAZORPAY_KEY_ID="", RAZORPAY_KEY_SECRET=""):
            with self.assertRaises(ProviderError):
                get_provider("razorpay")
        with override_settings(DEBUG=False, ALLOW_MOCK_PAYMENTS=False):
            with self.assertRaises(ProviderError):
                get_provider("mock")


@NO_LIMITS
class PaymentApiTests(Base):
    """The guest-facing HTTP flow, end to end."""

    def setUp(self):
        super().setUp()
        self.c = Client()

    def post(self, url, data):
        return self.c.post(url, json.dumps(data), content_type="application/json")

    def payload(self, **kw):
        p = {"room_type": self.rt.id, "rate_plan": self.plan.id, "check_in": d(10).isoformat(), "check_out": d(12).isoformat(), "adults": 2, "children": 0, "rooms": 1,
             "guest": {"first_name": "Asha", "last_name": "Rao", "email": "asha@example.com", "phone": "+91 9999911111"}, "purpose": "Leisure",
             "request_types": ["Quiet room"], "special_requests": "Late arrival"}
        p.update(kw)
        return p

    def test_full_guest_journey(self):
        r = self.c.get("/api/availability/", {"check_in": d(10), "check_out": d(12), "adults": 2, "children": 0, "rooms": 1})
        data = r.json()["data"]
        self.assertEqual(data["results"][0]["available"], 5)
        q = self.post("/api/quote/", self.payload()).json()["data"]["quote"]
        self.assertEqual(q["total"], "11200.00")
        h = self.post("/api/reservations/hold/", self.payload())
        self.assertEqual(h.status_code, 201)
        hold = h.json()["data"]
        self.assertEqual(self.avail(), 4)                                    # held inventory is already reflected
        # browser *claims* success without proof -> must not confirm
        bad = self.post("/api/payments/verify/", {"order_id": hold["order"]["order_id"], "payment_id": "pay_x", "signature": "forged"})
        self.assertEqual(bad.status_code, 402)
        self.assertEqual(Reservation.objects.get(access_token=hold["token"]).status, Reservation.PENDING_PAYMENT)
        # sandbox gateway signs and reports success
        ok = self.post(f"/api/payments/mock/{hold['order']['order_id']}/result/", {"result": "success"}).json()
        self.assertEqual(ok["data"]["status"], "CONFIRMED")
        conf = self.c.get(f"/booking/{hold['token']}/confirmation/")
        self.assertContains(conf, "TST-")
        res = Reservation.objects.get(access_token=hold["token"])
        self.assertEqual(res.requests.count(), 1)
        self.assertEqual(res.purpose, "Leisure")
        self.assertEqual(self.c.get(f"/booking/{hold['token']}/pdf/").status_code, 200)

    def test_unavailable_returns_friendly_error(self):
        self.book(rooms=5)
        r = self.post("/api/reservations/hold/", self.payload())
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["message"], "This room is no longer available. Please select another room.")

    def test_validation_messages(self):
        r = self.post("/api/reservations/hold/", self.payload(check_out=d(10).isoformat()))
        self.assertEqual(r.json()["error"]["message"], "Check-out date must be after check-in date.")
        r = self.post("/api/reservations/hold/", self.payload(adults=4))
        self.assertEqual(r.json()["error"]["message"], "Number of guests exceeds room capacity.")
        r = self.c.get("/api/availability/", {"check_in": d(10), "check_out": d(11), "adults": "abc"})
        self.assertEqual(r.status_code, 400)

    def test_failed_payment_flow_and_restart(self):
        hold = self.post("/api/reservations/hold/", self.payload()).json()["data"]
        self.post(f"/api/payments/mock/{hold['order']['order_id']}/result/", {"result": "fail"})
        res = Reservation.objects.get(access_token=hold["token"])
        self.assertEqual(res.status, Reservation.RELEASED)
        self.assertEqual(self.avail(), 5)
        again = self.post(f"/api/reservations/{hold['token']}/restart/", {})
        self.assertEqual(again.status_code, 201)
        self.assertEqual(self.avail(), 4)

    def test_lookup_and_guest_cancellation(self):
        hold = self.post("/api/reservations/hold/", self.payload()).json()["data"]
        self.post(f"/api/payments/mock/{hold['order']['order_id']}/result/", {"result": "success"})
        res = Reservation.objects.get(access_token=hold["token"])
        miss = self.post("/api/my-booking/lookup/", {"booking_number": res.booking_number, "contact": "wrong@example.com"})
        self.assertEqual(miss.status_code, 404)
        hit = self.post("/api/my-booking/lookup/", {"booking_number": res.booking_number, "contact": "ASHA@example.com"})
        self.assertEqual(hit.json()["data"]["url"], f"/my-booking/{res.access_token}/")
        by_phone = self.post("/api/my-booking/lookup/", {"booking_number": res.booking_number, "contact": "9999911111"})
        self.assertEqual(by_phone.status_code, 200)
        prev = self.post(f"/api/reservations/{hold['token']}/cancel/", {"preview": True}).json()["data"]
        self.assertEqual(prev["refund"], res.total.__str__())
        self.post(f"/api/reservations/{hold['token']}/cancel/", {})
        res.refresh_from_db()
        self.assertEqual(res.status, Reservation.CANCELLED)
        self.assertEqual(res.amount_paid, Decimal("0.00"))
        self.assertEqual(self.avail(), 5)

    def test_webhook_rejects_bad_signature(self):
        r = self.c.post("/api/payments/webhook/razorpay/", "{}", content_type="application/json")
        self.assertEqual(r.status_code, 400)
