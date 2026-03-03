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
    clean_columns = [col.split('.')[-1].replace('"', '').strip() for col in columns]
    return clean_columns


def filter_id(fields):
    return [
        f for f in fields
        if f != "id" and not f.endswith("_id")
    ]


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

            qs_id = id(self)

            # Mark this queryset as active
            active_queryset = qs_id

            try:
                for obj in original_iter(self):
                    yield obj
            finally:
                active_queryset = None


        def patched_fetch_all(self):
            qs_id = id(self)

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

            # Determine logical owner
            owner_id = active_queryset if active_queryset else qs_id

            if owner_id not in queryset_groups:
                queryset_groups[owner_id] = {
                    "origin": (file, line),
                    "queries": []
                }
                queryset_order.append(owner_id)

            for idx in range(q_start, q_end):
                queryset_groups[owner_id]["queries"].append(idx)

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

            f_clean = filter_id(all_fetched_raw)
            c_clean = filter_id(consumed_paths)

            print(f"\n{BOLD}{SKY}{logical_count}. QuerySet Analysis{RESET}")
            print(f"   {GRAY}Location: {origin[0]}:{origin[1]}{RESET}")

            for i, sql in enumerate(sql_previews, 1):
                print(f"   {SKY}SQL {i}: {sql}...{RESET}")

            print(f"   {GOLD}Fields Fetched  = {f_clean}{RESET}")
            print(f"   {LIME}Fields Consumed = {sorted(list(c_clean))}{RESET}")

            if len(q_indices) > 1:
                fingerprints = [
                    re.sub(r"\b\d+\b", "?", view_queries[q_idx - start_queries]["sql"])
                    for q_idx in q_indices
                ]
                if len(set(fingerprints)) < len(fingerprints):
                    print(f"   {CRIMSON}⚠ N+1 detected inside this QuerySet{RESET}")

            if c_clean:
                sugg = f".only({', '.join(repr(f) for f in sorted(c_clean))})"
                print(f"   {WHITE}💡 Suggestion: {sugg}{RESET}")

            logical_count += 1
        print("")
        return response
    
    # Wrapper function executes Django view, collects response, and we monkey-patch the execution
    # response is returned at the end
    return wrapper