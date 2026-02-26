from django.core.management.base import BaseCommand, CommandError
from importlib import import_module
import inspect
import time
from django.test import RequestFactory
from django.contrib.auth.models import AnonymousUser

# query capture
from django.db import connection, reset_queries
from django.conf import settings


class Command(BaseCommand):
    help = "Profile a Python callable for ORM cost analysis."

    def add_arguments(self, parser):
        parser.add_argument(
            "target",
            type=str,
            help="Dotted path to callable (e.g. myapp.views:my_function)",
        )

    def handle(self, *args, **options):
        target = options["target"]

        obj = self.resolve_target(target)

        if not callable(obj):
            raise CommandError("Target is not callable.")

        sig = inspect.signature(obj)
        params = sig.parameters

        factory = RequestFactory()

        # ---- Argument Preparation Phase ----
        call_args = []

        if "request" in params:
            self.stdout.write("Injecting mock HttpRequest...")
            request = factory.get("/", HTTP_HOST="localhost")
            request.user = AnonymousUser()
            call_args.append(request)

        elif len(params) == 0:
            pass

        else:
            raise CommandError(
                f"Unsupported signature: {sig}"
            )

        # ---- Execution Phase ----
        self.stdout.write(self.style.SUCCESS("Callable resolved. Executing..."))

        # Ensure DEBUG is enabled
        if not settings.DEBUG:
            self.stdout.write(
                self.style.WARNING("DEBUG=False — query capture may not work.")
            )

        reset_queries()

        start = time.perf_counter()

        try:
            result = obj(*call_args)
        except Exception as e:
            raise CommandError(f"Execution failed: {e}")

        end = time.perf_counter()

        queries = connection.queries

        query_count = len(queries)
        total_db_time = sum(float(q["time"]) for q in queries)

        self.stdout.write("\n--- ORM COST REPORT ---")
        self.stdout.write(f"Total queries: {query_count}")
        self.stdout.write(f"Total DB time: {total_db_time*1000:.2f} ms")
        self.stdout.write(f"Total execution time: {(end - start)*1000:.2f} ms")

    def resolve_target(self, target: str):
        if ":" not in target:
            raise CommandError(
                "Target must be in format module.path:object_name"
            )

        module_path, object_name = target.split(":", 1)

        try:
            module = import_module(module_path)
        except ModuleNotFoundError:
            raise CommandError(f"Module '{module_path}' not found.")

        if not hasattr(module, object_name):
            raise CommandError(
                f"Object '{object_name}' not found in module '{module_path}'."
            )

        return getattr(module, object_name)