from django.core.management.base import BaseCommand
from django.utils import timezone
import time
import logging

from api.dispatch import dispatch_tick

log = logging.getLogger("rydnow.dispatch")

class Command(BaseCommand):
    help = "Run dispatch loop — offer jobs to riders continuously"

    def handle(self, *args, **options):
        self.stdout.write("🚀 Dispatch engine started. Press Ctrl+C to stop.")
        log.info("dispatch: worker started")
        
        try:
            while True:
                result = dispatch_tick()
                self.stdout.write(
                    f"[{timezone.now():%H:%M:%S}] "
                    f"Expired: {result['expired']} | "
                    f"Waiting: {result['waiting']} | "
                    f"Offered: {result['offered']}"
                )
                time.sleep(3)  # Run every 3 seconds
        except KeyboardInterrupt:
            self.stdout.write("\n🛑 Dispatch stopped.")
            log.info("dispatch: worker stopped")
