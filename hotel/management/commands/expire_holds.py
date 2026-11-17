"""Release lapsed payment holds. Run every minute from cron / a scheduler (or use --loop)."""
import time

from django.core.management.base import BaseCommand

from hotel.services import reservations


class Command(BaseCommand):
    help = "Release PENDING_PAYMENT reservations whose hold has expired, returning the rooms to inventory."

    def add_arguments(self, parser):
        parser.add_argument("--loop", type=int, default=0, help="Keep running, sweeping every N seconds")

    def handle(self, *args, **opts):
        while True:
            n = reservations.expire_holds()
            if n or not opts["loop"]:
                self.stdout.write(f"Released {n} expired hold(s).")
            if not opts["loop"]:
                break
            time.sleep(opts["loop"])
