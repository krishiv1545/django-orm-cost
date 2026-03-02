import time
import re
import inspect
from functools import wraps
from django.test.utils import override_settings
from django.db import connection
from django.db.models.query import QuerySet
# connection.queries is a list of dicts {"sql" :, "time" : } executed during the request cycle
# it is like a logbook that we can inspect to see what SQL was run and how long it took
from django.db.models.signals import post_init
from django.apps import apps


BOLD = "\033[1m"
BLUE = "\033[38;5;39m"        # Electric Blue
SKY = "\033[38;5;117m"         # Sky Blue
LIME = "\033[38;5;118m"        # Vivid Lime
GOLD = "\033[38;5;220m"        # Deep Gold
WHITE = "\033[38;5;255m"       # Pure White
GRAY = "\033[38;5;244m"        # Dim Gray
CRIMSON = "\033[38;5;196m"     # Warning Red
RESET = "\033[0m"


class FieldUsageTracker:
    def __init__(self):
        self.used_fields = {}        # instance_id -> set of paths
        self.instance_to_path = {}   # instance_id -> path prefix
        self.query_to_instances = {} 
        self._patched_models = {}

    def patch_model(self, model):
        if model in self._patched_models: return
        original_getattribute = model.__getattribute__
        tracker = self

        def patched_getattribute(instance, name):
            instance_id = id(instance)
            try:
                meta = object.__getattribute__(instance, "_meta")
                field = meta.get_field(name)

                # Track relationship lineage
                if field.is_relation and not name.endswith('_id'):
                    related_obj = original_getattribute(instance, name)
                    if related_obj:
                        prefix = tracker.instance_to_path.get(instance_id, "")
                        tracker.instance_to_path[id(related_obj)] = f"{prefix}{name}__"
                    return related_obj

                # Track data usage
                if name in {f.name for f in meta.fields}:
                    prefix = tracker.instance_to_path.get(instance_id, "")
                    tracker.used_fields.setdefault(instance_id, set()).add(f"{prefix}{name}")
            except Exception: pass
            return original_getattribute(instance, name)

        model.__getattribute__ = patched_getattribute
        self._patched_models[model] = original_getattribute

    def record_creation(self, sender, instance, **kwargs):
        query_idx = len(connection.queries) - 1
        self.query_to_instances.setdefault(query_idx, set()).add(id(instance))

    def unpatch_all(self):
        for model, original in self._patched_models.items():
            model.__getattribute__ = original
        self._patched_models.clear()


def extract_fields_from_sql(sql):
    """
    Attempts to pull column names from a SELECT statement.
    """
    match = re.search(r'SELECT\s+(.*?)\s+FROM', sql, re.IGNORECASE | re.DOTALL)
    if not match:
        return ["*"]
    columns = match.group(1).split(',')
    clean_columns = [col.split('.')[-1].replace('"', '').strip() for col in columns]
    return clean_columns


def filter_id(fields):
    return [
        f for f in fields
        if f != "id" and not f.endswith("_id")
    ]


def track_orm_cost(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        tracker = FieldUsageTracker()
        original_fetch_all = QuerySet._fetch_all
        qs_origins = {}

        def patched_fetch_all(self):
            stack = inspect.stack()
            file, line = "Unknown", 0
            for frame in stack:
                if "django" in frame.filename or "site-packages" in frame.filename: continue
                file, line = frame.filename, frame.lineno
                break
            q_idx = len(connection.queries)
            result = original_fetch_all(self)
            qs_origins[q_idx] = (file, line)
            return result

        QuerySet._fetch_all = patched_fetch_all
        for model in apps.get_models(): tracker.patch_model(model)
        post_init.connect(tracker.record_creation)

        with override_settings(DEBUG=True):
            start = time.perf_counter()
            start_queries = len(connection.queries)
            response = view_func(request, *args, **kwargs)
            end_queries = len(connection.queries)
            total_time = (time.perf_counter() - start) * 1000

        post_init.disconnect(tracker.record_creation)
        tracker.unpatch_all()
        QuerySet._fetch_all = original_fetch_all

        view_queries = connection.queries[start_queries:end_queries]
        print(f"\n{BOLD}{BLUE}═══ Analysis: {view_func.__name__} ═══{RESET}")
        print(f"{BLUE}Time: {total_time:.2f}ms | Total Queries: {len(view_queries)}{RESET}")

        logical_count = 1
        skip = False
        for i in range(len(view_queries)):
            if skip: (skip := False); continue

            q = view_queries[i]
            global_idx = start_queries + i
            is_prefetch = False
            
            # Combine logic for prefetch pairs
            if i + 1 < len(view_queries):
                if " WHERE " in view_queries[i+1]['sql'].upper() and " IN " in view_queries[i+1]['sql'].upper():
                    is_prefetch = True

            # Data Collection
            inst_ids = tracker.query_to_instances.get(global_idx, set())
            if is_prefetch: inst_ids.update(tracker.query_to_instances.get(global_idx + 1, set()))
            
            consumed_paths = set()
            for obj_id in inst_ids:
                if obj_id in tracker.used_fields: consumed_paths.update(tracker.used_fields[obj_id])

            fetched_raw = extract_fields_from_sql(q['sql'])
            if is_prefetch: fetched_raw += extract_fields_from_sql(view_queries[i+1]['sql'])

            # Efficiency Calc
            f_clean = filter_id(fetched_raw)
            c_clean = filter_id(consumed_paths)
            efficiency = round((len(c_clean) / max(len(f_clean), 1)) * 100, 2)

            # --- OUTPUT ---
            print(f"\n{BOLD}{SKY}{logical_count}. QuerySet Analysis{RESET}")
            origin = qs_origins.get(global_idx, ("Unknown", 0))
            print(f"   {GRAY}Location: {origin[0]}:{origin[1]}{RESET}")
            print(f"   {SKY}SQL: {q['sql'][:120]}...{RESET}")
            
            print(f"   {GOLD}Fields Fetched  = {f_clean}{RESET}")
            print(f"   {LIME}Fields Consumed = {sorted(list(c_clean))}{RESET}")

            eff_color = LIME if efficiency > 80 else GOLD if efficiency > 40 else CRIMSON
            print(f"   {BOLD}{eff_color}Efficiency: {len(c_clean)}/{len(f_clean)} fields used ({100-efficiency}% over-fetched){RESET}")
            
            if efficiency < 100:
                sugg = f".only({', '.join(repr(f) for f in sorted(c_clean))})"
                print(f"   {WHITE}💡 Suggestion: {sugg}{RESET}")

            if is_prefetch: skip = True
            logical_count += 1
        print("")

        return response
    return wrapper