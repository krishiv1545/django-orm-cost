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
        
        # 1. Exact Duplicates
        sql_statements = [q['sql'] for q in view_queries]
        sql_counts = Counter(sql_statements)

        # 2. Detailed QuerySet Analysis
        logical_qs_count = 1
        skip_next = False

        for i in range(len(view_queries)):
            if skip_next:
                skip_next = False
                continue

            q = view_queries[i]
            sql = q['sql']
            
            # --- PREFETCH DETECTION LOGIC ---
            # If there's a next query and it looks like a prefetch (contains "IN" and many IDs)
            is_prefetch_parent = False
            if i + 1 < len(view_queries):
                next_sql = view_queries[i+1]['sql']
                if " WHERE " in next_sql.upper() and " IN " in next_sql.upper():
                    is_prefetch_parent = True

            # Process Primary Query
            current_query_global_idx = start_queries + i
            instance_ids = tracker.query_to_instances.get(current_query_global_idx, set())
            consumed = set()
            for inst_id in instance_ids:
                if inst_id in tracker.used_fields:
                    consumed.update(tracker.used_fields[inst_id])
            
            fetched = extract_fields_from_sql(sql)
            actual_consumed = [f for f in consumed if f in fetched or any(f in col for col in fetched)]

            # Print Header
            print(f"\n{CYAN}{logical_qs_count}. QuerySet Analysis:{RESET}")
            print(f"   {CYAN}SQL 1: {sql[:100]}...{RESET}")
            print(f"   {YELLOW}Fields fetched  = {fetched}{RESET}")
            print(f"   {GREEN}Fields consumed = {actual_consumed}{RESET}")

            # Process Prefetch Query if detected
            if is_prefetch_parent:
                next_q = view_queries[i+1]
                next_sql = next_q['sql']
                next_query_global_idx = current_query_global_idx + 1
                
                p_instance_ids = tracker.query_to_instances.get(next_query_global_idx, set())
                p_consumed = set()
                for inst_id in p_instance_ids:
                    if inst_id in tracker.used_fields:
                        p_consumed.update(tracker.used_fields[inst_id])
                
                p_fetched = extract_fields_from_sql(next_sql)
                p_actual_consumed = [f for f in p_consumed if f in p_fetched or any(f in col for col in p_fetched)]

                print(f"   {CYAN}SQL 2 (Prefetch): {next_sql[:100]}...{RESET}")
                print(f"   {YELLOW}Prefetch Fields fetched  = {p_fetched}{RESET}")
                print(f"   {GREEN}Prefetch Fields consumed = {p_actual_consumed}{RESET}")
                
                skip_next = True # Don't process this query again in the next loop iteration

            print(f"   {PINK}Suggested QuerySet:- [Pending Logic]{RESET}")
            logical_qs_count += 1

        # 3. Summary of inefficiencies
        duplicates = {sql: count for sql, count in sql_counts.items() if count > 1}
        if duplicates:
            if len(duplicates) >= 1:
                 print(f"\n{ORANGE}⚠ CRITICAL: Duplicate Queries Detected{RESET}")

        return response

    return wrapper