"""Apply the Hotel Terrazzo Suites brand assets (logo, hero, real room photography) to the database. Idempotent."""
from django.core.management.base import BaseCommand

from hotel.models import Hotel, RoomImage, RoomType

B = "/static/brand/"
# room type name -> [(file, caption), ...]; first image is the primary photo
PHOTOS = {
    "Standard Room": [("room-deluxe", "Bedroom"), ("room-balcony", "Balcony & view"), ("bathroom", "Bathroom"), ("corridor", "Floor corridor")],
    "Deluxe Room": [("room-king", "Bedroom & seating"), ("room-window", "Window view"), ("room-terrace-view", "Terrace outlook"), ("bathroom", "Bathroom")],
    "Premium Room": [("room-balcony", "Bedroom & balcony"), ("room-deluxe", "Skyline view"), ("room-lounge", "Dining nook"), ("bathroom", "Bathroom")],
    "Executive Room": [("room-window", "Bedroom & work area"), ("room-suite-bed", "King bed"), ("room-lounge", "Seating area"), ("bathroom", "Bathroom")],
    "Terrazzo Suite": [("room-lounge", "Suite lounge"), ("room-king", "Bedroom"), ("room-suite-bed", "Skyline windows"), ("bathroom", "Bathroom")],
}
DESCRIPTION = ("Hotel Terrazzo Suites is a boutique city hotel with bright, well-appointed rooms and a rooftop dining restaurant. "
               "Floor-to-ceiling windows, warm wood floors and thoughtful details make every stay feel easy. Managed by Aarushi Hospitality.")


class Command(BaseCommand):
    help = "Apply logo, hero image, copy and real photography to the hotel and room types."

    def handle(self, *a, **o):
        hotel = Hotel.get()
        if not hotel:
            self.stdout.write("No hotel yet — run seed_demo first.")
            return
        hotel.name = "Hotel Terrazzo Suites"
        hotel.tagline = "Rooftop dining & restaurant · Managed by Aarushi Hospitality"
        hotel.description = DESCRIPTION
        # Real location (from public listings: Treebo, EaseMyTrip, Booking, Hotels.com)
        hotel.address = "The Obelisk, Science City Road, Opp. Shell Petrol Pump, Sola"
        hotel.city, hotel.state, hotel.postal_code, hotel.country = "Ahmedabad", "Gujarat", "380060", "India"
        hotel.latitude = hotel.longitude = None  # old demo coordinates pointed at Bengaluru; maps links fall back to the address
        hotel.logo_url = B + "logo-mark.png"
        hotel.hero_image_url = B + "room-king.jpg"
        hotel.save()
        n = 0
        for rt in RoomType.objects.all():
            photos = PHOTOS.get(rt.name)
            if not photos:
                continue
            rt.images.all().delete()
            for i, (f, cap) in enumerate(photos):
                RoomImage.objects.create(room_type=rt, image_url=f"{B}{f}.jpg", caption=cap, display_order=i, is_primary=(i == 0))
                n += 1
        self.stdout.write(self.style.SUCCESS(f"Branding applied ({n} room photos)."))
