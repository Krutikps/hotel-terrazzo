from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from hotel.models import (HousekeepingLog, Payment, Reservation, ReservationRoom, Room, RoomBlock)
from hotel.services import availability, payments, pricing, reservations as rs

from .base import GUEST, d, make_hotel, make_plan, make_room_type, make_user


class Base(TestCase):
    def setUp(self):
        self.hotel = make_hotel()
        self.rt = make_room_type(self.hotel, rooms=("101", "102", "103", "104", "105"))
        self.plan = make_plan(self.rt)
        self.staff = make_user("receptionist")

    def book(self, start=10, nights=2, rooms=1, hold=False, **kw):
        return rs.create_reservation(room_type=self.rt, rate_plan=self.plan, check_in=d(start), check_out=d(start + nights), adults=2 * rooms,
                                     rooms=rooms, guest_data=GUEST, online_hold=hold, user=None if hold else self.staff,
                                     source="WEBSITE" if hold else "RECEPTION", **kw)

    def avail(self, start=10, nights=2):
        return availability.room_type_availability(self.rt.id, d(start), d(start + nights))["available"]


class AvailabilityTests(Base):
    def test_example_from_spec_five_rooms(self):
        # 101,102 booked; 103 blocked; 104 maintenance -> only 105 available
        self.book(rooms=2, room_ids=[Room.objects.get(room_number="101").id, Room.objects.get(room_number="102").id])
        rs.block_room(Room.objects.get(room_number="103"), d(10), d(11), "OWNER_USE", "", self.staff)
        rs.create_ticket(Room.objects.get(room_number="104"), "AC", "HIGH", self.staff)
        self.assertEqual(self.avail(), 1)
        free = list(availability.assignable_rooms(self.rt.id, d(10), d(12)).values_list("room_number", flat=True))
        self.assertEqual(free, ["105"])

    def test_back_to_back_stays_do_not_conflict(self):
        self.book(start=10, nights=2, rooms=5)
        self.assertEqual(self.avail(start=12), 5)   # arriving on the departure day is fine
        self.assertEqual(self.avail(start=11, nights=2), 0)

    def test_pending_hold_blocks_inventory_until_it_expires(self):
        r = self.book(rooms=5, hold=True)
        self.assertEqual(self.avail(), 0)
        Reservation.objects.filter(pk=r.pk).update(hold_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.avail(), 5)           # expired hold no longer counts, even before the sweeper runs
        self.assertEqual(rs.expire_holds(), 1)
        r.refresh_from_db()
        self.assertEqual(r.status, Reservation.RELEASED)

    def test_block_removes_room_from_guest_search_and_unblock_restores(self):
        room = Room.objects.get(room_number="101")
        blk = rs.block_room(room, d(10), d(12), "RENOVATION", "paint", self.staff)
        self.assertEqual(self.avail(), 4)
        results = availability.search(d(10), d(12), 2, 0, 1)
        self.assertEqual(results[0]["available"], 4)
        rs.unblock_room(blk, self.staff)
        self.assertEqual(self.avail(), 5)

    def test_cannot_block_room_with_guest_or_oversell(self):
        r = self.book(rooms=1, room_ids=[Room.objects.get(room_number="101").id])
        with self.assertRaises(rs.BookingError):
            rs.block_room(Room.objects.get(room_number="101"), d(10), d(11), "OTHER", "", self.staff)
        self.book(rooms=4)  # all 5 rooms now sold for those nights (one assigned, four not)
        with self.assertRaises(rs.BookingError):
            rs.block_room(Room.objects.get(room_number="105"), d(10), d(11), "OTHER", "", self.staff)
        self.assertEqual(RoomBlock.objects.count(), 0)

    def test_maintenance_removes_room_and_resolution_restores_it(self):
        room = Room.objects.get(room_number="102")
        t = rs.create_ticket(room, "Leak", "URGENT", self.staff)
        room.refresh_from_db()
        self.assertEqual(room.status, Room.MAINTENANCE)
        self.assertEqual(self.avail(), 4)
        rs.update_ticket(t, self.staff, status="RESOLVED")
        room.refresh_from_db()
        self.assertEqual(room.status, Room.DIRTY)
        self.assertEqual(self.avail(), 5)

    def test_room_capacity_filters_search(self):
        self.assertEqual(availability.search(d(10), d(12), 3, 1, 1), [])   # 4 guests > max occupancy 3
        with self.assertRaises(pricing.PricingError):
            availability.search(d(12), d(10), 2, 0, 1)


class ReservationTests(Base):
    def test_staff_booking_is_confirmed_with_number_and_totals(self):
        r = self.book()
        self.assertEqual(r.status, Reservation.CONFIRMED)
        self.assertRegex(r.booking_number, r"^TST-\d{4}-000001$")
        self.assertEqual(r.total, Decimal("11200.00"))     # 2 x 5000 + 12% tax
        self.assertEqual(r.balance, Decimal("11200.00"))
        r2 = self.book(start=20)
        self.assertTrue(r2.booking_number.endswith("000002"))

    def test_online_hold_has_no_number_until_confirmed(self):
        r = self.book(hold=True)
        self.assertIsNone(r.booking_number)
        self.assertEqual(r.status, Reservation.PENDING_PAYMENT)
        self.assertTrue(r.is_hold_alive)

    def test_assign_room_validations(self):
        a, b = self.book(rooms=1), self.book(rooms=1)
        rr_a, rr_b = a.rooms.first(), b.rooms.first()
        room = Room.objects.get(room_number="101")
        rs.assign_room(a.id, rr_a.id, room.id, self.staff)
        with self.assertRaises(rs.BookingError):                 # already assigned to another booking
            rs.assign_room(b.id, rr_b.id, room.id, self.staff)
        other_type = make_room_type(self.hotel, "Suite", rooms=("201",), rate="9000")
        with self.assertRaises(rs.BookingError):                 # wrong room type
            rs.assign_room(b.id, rr_b.id, Room.objects.get(room_number="201").id, self.staff)

    def test_cancellation_free_vs_late_and_inventory_released(self):
        free = self.book(start=10)            # free until 24h before check-in
        paid = payments.record_offline(free, Decimal("1000"), "CASH", user=self.staff)
        rs.cancel(free.id, user=self.staff, reason="x")
        free.refresh_from_db()
        self.assertEqual(free.status, Reservation.CANCELLED)
        self.assertEqual(free.amount_paid, Decimal("0.00"))      # fully refunded
        self.assertEqual(free.payment_status, Reservation.P_REFUNDED)
        self.assertEqual(self.avail(), 5)
        late = self.book(start=0, nights=1)   # arrival today: always inside the 24h free-cancellation window
        payments.record_offline(late, late.total, "CASH", user=self.staff)
        late.refresh_from_db()                # amount_paid is recomputed on a fresh row
        fee, refund = rs.cancellation_quote(late)
        self.assertEqual(fee, late.total)     # plan penalty is 100%
        self.assertEqual(refund, Decimal("0.00"))
        fee2, refund2 = rs.cancellation_quote(late, waive_fee=True)
        self.assertEqual((fee2, refund2), (Decimal("0.00"), late.total))

    def test_non_refundable_plan_keeps_everything(self):
        nr = make_plan(self.rt, "NR", refundable=False)
        r = rs.create_reservation(room_type=self.rt, rate_plan=nr, check_in=d(30), check_out=d(32), adults=2, guest_data=GUEST, user=self.staff)
        payments.record_offline(r, r.total, "UPI", user=self.staff)
        self.assertEqual(rs.cancellation_quote(r), (r.total, Decimal("0.00")))

    def test_guest_cannot_cancel_after_arrival_date(self):
        r = self.book(start=0, nights=1)
        with self.assertRaises(rs.BookingError):
            rs.cancel(r.id, by_guest=True)

    def test_modify_reprices_and_rechecks_availability(self):
        r = self.book(start=10, nights=2)
        r = rs.modify(r.id, self.staff, check_out=d(13))
        self.assertEqual(r.nights, 3)
        self.assertEqual(r.total, Decimal("16800.00"))
        self.book(start=14, nights=1, rooms=5)           # sells out day 14
        with self.assertRaises(rs.BookingError):
            rs.modify(r.id, self.staff, check_out=d(15))
        r.refresh_from_db()
        self.assertEqual(r.nights, 3)                    # failed change left the booking untouched

    def test_modify_keeps_assigned_room_when_still_valid_and_audits(self):
        from hotel.models import AuditLog

        r = self.book(rooms=1, room_ids=[Room.objects.get(room_number="103").id])
        rs.modify(r.id, self.staff, adults=3)
        self.assertEqual(r.rooms.first().room.room_number, "103")
        self.assertTrue(AuditLog.objects.filter(action="reservation.modified", entity_id=str(r.id)).exists())

    def test_check_in_and_check_out_flow(self):
        r = self.book(start=0, nights=2)
        with self.assertRaises(rs.BookingError):          # identity required
            rs.check_in(r.id, self.staff)
        r = rs.check_in(r.id, self.staff, id_type="Aadhaar", id_number="1234")
        self.assertEqual(r.status, Reservation.CHECKED_IN)
        room = r.rooms.first().room
        room.refresh_from_db()
        self.assertEqual(room.status, Room.OCCUPIED)
        with self.assertRaises(rs.BookingError):          # unpaid balance
            rs.check_out(r.id, self.staff)
        r, inv = rs.check_out(r.id, self.staff, charges=[{"category": "MINIBAR", "description": "Minibar", "amount": Decimal("500"), "taxable": True}],
                              collect_amount=Decimal("11790"), method="CARD")
        self.assertEqual(r.status, Reservation.CHECKED_OUT)
        self.assertEqual(r.total, Decimal("11790.00"))    # 11200 + 500 + 18% GST on the extra
        self.assertEqual(r.balance, Decimal("0.00"))
        room.refresh_from_db()
        self.assertEqual(room.status, Room.DIRTY)
        self.assertTrue(HousekeepingLog.objects.filter(room=room, to_status=Room.DIRTY).exists())
        self.assertTrue(inv.number.startswith("INV-"))

    def test_cannot_check_in_dirty_room_or_future_booking(self):
        future = self.book(start=5)
        with self.assertRaises(rs.BookingError):
            rs.check_in(future.id, self.staff, id_type="PAN", id_number="1")
        r = self.book(start=0, nights=1, rooms=1, room_ids=[Room.objects.get(room_number="101").id])
        Room.objects.filter(room_number="101").update(status=Room.DIRTY)
        with self.assertRaises(rs.BookingError):
            rs.check_in(r.id, self.staff, id_type="PAN", id_number="1")

    def test_no_show_sweeper(self):
        past = self.book(start=-3, nights=1)
        self.assertEqual(rs.mark_no_shows(), 1)
        past.refresh_from_db()
        self.assertEqual(past.status, Reservation.NO_SHOW)
        self.assertEqual(self.avail(start=-3, nights=1), 5)
