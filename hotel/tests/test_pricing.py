from decimal import Decimal

from django.test import TestCase

from hotel.models import Coupon, DailyRate, PricingRule, Service
from hotel.services import pricing

from .base import d, make_hotel, make_plan, make_room_type


class PricingTests(TestCase):
    def setUp(self):
        self.hotel = make_hotel()
        self.rt = make_room_type(self.hotel, rate="5000", max_occupancy=4)
        self.plan = make_plan(self.rt)
        self.bf = make_plan(self.rt, "Breakfast", meal_plan="BREAKFAST", meal_adult_price=Decimal("800"), meal_child_price=Decimal("400"))

    def q(self, plan=None, nights=3, adults=2, children=0, rooms=1, **kw):
        return pricing.quote(self.rt, plan or self.plan, d(20), d(20 + nights), adults, children, rooms, **kw)

    def test_basic_room_price_and_tax_slab(self):
        q = self.q()
        self.assertEqual(q["room_charges"], Decimal("15000.00"))   # 5000 x 3
        self.assertEqual(q["tax_total"], Decimal("1800.00"))       # 12% slab (<=7500/night)
        self.assertEqual(q["total"], Decimal("16800.00"))

    def test_higher_slab_uses_18_percent(self):
        DailyRate.objects.create(room_type=self.rt, date=d(20), price=Decimal("9000"))
        q = self.q(nights=1)
        self.assertEqual(q["tax_total"], Decimal("1620.00"))

    def test_extra_adult_and_child_charges_are_configured_not_hardcoded(self):
        q = self.q(adults=3, children=1, nights=2)
        # base occupancy 2 -> 1 extra adult x 1000 x 2 nights + child 500 x 2 nights
        self.assertEqual(q["extra_guest_charges"], Decimal("3000.00"))
        self.rt.extra_adult_charge = Decimal("1500")
        self.rt.save()
        self.assertEqual(self.q(adults=3, children=1, nights=2)["extra_guest_charges"], Decimal("4000.00"))

    def test_meals_per_guest_per_night(self):
        q = self.q(plan=self.bf, adults=2, children=0, nights=3)
        self.assertEqual(q["meal_charges"], Decimal("4800.00"))    # 800 x 2 x 3
        self.assertIn(Decimal("240.00"), [t["amount"] for t in q["tax_lines"]])  # 5% of meals

    def test_weekend_rule_and_explicit_price_override(self):
        PricingRule.objects.create(name="Weekend", kind="WEEKEND", weekdays="0,1,2,3,4,5,6", adjustment_type="PERCENT", value=Decimal("20"))
        self.assertEqual(self.q(nights=1)["room_charges"], Decimal("6000.00"))
        DailyRate.objects.create(room_type=self.rt, date=d(20), price=Decimal("4200"))
        self.assertEqual(self.q(nights=1)["room_charges"], Decimal("4200.00"))

    def test_rooms_multiply_and_guests_are_distributed(self):
        q = self.q(adults=4, rooms=2, nights=1)
        self.assertEqual(q["room_charges"], Decimal("10000.00"))
        self.assertEqual(q["extra_guest_charges"], Decimal("0.00"))  # 2 adults per room, no extras

    def test_percent_coupon_with_cap_and_tax_on_discounted_amount(self):
        Coupon.objects.create(code="SAVE10", title="Ten", discount_type="PERCENT", value=10, max_discount=1000)
        q = self.q(coupon_code="save10")
        self.assertEqual(q["discount"], Decimal("1000.00"))        # 10% of 15000 = 1500, capped at 1000
        self.assertEqual(q["tax_total"], Decimal("1680.00"))       # 12% of 14000
        self.assertEqual(q["total"], Decimal("15680.00"))

    def test_invalid_and_expired_coupons_are_rejected(self):
        with self.assertRaises(pricing.CouponError):
            self.q(coupon_code="NOPE")
        Coupon.objects.create(code="OLD", title="Old", value=5, valid_to=d(-1))
        with self.assertRaises(pricing.CouponError):
            self.q(coupon_code="OLD")
        Coupon.objects.create(code="MIN5", title="5n", value=5, min_nights=5)
        with self.assertRaises(pricing.CouponError):
            self.q(coupon_code="MIN5", nights=2)

    def test_services_are_charged_per_configured_basis(self):
        s1 = Service.objects.create(name="Pickup", price=Decimal("1500"), charge_type="PER_STAY")
        s2 = Service.objects.create(name="Extra bed", price=Decimal("1000"), charge_type="PER_NIGHT")
        q = self.q(service_ids=[s1.id, s2.id], nights=2)
        self.assertEqual(q["service_charges"], Decimal("3500.00"))

    def test_capacity_validation(self):
        with self.assertRaises(pricing.PricingError) as e:
            self.q(adults=4)
        self.assertEqual(e.exception.message, "Number of guests exceeds room capacity.")
        with self.assertRaises(pricing.PricingError):
            self.q(adults=2, children=2)  # max_children = 1
        with self.assertRaises(pricing.PricingError):
            self.q(adults=1, rooms=2)     # each room needs an adult

    def test_date_validation(self):
        with self.assertRaises(pricing.PricingError) as e:
            pricing.quote(self.rt, self.plan, d(5), d(5), 2, 0, 1)
        self.assertEqual(e.exception.message, "Check-out date must be after check-in date.")
        with self.assertRaises(pricing.PricingError):
            pricing.quote(self.rt, self.plan, d(-3), d(-1), 2, 0, 1)

    def test_restrictions_block_guests_but_reception_can_override(self):
        DailyRate.objects.create(room_type=self.rt, date=d(20), closed_to_arrival=True)
        with self.assertRaises(pricing.PricingError):
            self.q()
        self.assertEqual(self.q(staff=True)["nights"], 3)   # reception may override channel restrictions
        DailyRate.objects.filter(room_type=self.rt).delete()
        DailyRate.objects.create(room_type=self.rt, date=d(21), stop_sell=True)
        with self.assertRaises(pricing.PricingError):
            self.q()
        DailyRate.objects.filter(room_type=self.rt).delete()
        DailyRate.objects.create(room_type=self.rt, date=d(20), min_stay=4)
        with self.assertRaises(pricing.PricingError):
            self.q(nights=2)
        self.assertEqual(self.q(nights=4)["nights"], 4)
        DailyRate.objects.filter(room_type=self.rt).delete()
        DailyRate.objects.create(room_type=self.rt, date=d(23), closed_to_departure=True)
        with self.assertRaises(pricing.PricingError):
            self.q(nights=3)

    def test_rate_plan_adjustment_and_minimum_stay(self):
        saver = make_plan(self.rt, "Saver", adjustment_type="PERCENT", adjustment_value=Decimal("-10"), refundable=False, min_stay=2)
        self.assertEqual(self.q(plan=saver, nights=2)["room_charges"], Decimal("9000.00"))
        with self.assertRaises(pricing.PricingError):
            self.q(plan=saver, nights=1)
