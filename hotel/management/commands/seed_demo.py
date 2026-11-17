"""Seed the real database with realistic demo data (hotel, rooms, rates, guests, bookings, payments, blocks…)."""
import random
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import Role, User
from accounts.permissions import ensure_default_roles
from hotel.models import (Review, Amenity, Coupon, DailyRate, Guest, HousekeepingLog, Hotel, MaintenanceTicket, Payment, PricingRule,
                          RatePlan, Reservation, ReservationRequest, ReservationRoom, Room, RoomBlock, RoomImage, RoomType, Service, TaxRule)
from hotel.services import availability, reservations as rs

DEMO_PASSWORD = "Terrazzo@123"

AMENITIES = [("Wi-Fi", "📶"), ("Air conditioning", "❄️"), ("Smart TV", "📺"), ("Mini fridge", "🧊"), ("In-room safe", "🔒"), ("Hair dryer", "💨"),
             ("Tea & coffee maker", "☕"), ("Rain shower", "🚿"), ("Bathtub", "🛁"), ("Balcony", "🌇"), ("Work desk", "💼"),
             ("Bathrobe & slippers", "🥿"), ("Living area", "🛋️"), ("Nespresso machine", "☕"), ("Sofa bed", "🛏️")]

ROOM_TYPES = [
    # name, short, size, bed, adults, children, occ, base_occ, rate, extra_adult, child, floor, numbers, amenities
    ("Standard Room", "Calm, compact comfort for the solo or couple traveller.", 240, "Queen bed", 2, 1, 3, 2, 3500, 1000, 500, 1, ["101", "102", "103", "104", "105", "106"],
     ["Wi-Fi", "Air conditioning", "Smart TV", "Mini fridge", "In-room safe", "Hair dryer", "Tea & coffee maker"]),
    ("Deluxe Room", "Generous space with a private balcony and rain shower.", 320, "King bed", 3, 1, 3, 2, 5000, 1000, 500, 2, ["201", "202", "203", "204", "205", "206"],
     ["Wi-Fi", "Air conditioning", "Smart TV", "Mini fridge", "In-room safe", "Hair dryer", "Tea & coffee maker", "Rain shower", "Balcony", "Work desk"]),
    ("Premium Room", "Higher floors, garden views and a curated minibar.", 380, "King bed", 3, 2, 4, 2, 6500, 1200, 600, 3, ["301", "302", "303", "304"],
     ["Wi-Fi", "Air conditioning", "Smart TV", "Mini fridge", "In-room safe", "Hair dryer", "Nespresso machine", "Rain shower", "Balcony", "Bathrobe & slippers", "Work desk"]),
    ("Executive Room", "A work-ready retreat with lounge access and a separate desk area.", 450, "King bed", 3, 2, 4, 2, 8000, 1500, 700, 4, ["401", "402", "403", "404"],
     ["Wi-Fi", "Air conditioning", "Smart TV", "Mini fridge", "In-room safe", "Hair dryer", "Nespresso machine", "Rain shower", "Bathtub", "Work desk", "Living area", "Bathrobe & slippers"]),
    ("Terrazzo Suite", "Our signature suite: bedroom, living room and a wraparound terrace.", 720, "King bed + sofa bed", 4, 2, 5, 2, 12000, 2000, 900, 5, ["501", "502", "503"],
     ["Wi-Fi", "Air conditioning", "Smart TV", "Mini fridge", "In-room safe", "Hair dryer", "Nespresso machine", "Rain shower", "Bathtub", "Balcony", "Living area", "Sofa bed", "Bathrobe & slippers"]),
]
CAPTIONS = ["Bedroom", "Bathroom", "Balcony & view", "Lounge corner"]


class Command(BaseCommand):
    help = "Seed demo data. Use --reset to wipe existing hotel data first."

    def add_arguments(self, p):
        p.add_argument("--reset", action="store_true", help="Delete all hotel data (keeps users) before seeding")
        p.add_argument("--password", default=DEMO_PASSWORD, help="Password for demo staff accounts")

    def handle(self, *a, **o):
        if Hotel.objects.exists() and not o["reset"]:
            raise CommandError("Data already exists. Re-run with --reset to wipe and re-seed.")
        random.seed(42)
        settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"  # don't spam the console while seeding
        if o["reset"]:
            self._wipe()
        ensure_default_roles()
        with transaction.atomic():
            hotel = self._hotel()
            hotel.refresh_from_db()
            self._users(o["password"])
            types = self._rooms(hotel)
            self._rates(types)
            guests = self._guests()
        self._bookings(hotel, types, guests)
        self._ops(types)
        from django.core.management import call_command

        call_command("apply_branding", stdout=self.stdout)   # real logo, hero & room photography
        self.stdout.write(self.style.SUCCESS("Demo data ready."))
        self.stdout.write(f"Staff logins (password {o['password']}): admin@terrazzo.test, manager@terrazzo.test, reception@terrazzo.test, housekeeping@terrazzo.test, accounts@terrazzo.test")

    def _wipe(self):
        from hotel import models as m

        for model in (m.Notification, m.AuditLog, m.Invoice, m.Payment, m.ReservationLine, m.ReservationRequest, m.ReservationRoom, m.Reservation, m.Guest,
                      m.HousekeepingLog, m.MaintenanceTicket, m.RoomBlock, m.DailyRate, m.PricingRule, m.RatePlan, m.Coupon, m.Service, m.Room,
                      m.RoomImage, m.RoomType, m.Amenity, m.TaxRule, m.Review, m.Counter, m.Hotel):
            model.objects.all().delete()

    # ------------------------------------------------------------------
    def _hotel(self):
        return Hotel.objects.create(
            name="Hotel Terrazzo Suites", tagline="Warm stone, quiet luxury, unhurried stays",
            description="A boutique city hotel inspired by terrazzo craft: speckled stone floors, warm terracotta accents and calm, light-filled rooms. "
                        "Enjoy an all-day restaurant, rooftop pool, spa and a team that remembers your name.",
            address="The Obelisk, Science City Road, Opp. Shell Petrol Pump, Sola", city="Ahmedabad", state="Gujarat", country="India", postal_code="380060",
            phone="+91 80 4000 1234", email="reservations@terrazzosuites.example", website="https://terrazzosuites.example", gstin="29ABCDE1234F1Z5",
            booking_prefix="HTS", hold_minutes=15, payment_provider=settings.PAYMENT_PROVIDER if settings.PAYMENT_PROVIDER in ("mock", "razorpay") else "mock",
            hero_image_url="/placeholder/hero.svg?t=Hotel+Terrazzo+Suites",             admin_notification_email="frontdesk@terrazzosuites.example",
        )

    def _users(self, pw):
        roles = {r.code: r for r in Role.objects.all()}
        for email, name, role in [("admin@terrazzo.test", "Aarav Admin", "super_admin"), ("manager@terrazzo.test", "Meera Manager", "hotel_manager"),
                                  ("reception@terrazzo.test", "Riya Reception", "receptionist"), ("housekeeping@terrazzo.test", "Hari Housekeeping", "housekeeping"),
                                  ("accounts@terrazzo.test", "Anita Accounts", "accountant")]:
            u, _ = User.objects.get_or_create(email=email, defaults={"first_name": name.split()[0], "last_name": name.split()[1], "role": roles[role],
                                                                      "is_superuser": role == "super_admin", "phone": "+91 90000 0000" + str(random.randint(0, 9))})
            u.set_password(pw)
            u.role = roles[role]
            u.save()

    def _rooms(self, hotel):
        for n, r, mn, mx, sa in [("GST (room ≤ ₹7,500)", 12, 0, 7500, "ROOM"), ("GST (room > ₹7,500)", 18, 7500, None, "ROOM"), ("GST on food", 5, 0, None, "MEAL"),
                                 ("GST on services", 18, 0, None, "SERVICE")]:
            TaxRule.objects.create(hotel=hotel, name=n, rate=r, min_amount=mn, max_amount=mx, applies_to=sa)
        am = {n: Amenity.objects.create(name=n, icon=i) for n, i in AMENITIES}
        types = {}
        for order, (name, short, size, bed, ma, mc, mo, bo, rate, ea, cc, floor, numbers, amen) in enumerate(ROOM_TYPES):
            rt = RoomType.objects.create(hotel=hotel, name=name, short_description=short, size_sqft=size, bed_type=bed, max_adults=ma, max_children=mc, max_occupancy=mo,
                                         base_occupancy=bo, base_rate=rate, extra_adult_charge=ea, child_charge=cc, display_order=order,
                                         description=f"{short} Designed with locally crafted terrazzo, soft linens and thoughtful lighting. {name} offers {bed.lower()}, "
                                                     f"{size} sq ft of space and everything you need for a restful stay.")
            rt.amenities.set([am[a] for a in amen])
            for i, cap in enumerate(CAPTIONS):
                RoomImage.objects.create(room_type=rt, image_url=f"/placeholder/{rt.slug}-{i + 1}.svg?t={name.replace(' ', '+')}+·+{cap.replace(' ', '+').replace('&', 'and')}", caption=cap,
                                         display_order=i, is_primary=(i == 0))
            for num in numbers:
                Room.objects.create(room_number=num, room_type=rt, floor=int(num[0]), status=Room.CLEAN)
            types[name] = rt
        return types

    def _rates(self, types):
        today = timezone.localdate()
        for name, rt in types.items():
            premium = name in ("Premium Room", "Executive Room", "Terrazzo Suite")
            RatePlan.objects.create(room_type=rt, name="Room Only", description="Room only, no meals.", meal_plan="ROOM_ONLY", refundable=True, free_cancel_hours=24, display_order=1)
            RatePlan.objects.create(room_type=rt, name="Breakfast Included", description="Room with daily breakfast buffet for every guest.", meal_plan="BREAKFAST",
                                    meal_adult_price=Decimal("600"), meal_child_price=Decimal("300"), refundable=True, free_cancel_hours=24, display_order=2)
            if name != "Standard Room":
                RatePlan.objects.create(room_type=rt, name="Half Board", description="Breakfast and dinner included.", meal_plan="HALF_BOARD", meal_adult_price=Decimal("1200"),
                                        meal_child_price=Decimal("600"), refundable=True, free_cancel_hours=48, display_order=3)
            if premium:
                RatePlan.objects.create(room_type=rt, name="Full Board", description="Breakfast, lunch and dinner included.", meal_plan="FULL_BOARD", meal_adult_price=Decimal("1800"),
                                        meal_child_price=Decimal("900"), refundable=True, free_cancel_hours=48, display_order=4)
            RatePlan.objects.create(room_type=rt, name="Non-Refundable Saver", description="Our lowest rate. Prepaid, no refund or changes.", meal_plan="ROOM_ONLY",
                                    adjustment_type="PERCENT", adjustment_value=Decimal("-12"), refundable=False, cancel_penalty_percent=100, min_stay=1, display_order=5)
            RatePlan.objects.create(room_type=rt, name="Corporate Rate", description="Negotiated corporate tariff with breakfast.", meal_plan="BREAKFAST", meal_adult_price=Decimal("400"),
                                    adjustment_type="PERCENT", adjustment_value=Decimal("-15"), corporate=True, free_cancel_hours=24, display_order=6)
        Service.objects.bulk_create([
            Service(name="Airport pickup", description="Sedan transfer from the airport", price=Decimal("1500"), charge_type="PER_STAY"),
            Service(name="Extra bed", description="Rollaway bed in room", price=Decimal("1000"), charge_type="PER_NIGHT"),
            Service(name="Baby cot", description="Complimentary cot", price=Decimal("0"), charge_type="PER_STAY", taxable=False),
            Service(name="Anniversary decoration", description="Flowers, cake & candles", price=Decimal("2500"), charge_type="PER_STAY"),
            Service(name="Late checkout (until 4pm)", description="Subject to availability", price=Decimal("1500"), charge_type="PER_STAY", bookable_online=False),
        ])
        PricingRule.objects.create(name="Weekend uplift", kind="WEEKEND", weekdays="4,5", adjustment_type="PERCENT", value=Decimal("20"), priority=10)
        y = today.year if today < date(today.year, 12, 15) else today.year
        PricingRule.objects.create(name="Festive season", kind="SEASONAL", start_date=date(y, 12, 15), end_date=date(y + 1, 1, 5), adjustment_type="PERCENT", value=Decimal("35"), priority=20)
        PricingRule.objects.create(name="Republic Day long weekend", kind="HOLIDAY", start_date=date(y + 1, 1, 24), end_date=date(y + 1, 1, 26), adjustment_type="PERCENT", value=Decimal("25"), priority=30)
        PricingRule.objects.create(name="Terrazzo Music Weekend", kind="EVENT", start_date=today + timedelta(days=40), end_date=today + timedelta(days=42), adjustment_type="PERCENT", value=Decimal("40"), priority=40)
        # explicit, hand-set prices for the next 10 days (the "pricing calendar" in action)
        bumps = [0, 0, 300, 500, 800, 800, 300, 0, 0, 200]
        for rt in types.values():
            for i, b in enumerate(bumps):
                DailyRate.objects.create(room_type=rt, date=today + timedelta(days=i), price=rt.base_rate + Decimal(b) * (1 + rt.display_order * Decimal("0.4")))
        first = next(iter(types.values()))
        DailyRate.objects.update_or_create(room_type=first, rate_plan=None, date=today + timedelta(days=20), defaults={"closed_to_arrival": True})
        DailyRate.objects.create(room_type=types["Terrazzo Suite"], rate_plan=None, date=today + timedelta(days=40), min_stay=2)
        for n, loc, r, t in [("Ananya S.", "Bengaluru", 5, "Calm, spotless and the breakfast was excellent. Dinner on the rooftop is a must."),
                             ("Rohan M.", "Mumbai", 5, "Check-in took two minutes and the team sorted an early arrival for us. Great value for a business trip."),
                             ("Sara T.", "London", 4, "Beautifully designed rooms and very quiet. Booked directly and got a better rate than the apps.")]:
            Review.objects.create(name=n, location=loc, rating=r, text=t)
        Coupon.objects.create(code="WELCOME10", title="Welcome offer — 10% off", description="10% off your room charges (up to ₹2,000) on any stay.", discount_type="PERCENT", value=10, max_discount=2000, valid_to=today + timedelta(days=365))
        Coupon.objects.create(code="STAY3SAVE", title="Stay 3, save ₹1,500", description="Book 3 nights or more and save ₹1,500 on your room.", discount_type="FIXED", value=1500, min_nights=3, valid_to=today + timedelta(days=365))
        Coupon.objects.create(code="EARLYBIRD15", title="Early bird — 15% off", description="Book at least a month ahead for 15% off.", discount_type="PERCENT", value=15, min_nights=2, max_discount=5000, valid_from=today, valid_to=today + timedelta(days=365))

    def _guests(self):
        data = [("Ananya", "Sharma", "ananya.sharma@example.com", "+91 98450 11001", "Bengaluru", "Karnataka", "India"),
                ("Rohan", "Mehta", "rohan.mehta@example.com", "+91 98450 11002", "Mumbai", "Maharashtra", "India"),
                ("Priya", "Nair", "priya.nair@example.com", "+91 98450 11003", "Kochi", "Kerala", "India"),
                ("Vikram", "Singh", "vikram.singh@example.com", "+91 98450 11004", "Delhi", "Delhi", "India"),
                ("Sara", "Thompson", "sara.thompson@example.com", "+44 7700 900123", "London", "England", "United Kingdom"),
                ("Karthik", "Iyer", "karthik.iyer@example.com", "+91 98450 11006", "Chennai", "Tamil Nadu", "India"),
                ("Neha", "Gupta", "neha.gupta@example.com", "+91 98450 11007", "Pune", "Maharashtra", "India"),
                ("David", "Miller", "david.miller@example.com", "+1 202 555 0147", "New York", "NY", "United States"),
                ("Fatima", "Khan", "fatima.khan@example.com", "+91 98450 11009", "Hyderabad", "Telangana", "India"),
                ("Arjun", "Reddy", "arjun.reddy@example.com", "+91 98450 11010", "Hyderabad", "Telangana", "India"),
                ("Meenal", "Joshi", "meenal.joshi@example.com", "+91 98450 11011", "Ahmedabad", "Gujarat", "India"),
                ("Tom", "Becker", "tom.becker@example.com", "+49 151 2345 6789", "Berlin", "Berlin", "Germany")]
        out = []
        for i, (f, l, e, p, c, s, co) in enumerate(data):
            out.append(Guest.objects.create(first_name=f, last_name=l, email=e, phone=p, city=c, state=s, country=co, address=f"{10 + i} Garden Street",
                                            id_type="Passport" if co != "India" else "Aadhaar", id_number=f"ID{random.randint(10**7, 10**8 - 1)}",
                                            preferences=random.choice(["High floor", "Quiet room", "Extra pillows", "Vegetarian meals", ""]),
                                            notes=""))
        return out

    # ------------------------------------------------------------------
    def _bookings(self, hotel, types, guests):
        today = timezone.localdate()
        admin = User.objects.get(email="reception@terrazzo.test")
        plans = {rt.name: {p.name: p for p in rt.rate_plans.all()} for rt in types.values()}
        N = iter(range(10_000))

        def make(gi, tname, plan, start, nights, adults=2, children=0, rooms=1, source="WEBSITE", purpose="Leisure", reqs=(), notes="", paid="none", coupon=None, status=None, assign=False, created_days_ago=None):
            g = guests[gi]
            gd = {"first_name": g.first_name, "last_name": g.last_name, "email": g.email, "phone": g.phone, "city": g.city, "state": g.state, "country": g.country}
            res = rs.create_reservation(room_type=types[tname], rate_plan=plans[tname][plan], check_in=start, check_out=start + timedelta(days=nights), adults=adults, children=children,
                                        rooms=rooms, guest_data=gd, source=source, purpose=purpose, request_types=list(reqs), special_requests=notes,
                                        coupon_code=coupon, user=admin if source != "WEBSITE" else None)
            if assign or status in ("CHECKED_IN", "CHECKED_OUT"):
                for rr in res.rooms.all():
                    free = availability.assignable_rooms(rr.room_type_id, res.check_in, res.check_out, exclude_reservation_id=res.id).order_by("room_number").first()
                    if free:
                        rr.room = free
                        rr.save()
            if paid != "none":
                amt = res.total if paid == "full" else (res.total * Decimal("0.3")).quantize(Decimal("0.01"))
                online = source == "WEBSITE"
                Payment.objects.create(reservation=res, amount=amt, method="ONLINE" if online else random.choice(["CASH", "CARD", "UPI"]), provider="mock" if online else "",
                                       provider_order_id=f"mock_order_seed{next(N)}" if online else "", provider_payment_id=f"mock_pay_seed{next(N)}" if online else "",
                                       transaction_id=f"TXN{random.randint(10**8, 10**9)}", status=Payment.PAID, paid_at=timezone.now(), created_by=None if online else admin)
                rs.recompute(res)
            if status in ("CHECKED_IN", "CHECKED_OUT"):
                res.status = status
                res.checked_in_at = timezone.make_aware(timezone.datetime.combine(res.check_in, hotel.check_in_time))
                if status == "CHECKED_OUT":
                    res.checked_out_at = timezone.make_aware(timezone.datetime.combine(res.check_out, hotel.check_out_time))
                res.save()
                for rr in res.rooms.select_related("room"):
                    if rr.room:
                        rr.room.status = Room.OCCUPIED if status == "CHECKED_IN" else Room.CLEAN
                        rr.room.save(update_fields=["status"])
                if status == "CHECKED_OUT":
                    rs.create_invoice(res, admin)
            if created_days_ago is not None:
                Reservation.objects.filter(pk=res.pk).update(created_at=timezone.now() - timedelta(days=created_days_ago))
            return res

        d = lambda n: today + timedelta(days=n)
        # completed stays over the last ~5 weeks (feeds the reports & charts)
        past = [(0, "Deluxe Room", "Breakfast Included", -30, 2, 2, 0, "WEBSITE", "Leisure"), (1, "Standard Room", "Room Only", -28, 1, 1, 0, "WALK_IN", "Business"),
                (2, "Premium Room", "Half Board", -26, 3, 2, 1, "WEBSITE", "Family"), (3, "Executive Room", "Room Only", -24, 2, 2, 0, "CORPORATE", "Business"),
                (4, "Terrazzo Suite", "Breakfast Included", -22, 4, 3, 1, "WEBSITE", "Wedding/Event"), (5, "Deluxe Room", "Room Only", -20, 3, 2, 0, "PHONE", "Leisure"),
                (6, "Standard Room", "Non-Refundable Saver", -18, 2, 2, 0, "WEBSITE", "Leisure"), (7, "Executive Room", "Breakfast Included", -16, 5, 1, 0, "WEBSITE", "Business"),
                (8, "Premium Room", "Room Only", -14, 2, 2, 0, "RECEPTION", "Medical"), (9, "Deluxe Room", "Half Board", -12, 3, 2, 1, "WEBSITE", "Family"),
                (10, "Standard Room", "Room Only", -10, 2, 2, 0, "WEBSITE", "Leisure"), (11, "Terrazzo Suite", "Full Board" if False else "Breakfast Included", -9, 3, 2, 0, "WEBSITE", "Leisure"),
                (0, "Premium Room", "Breakfast Included", -7, 2, 2, 0, "WEBSITE", "Leisure"), (3, "Deluxe Room", "Room Only", -6, 2, 2, 0, "EMAIL", "Business"),
                (5, "Standard Room", "Breakfast Included", -5, 1, 2, 0, "WEBSITE", "Leisure"), (7, "Executive Room", "Room Only", -4, 2, 1, 0, "CORPORATE", "Business")]
        for gi, t, p, off, n, a, c, src, purp in past:
            make(gi, t, p, d(off), n, a, c, source=src, purpose=purp, paid="full", status="CHECKED_OUT", created_days_ago=abs(off) + random.randint(3, 20))
        # in-house guests
        make(1, "Deluxe Room", "Breakfast Included", d(-1), 3, 2, 0, source="RECEPTION", paid="full", status="CHECKED_IN", reqs=["Quiet room"], purpose="Business")
        make(4, "Premium Room", "Half Board", d(-2), 4, 2, 1, source="WEBSITE", paid="partial", status="CHECKED_IN", reqs=["Baby cot", "High floor"], purpose="Family")
        make(6, "Standard Room", "Room Only", d(-1), 2, 2, 0, source="WALK_IN", paid="full", status="CHECKED_IN", purpose="Leisure")
        make(8, "Terrazzo Suite", "Breakfast Included", d(0), 3, 2, 0, source="WEBSITE", paid="full", status="CHECKED_IN", reqs=["Anniversary decoration"], purpose="Leisure", notes="Celebrating 10th anniversary — please arrange a cake.")
        # arrivals today / upcoming
        make(2, "Deluxe Room", "Breakfast Included", d(0), 2, 2, 1, source="WEBSITE", paid="full", reqs=["Early check-in"], purpose="Family", notes="Arriving by train at 9 AM; early check-in if possible.", assign=True)
        make(9, "Standard Room", "Room Only", d(0), 1, 2, 0, source="PHONE", paid="none", purpose="Business")
        make(3, "Executive Room", "Room Only", d(1), 3, 2, 0, source="CORPORATE", paid="partial", purpose="Business", reqs=["Late checkout"], assign=True)
        make(5, "Premium Room", "Half Board", d(2), 2, 2, 0, source="WEBSITE", paid="full", purpose="Leisure", reqs=["Airport pickup"], coupon="WELCOME10")
        make(7, "Deluxe Room", "Room Only", d(3), 2, source="WEBSITE", paid="full", purpose="Wedding/Event", reqs=["Extra bed", "Quiet room"], rooms=2, adults=4, children=1, notes="Family attending a wedding nearby.")
        make(10, "Terrazzo Suite", "Breakfast Included", d(5), 2, 2, 0, source="WEBSITE", paid="full", purpose="Leisure", reqs=["Birthday setup"])
        make(11, "Standard Room", "Non-Refundable Saver", d(7), 3, 2, 0, source="WEBSITE", paid="full", purpose="Leisure")
        make(0, "Premium Room", "Breakfast Included", d(14), 3, 3, 0, source="EMAIL", paid="partial", purpose="Family")
        make(6, "Executive Room", "Room Only", d(21), 2, 2, 0, source="WEBSITE", paid="full", purpose="Business")
        make(2, "Deluxe Room", "Breakfast Included", d(30), 5, 2, 0, source="WEBSITE", paid="full", purpose="Leisure", coupon="STAY3SAVE")
        # cancelled
        c1 = make(9, "Deluxe Room", "Room Only", d(10), 2, 2, 0, source="WEBSITE", paid="full", created_days_ago=6)
        rs.cancel(c1.id, user=None, reason="Change of plans", by_guest=True)
        c2 = make(11, "Standard Room", "Non-Refundable Saver", d(12), 2, 2, 0, source="WEBSITE", paid="full", created_days_ago=3)
        rs.cancel(c2.id, user=admin, reason="Guest request (illness)")

    def _ops(self, types):
        today = timezone.localdate()
        mgr = User.objects.get(email="manager@terrazzo.test")
        hk = User.objects.get(email="housekeeping@terrazzo.test")
        reception = User.objects.get(email="reception@terrazzo.test")
        free = Room.objects.exclude(status=Room.OCCUPIED)
        # blocks
        rs.block_room(Room.objects.get(room_number="304"), today + timedelta(days=3), today + timedelta(days=6), "OWNER_USE", "Owner's family visit", mgr)
        rs.block_room(Room.objects.get(room_number="106"), today, today + timedelta(days=1), "CLEANING", "Deep clean + carpet shampoo", reception)
        # maintenance
        rs.create_ticket(Room.objects.get(room_number="203"), "AC not cooling", "HIGH", reception, assigned_to=mgr, notes="Technician called; compressor suspected.")
        old = rs.create_ticket(Room.objects.get(room_number="205"), "Leaking bathroom tap", "MEDIUM", reception, assigned_to=mgr)
        rs.update_ticket(old, mgr, status="RESOLVED", notes="Washer replaced.")
        rs.create_ticket(Room.objects.get(room_number="403"), "Balcony door lock stiff", "LOW", reception, blocks_room=False)
        # housekeeping states
        for num, st in [("102", Room.DIRTY), ("104", Room.CLEANING), ("201", Room.DIRTY), ("302", Room.CLEAN), ("401", Room.INSPECTED), ("205", Room.DIRTY)]:
            room = Room.objects.get(room_number=num)
            if room.status != Room.OCCUPIED:
                rs.set_room_status(room, st, hk, "Seeded housekeeping state")
