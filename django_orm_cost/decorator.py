import time
import re
from functools import wraps
from collections import Counter
from django.test.utils import override_settings
from django.db import connection
# connection.queries is a list of dicts {"sql" :, "time" : } executed during the request cycle
# it is like a logbook that we can inspect to see what SQL was run and how long it took
from django.db.models.signals import post_init
from django.apps import apps


PURPLE = "\033[95m"
PINK = "\033[94m"
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
ORANGE = "\033[38;5;208m"
RED = "\033[91m"
RESET = "\033[0m"


class FieldUsageTracker:
    def __init__(self):
        self.used_fields = {}
        self._patched_models = {}
        # New: Track which query index created which instance
        self.query_to_instances = {} 

    def patch_model(self, model):
        if model in self._patched_models:
            return

        original_getattribute = model.__getattribute__
        tracker = self

        def patched_getattribute(instance, name):
            # Always use object.__getattribute__ to avoid recursion
            try:
                meta = object.__getattribute__(instance, "_meta")
                fields = {f.name for f in meta.fields}
            except Exception:
                fields = set()

            # Only track actual model fields
            if name in fields:
                instance_id = id(instance)
                tracker.used_fields.setdefault(instance_id, set()).add(name)

            # Call original Django behavior
            return original_getattribute(instance, name)

        model.__getattribute__ = patched_getattribute
        self._patched_models[model] = original_getattribute

    def record_creation(self, sender, instance, **kwargs):
        # Tie this instance to the most recent query index
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
    # Clean up table aliases (e.g., "myapp_table"."column" -> column)
    clean_columns = [col.split('.')[-1].replace('"', '').strip() for col in columns]
    return clean_columns


def track_orm_cost(view_func):
    """
    Minimal version:
    Logs total execution time for the decorated view.
    """

    # When Django starts and imports view *.py files, @track_orm_cost is executed
    # The original view function is passed in as `view_func` and we return a new function `wrapper`
    # `wrapper` will be called instead of the original view when an HTTP request comes in
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        tracker = FieldUsageTracker()

        # Patch ALL registered models
        for model in apps.get_models():
            tracker.patch_model(model)
        
        # Connect signal to catch which query creates which object
        post_init.connect(tracker.record_creation)

        with override_settings(DEBUG=True):
            # *args = URL params, **kwargs = query params
            start = time.perf_counter()
            start_queries = len(connection.queries)
            # perf_counter = high resolution timer, good for measuring short durations

            response = view_func(request, *args, **kwargs) # Call the original view

            end = time.perf_counter()
            end_queries = len(connection.queries)

        post_init.disconnect(tracker.record_creation)
        tracker.unpatch_all()

        total_time = (end - start) * 1000  # ms
        view_queries = connection.queries[start_queries:end_queries]

        print(f"\n{PURPLE}--- Analysis for {view_func.__name__} ---{RESET}")
        print(f"{PURPLE}Time: {total_time:.2f}ms | Queries: {len(view_queries)}{RESET}")
        
        # 1. Exact Duplicates (Keeping your previous logic)
        sql_statements = [q['sql'] for q in view_queries]
        sql_counts = Counter(sql_statements)

        # 2. Detailed QuerySet Analysis
        for i, q in enumerate(view_queries, 0): # Using 0-index for correlation
            sql = q['sql']
            
            # Identify which objects belonged specifically to this query
            current_query_global_idx = start_queries + i
            instance_ids = tracker.query_to_instances.get(current_query_global_idx, set())
            
            consumed = set()
            for inst_id in instance_ids:
                if inst_id in tracker.used_fields:
                    consumed.update(tracker.used_fields[inst_id])

            fetched = extract_fields_from_sql(sql)
            
            # Filter consumed to only show fields that were actually in the 'fetched' list
            actual_consumed = [f for f in consumed if f in fetched or any(f in col for col in fetched)]

            print(f"\n{CYAN}{i+1}. QuerySet Analysis:{RESET}")
            print(f"{CYAN}SQL Trace: {sql[:100]}...{RESET}")
            print(f"{YELLOW}Fields fetched  = {fetched}{RESET}")
            print(f"{GREEN}Fields consumed = {actual_consumed}{RESET}")
            print(f"{PINK}Suggested QuerySet:- [Pending Logic]{RESET}")

        # 2. Summary of inefficiencies
        duplicates = {sql: count for sql, count in sql_counts.items() if count > 1}
        if duplicates:
            # Your existing duplicate summary logic remains here...
            if len(duplicates) >= 1:
                 print(f"\n{ORANGE}⚠ CRITICAL: Duplicate Queries Detected{RESET}")

        return response

    return wrapper