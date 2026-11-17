"""The most important test: concurrent attempts to book the last available room."""
import threading

from django.db import connection
from django.test import TransactionTestCase

from hotel.models import Reservation, ReservationRoom
from hotel.services import availability, reservations as rs

from .base import GUEST, d, make_hotel, make_plan, make_room_type


class DoubleBookingTests(TransactionTestCase):
    def setUp(self):
        self.hotel = make_hotel()
        self.rt = make_room_type(self.hotel, rooms=("101", "102"))
        self.plan = make_plan(self.rt)

    def attempt(self, results, idx, hold, barrier):
        try:
            barrier.wait()
            r = rs.create_reservation(room_type=self.rt, rate_plan=self.plan, check_in=d(10), check_out=d(12), adults=2,
                                      guest_data=dict(GUEST, email=f"g{idx}@example.com"), online_hold=hold)
            results[idx] = ("ok", r.id)
        except rs.BookingError as e:
            results[idx] = ("unavailable", e.message)
        except Exception as e:  # noqa: BLE001
            results[idx] = ("error", repr(e))
        finally:
            connection.close()

    def run_race(self, n, hold):
        results, barrier = {}, threading.Barrier(n)
        threads = [threading.Thread(target=self.attempt, args=(results, i, hold, barrier)) for i in range(n)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        return results

    def test_only_available_rooms_can_be_sold_under_concurrency(self):
        # 2 rooms, 8 simultaneous online guests -> exactly 2 succeed, the rest get the friendly unavailable message
        results = self.run_race(8, hold=True)
        errors = [v for v in results.values() if v[0] == "error"]
        self.assertEqual(errors, [], errors)
        ok = [v for v in results.values() if v[0] == "ok"]
        no = [v for v in results.values() if v[0] == "unavailable"]
        self.assertEqual(len(ok), 2)
        self.assertEqual(len(no), 6)
        self.assertTrue(all(m == "This room is no longer available. Please select another room." for _, m in no))
        self.assertEqual(Reservation.objects.count(), 2)
        self.assertEqual(availability.room_type_availability(self.rt.id, d(10), d(12))["available"], 0)

    def test_last_room_race_staff_and_guest_mixed(self):
        Reservation.objects.all().delete()
        rs.create_reservation(room_type=self.rt, rate_plan=self.plan, check_in=d(10), check_out=d(12), adults=2, guest_data=GUEST, online_hold=False,
                              user=None)                                     # one of two rooms already gone
        results = self.run_race(6, hold=False)                              # six reception bookings fight for the last one
        self.assertEqual([v for v in results.values() if v[0] == "error"], [])
        self.assertEqual(len([v for v in results.values() if v[0] == "ok"]), 1)
        self.assertEqual(ReservationRoom.objects.count(), 2)
        nums = list(Reservation.objects.values_list("booking_number", flat=True))
        self.assertEqual(len(nums), len(set(nums)))                         # booking numbers stay unique
