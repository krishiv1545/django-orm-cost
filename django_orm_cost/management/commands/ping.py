from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Simple ping command to verify django-endpoint-profiler is installed."

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("Pong ~ Marty Supreme"))
        