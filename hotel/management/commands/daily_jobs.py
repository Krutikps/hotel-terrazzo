"""Daily housekeeping: pre-arrival & checkout reminders, no-show marking. Schedule once a day (e.g. 08:00)."""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from hotel.models import Notification, Reservation
from hotel.services import notifications, reservations


class Command(BaseCommand):
    help = "Send pre-arrival / check-out reminders and mark no-shows."

    def handle(self, *args, **opts):
        today = timezone.localdate()
        sent = 0
        for r in Reservation.objects.filter(status=Reservation.CONFIRMED, check_in=today + timedelta(days=1)):
            if not Notification.objects.filter(reservation=r, event="PRE_ARRIVAL").exists():
                notifications.send("PRE_ARRIVAL", r.id)
                sent += 1
        for r in Reservation.objects.filter(status=Reservation.CHECKED_IN, check_out=today):
            if not Notification.objects.filter(reservation=r, event="CHECKOUT_REMINDER").exists():
                notifications.send("CHECKOUT_REMINDER", r.id)
                sent += 1
        no_shows = reservations.mark_no_shows(today)
        released = reservations.expire_holds()
        self.stdout.write(f"Reminders: {sent}, no-shows marked: {no_shows}, holds released: {released}")
