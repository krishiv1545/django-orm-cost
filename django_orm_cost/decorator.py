import time
import re
import inspect
from functools import wraps
from django.test.utils import override_settings
from django.db import connection
# connection is an instance of BaseDatabaseWrapper
# if DEBUG=True, db cursor is wrapped by CursorDebugWrapper
# everytime a SQL is executed, it adds a record to connection.queries
# connection.queries is a list of dicts {"sql" :, "time" : }
from django.db.models.query import QuerySet
# Django QuerySet represents a SQL AST (Abstract Syntax Tree)
# _fetch_all() is the method that executes the SQL and populates the QuerySet's _result_cache
# _fetch_all():-
# 1. Compiles Django QuerySet into raw SQL
# 2. Executes the SQL using the database cursor
# 3. Receives raw rows from the database
# 4. Converts raw rows into Django model instances and stores them in _result_cache*
# *instance is created for each DB row record
# post_init.send(sender=Model, instance=instance) is emitted after instance creation
from django.db.models.signals import post_init
from django.apps import apps
from collections import OrderedDict # OrderedDict is a subclass of dict that remembers the order of insertion
from collections import Counter # Counter is a subclass of dict that counts the occurrences of each key


BOLD = "\033[1m"
BLUE = "\033[38;5;39m"         # Dark Blue
SKY = "\033[38;5;117m"         # Sky Blue
LIME = "\033[38;5;118m"        # Lime
GOLD = "\033[38;5;220m"        # Gold
WHITE = "\033[38;5;255m"       # White
GRAY = "\033[38;5;244m"        # Gray
CRIMSON = "\033[38;5;196m"     # Red
RESET = "\033[0m"


# FieldUsageTracker tracks instances created by SQL execution and which fields are accessed on those instances
# It tracks 3 graphs:-
# 1. Query Index -> Instance IDs* // from post_init signal, 
#                                    we know which instance was created by which query (using len(connection.queries) as index)
# 2. Instance ID* -> Accessed field Paths (e.g. "user__email") // from patched __getattribute__, 
# 3. Instance ID* -> Relation Path-Prefix (e.g. "user__") // from patched __getattribute__, 
#                                                           when we access a related field, we track the lineage of that relationship
# *IDs are Python memory addresses/IDs (e.g, id(instance))
class FieldUsageTracker:
    def __init__(self):
        self.used_fields = {}        # instance_id -> set of paths
        self.instance_to_path = {}   # instance_id -> path prefix
        self.query_to_instances = {} # query index -> set of instance_ids
        self._patched_models = {}    # model -> original __getattribute__

    # Every Python model/object has a __getattribute__ method that is called whenever we access any attribute on that object
    # In context to Django, models are CustomUser, ContentType, etc.
    # This function patches the __getattribute__ method of every model
    # (original) user.username -> user.__getattribute__("username")
    # (patched) user.username -> patched_getattribute(user, "username")
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
            except Exception: 
                pass
            # call original getattribute to ensure normal behavior
            # user.username -> patched_getattribute(user, "username") // track data usage -> original_getattribute(user, "username")
            return original_getattribute(instance, name)

        model.__getattribute__ = patched_getattribute # overiding getattribute of model with patched version
        self._patched_models[model] = original_getattribute

    def record_creation(self, sender, instance, **kwargs):
        query_idx = len(connection.queries) - 1
        self.query_to_instances.setdefault(query_idx, set()).add(id(instance))

    # Undo monkey-patching to restore original __getattribute__ methods of all models that were patched
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
    # clean_columns = [col.split('.')[-1].replace('"', '').strip() for col in columns]
    clean_columns = [col.replace('"', '').strip() for col in columns]
    return clean_columns


def filter_id(fields):
    return [
        f for f in fields
        if f != "id"
    ]


def c_filter_id(fields):
    return [
        f for f in fields
        if f != "id" and not f.endswith("_id")
    ]


def f_filter_id(fields):
    f_clean = [f.split('.')[-1] for f in fields]
    return [
        f for f in f_clean
        if f != "id" and not f.endswith("_id")
    ]


def suggest(fetched, consumed, tracker, all_instance_ids, qs_id, model_name="Model", is_n1=False):
    if not consumed:
        return f"{model_name}.objects.none()"

    # 1. Categorize fields
    local_fields = sorted([f for f in consumed if "__" not in f])
    related_paths = sorted([f for f in consumed if "__" in f])
    
    # Identify the top-level relations (e.g., 'reviewer' from 'reviewer__first_name')
    relations = sorted(list({p.split('__')[0] for p in related_paths}))

    # 2. Build ORM components
    # If N+1 is detected, we MUST suggest select_related to fix the loop
    # If no N+1 but we have related paths, it's likely already select_related or needs to be
    
    method_chain = [f"{model_name}.objects"]
    
    if relations:
        # For simplicity in this tracker, we treat related field access 
        # as a candidate for select_related to flatten the query
        rel_args = ", ".join(repr(r) for r in relations)
        method_chain.append(f"select_related({rel_args})")

    # .only() should contain both local fields and the specific related paths
    only_args = [repr(f) for f in local_fields + related_paths]
    method_chain.append(f"only({', '.join(only_args)})")

    sugg = ".".join(method_chain)
    return sugg


# Decorator to wrap Django view
def track_orm_cost(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        tracker = FieldUsageTracker()

        active_queryset = None
        queryset_groups = {}
        queryset_order = []

        def patched_iter(self):
            nonlocal active_queryset

            # Resolve what group_key this qs maps to
            # We need the call-site, so re-derive it here
            stack = inspect.stack()
            file, line = "Unknown", 0
            for frame in stack:
                if "django" in frame.filename or "site-packages" in frame.filename:
                    continue
                file, line = frame.filename, frame.lineno
                break

            group_key = (file, line)
            active_queryset = group_key

            try:
                for obj in original_iter(self):
                    yield obj
            finally:
                active_queryset = None

        def patched_fetch_all(self):
            qs_id = id(self)
            model_name = self.model.__name__

            stack = inspect.stack()
            file, line = "Unknown", 0
            for frame in stack:
                if "django" in frame.filename or "site-packages" in frame.filename:
                    continue
                # print(f"Frame: {frame}")
                # print(f"Inspecting frame: {frame.filename}:{frame.lineno} in {frame.function}")
                # Frame:
                #   filename: D:\Projects\finnovate_project\fintech_project\core_APP\modules\gl_reviews\gl_reviews.py
                #   function: get_review_trail
                #   lineno: 1008
                #   code context: for t in trails4:
                #   column offset: 13
                #   end column offset: 20
                #
                # Inspecting frame:
                #   File: D:\Projects\finnovate_project\fintech_project\core_APP\modules\gl_reviews\gl_reviews.py
                #   Function: get_review_trail
                #   Line Number: 1008
                file, line = frame.filename, frame.lineno
                break

            q_start = len(connection.queries)
            result = original_fetch_all(self)
            q_end = len(connection.queries)

            # ── NEW: if we're inside an active iteration, attach to its group ──
            if active_queryset is not None and active_queryset in queryset_groups:
                parent_group = queryset_groups[active_queryset]
                for idx in range(q_start, q_end):
                    parent_group["queries"].append(idx)
                    # Tag these as "child" queries so we can flag N+1 later
                    parent_group.setdefault("child_queries", set()).add(idx)
                return result
            # ───────────────────────────────────────────────────────────────────

            group_key = (file, line)

            if group_key not in queryset_groups:
                queryset_groups[group_key] = {
                    "origin": (file, line),
                    "queries": [],
                    "model": model_name,
                    "child_queries": set()
                }
                queryset_order.append(group_key)

            for idx in range(q_start, q_end):
                queryset_groups[group_key]["queries"].append(idx)

            return result
        
        original_fetch_all = QuerySet._fetch_all
        original_iter = QuerySet.__iter__

        QuerySet._fetch_all = patched_fetch_all
        QuerySet.__iter__ = patched_iter

        QuerySet._fetch_all = patched_fetch_all
        # print(f"apps.get_models(): {apps.get_models()}")
        for model in apps.get_models():
            # apps.get_models() returns all models in the Django project, we monkey-patch them to track field access
            # [<class 'django.contrib.auth.models.CustomUser'>, <class 'django.contrib.contenttypes.models.ContentType'>, ...]
            tracker.patch_model(model)
        post_init.connect(tracker.record_creation)

        with override_settings(DEBUG=True):
            start = time.perf_counter()
            start_queries = len(connection.queries)
            response = view_func(request, *args, **kwargs)
            # print(f"qs_origins: {qs_origins}")
            # {
            # "2": ("D:\\Projects\\finnovate_project\\fintech_project\\core_APP\\modules\\gl_reviews\\gl_reviews.py", 993),
            # "4": ("D:\\Projects\\finnovate_project\\fintech_project\\core_APP\\modules\\gl_reviews\\gl_reviews.py", 1008),
            # "5": ("D:\\Projects\\finnovate_project\\fintech_project\\core_APP\\modules\\gl_reviews\\gl_reviews.py", 1008),
            # "3": ("D:\\Projects\\finnovate_project\\fintech_project\\core_APP\\modules\\gl_reviews\\gl_reviews.py", 1008)
            # }
            end_queries = len(connection.queries)
            total_time = (time.perf_counter() - start) * 1000

        post_init.disconnect(tracker.record_creation)
        tracker.unpatch_all()
        QuerySet._fetch_all = original_fetch_all

        view_queries = connection.queries[start_queries:end_queries]
        
        print(f"\n{BOLD}{BLUE}═══ Analysis: {view_func.__name__} ═══{RESET}")
        print(f"{BLUE}Time: {total_time:.2f}ms | Total Queries: {len(view_queries)}{RESET}")

        logical_count = 1
        for qs_id in queryset_order:

            group = queryset_groups[qs_id]
            model_name = group["model"]

            origin = group["origin"]
            q_indices = [
                idx for idx in group["queries"]
                if start_queries <= idx < end_queries
            ]

            if not q_indices:
                continue

            all_instance_ids = set()
            all_fetched_raw = []
            sql_previews = []

            for q_idx in q_indices:
                local_idx = q_idx - start_queries
                q = view_queries[local_idx]

                sql_previews.append(q["sql"][:120])

                inst_ids = tracker.query_to_instances.get(q_idx, set())
                all_instance_ids.update(inst_ids)

                all_fetched_raw += extract_fields_from_sql(q["sql"])

            consumed_paths = set()
            for obj_id in all_instance_ids:
                if obj_id in tracker.used_fields:
                    consumed_paths.update(tracker.used_fields[obj_id])

            f_clean = f_filter_id(all_fetched_raw)
            field_counts = Counter(f_clean)
            pretty_fields = []
            for field, count in field_counts.items():
                if count > 1:
                    pretty_fields.append(f"[{count}x] {field.split('.')[-1]}")
                else:
                    pretty_fields.append(field.split('.')[-1])
            c_clean = filter_id(consumed_paths)

            print(f"\n{BOLD}{SKY}{logical_count}. QuerySet Analysis{RESET}")
            print(f"   {GRAY}Location: {origin[0]}:{origin[1]}{RESET}")

            sql_counter = OrderedDict()

            for q_idx in q_indices:
                raw_sql = view_queries[q_idx - start_queries]["sql"]

                # Normalize numeric values to detect repetition
                fingerprint = re.sub(r"\b\d+\b", "?", raw_sql)

                if fingerprint not in sql_counter:
                    sql_counter[fingerprint] = {
                        "count": 0,
                        "sql_ast": raw_sql
                    }

                sql_counter[fingerprint]["count"] += 1

            # Print aggregated SQLs
            for i, (fp, data) in enumerate(sql_counter.items(), 1):
                count = data["count"]
                sql_ast = data["sql_ast"]

                prefix = f"[{count}x] " if count > 1 else ""
                print(f"   {SKY}SQL {i}: {prefix}{sql_ast}{RESET}")

            print(f"   {GOLD}Fields Fetched  = {pretty_fields}{RESET}")
            print(f"   {LIME}Fields Consumed = {c_filter_id(c_clean)}{RESET}")
            print(f"   {LIME}Efficiency = {len(c_filter_id(c_clean))}/{len(pretty_fields)} | {100 - (len(c_filter_id(c_clean)) / len(pretty_fields) * 100):.2f}% over-fetched{RESET}")

            is_n1 = False
            if len(q_indices) > 1:
                fingerprints = [
                    re.sub(r"\b\d+\b", "?", view_queries[q_idx - start_queries]["sql"])
                    for q_idx in q_indices
                ]
                if len(set(fingerprints)) < len(fingerprints):
                    is_n1 = True
                    print(f"   {CRIMSON}>>> N+1 detected inside this QuerySet{RESET}")

            if (100 - (len(c_clean) / len(pretty_fields) * 100)) > 0:
                sugg = suggest(f_clean, c_clean, tracker, all_instance_ids, qs_id, model_name, is_n1)
                if sugg:
                    print(f"   {WHITE}>>> Suggestion: {sugg}{RESET}")

            logical_count += 1
        print("")
        return response
    
    # Wrapper function executes Django view, collects response, and we monkey-patch the execution
    # response is returned at the end
    return wrapper