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
SUNSET = "\033[38;5;202m"      # Orange
RESET = "\033[0m"


# FieldUsageTracker tracks instances created by SQL execution and which fields are accessed on those instances
# It tracks 3 graphs:-
# 1. Query Index -> Instance IDs* // from post_init signal, 
#                                    we know which instance was created by which query (using len(connection.queries) as index)
# 2. Instance ID* -> Accessed field Paths (e.g. "user__email") // from patched __getattribute__, 
# 3. Instance ID* -> Relation Path-Prefix (e.g. "user__") // from patched __getattribute__, 
#                                                           when we access a related field, we track the lineage of that relationship
# 4. Instance ID* -> Model Class Name // from patched __getattribute__
# *IDs are Python memory addresses/IDs (e.g, id(instance))
class FieldUsageTracker:
    def __init__(self):
        self.used_fields = {}        # instance_id -> set of paths
        self.instance_to_path = {}   # instance_id -> path prefix
        self.query_to_instances = {} # query index -> set of instance_ids
        self._patched_models = {}    # model -> original __getattribute__
        self.instance_to_model = {}  # instance_id -> model class name
        # instance_to_model is used to detect Python memory address reuse:
        # when a short-lived instance (e.g. GLReview from .first()) is GC'd,
        # its address may be reused by a later instance (e.g. GLSupportingDocument).
        # By stamping each address with the model name at creation time and
        # overwriting on reuse, we can filter out stale/colliding instance IDs
        # when collecting consumed fields for a given group.

    # Every Python model/object has a __getattribute__ method that is called whenever we access any attribute on that object
    # In context to Django, models are CustomUser, ContentType, etc.
    # This function patches the __getattribute__ method of every model
    # (original) user.username -> user.__getattribute__("username")
    # (patched) user.username -> patched_getattribute(user, "username")


    def patch_model(self, model):
        if model in self._patched_models: 
            return
        original_getattribute = model.__getattribute__
        tracker = self


        def patched_getattribute(instance, name):
            instance_id = id(instance)
            try:
                # meta holds all fields of the model
                meta = object.__getattribute__(instance, "_meta")
                # student.username -> meta.get_field("username")
                # student.<method_name> -> wont be found in meta, goes to exception
                # this is how we filter out non-field attributes and only track database fields

                # attname = attribute name; student.teacher.id is caught
                # but student.teacher_id is considered an attribute name
                attname_to_field = {f.attname: f for f in meta.fields if hasattr(f, 'attname')}
                if name in attname_to_field:
                    prefix = tracker.instance_to_path.get(instance_id, "")
                    tracker.used_fields.setdefault(instance_id, set()).add(f"{prefix}{name}")
                    return original_getattribute(instance, name)
        
                field = meta.get_field(name)

                # triggered when field access triggers a DB lookup for a related object
                if field.is_relation:
                    related_obj = original_getattribute(instance, name)
                    if related_obj:
                        # if triggered when you run 'student.teacher'
                        prefix = tracker.instance_to_path.get(instance_id, "") # prefix is full path to current instance (e.g, student__)
                        tracker.instance_to_path[id(related_obj)] = f"{prefix}{name}__" # id(teacher instance) -> student__teacher__
                    return related_obj

                if name in {f.name for f in meta.fields}:
                    prefix = tracker.instance_to_path.get(instance_id, "") # If instance is 'student', prefix would be student__ in 'instance_to_path'
                    tracker.used_fields.setdefault(instance_id, set()).add(f"{prefix}{name}") # if field 'username' is fetched, add 'student__username' to used_fields
            except Exception: 
                pass
            # call original getattribute to ensure normal behavior
            # user.username -> patched_getattribute(user, "username") -> track data usage -> original_getattribute(user, "username")
            return original_getattribute(instance, name)

        model.__getattribute__ = patched_getattribute # overiding getattribute of model with patched version
        self._patched_models[model] = original_getattribute


    # Linking created instances back to the query that created them, using post_init signal
    def record_creation(self, sender, instance, **kwargs):
        query_idx = len(connection.queries) - 1
        inst_id = id(instance)
        self.query_to_instances.setdefault(query_idx, set()).add(inst_id)
        # Stamp this memory address with the model that currently owns it.
        # If the address is later reused by a different model instance,
        # this entry will be overwritten, allowing stale ID detection.
        self.instance_to_model[inst_id] = sender.__name__

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


def normalize_sql(sql):
    # Normalize standalone integers
    sql = re.sub(r"\b\d+\b", "?", sql)
    # Normalize UUIDs (hex strings with optional hyphens inside quotes)
    sql = re.sub(r"'[0-9a-f]{8}[0-9a-f\-]{0,27}'", "'?'", sql, flags=re.IGNORECASE)
    # Normalize any remaining quoted strings (catches other id formats)
    sql = re.sub(r"'[^']{8,}'", "'?'", sql)
    return sql


def filter_id(fields):
    return [
        f for f in fields
        if f != "id"
    ]


def f_filter_id(fields):
    f_clean = [f.split('.')[-1] for f in fields]
    return [
        f for f in f_clean
        if f != "id"
    ]


def suggest(fetched, consumed, tracker, all_instance_ids, qs_id, model_name="Model", is_n1=False):
    if not consumed:
        return f"{model_name}.objects.none() // Or, it maybe used to filter another QuerySet without consuming fields (e.g. used in .filter(related__in=qs)), in which case no fields are consumed here."

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
        # *args = positional arguments (1, "f1"), **kwargs = keyword arguments (number=1, name="f1")
        # args becomes a tuple (1, "f1"), kwargs becomes a dict {"number": 1, "name": "f1"}
        tracker = FieldUsageTracker()

        active_queryset = None
        queryset_groups = {}
        queryset_order = []

        # patched_iter exists to group N+1 under parent query
        # When QS are executed within a for loop, QuerrySet.__iter__
        # We monkey-patch __iter__ to detect the file and line number of the loop
        def patched_iter(self):
            nonlocal active_queryset # nearest enclosing scope (nearest outside of patched_iter)

            stack = inspect.stack()
            file, line = "Unknown", 0
            for frame in stack:
                if "django" in frame.filename or "site-packages" in frame.filename:
                    continue
                file, line = frame.filename, frame.lineno
                break

            group_key = (file, line)
            active_queryset = group_key
            # This sets a flag that any QuerySet fetches triggered during this iteration will be grouped under this file:line key,
            # allowing us to group parent and child queries together for N+1 detection and analysis. 
            # The flag is cleared after the loop finishes.
            # (e.g, for student in students: -> active_queryset = ("students.py", 1008) -> 
            # student.teacher triggers a fetch with active_queryset = ("students.py", 1008) -> 
            # we know this fetch is a child query of the loop at students.py:1008)
            try:
                for obj in original_iter(self):
                    yield obj
            finally:
                # Clear the flag after iteration completes, to avoid grouping unrelated queries under the same file:line key
                active_queryset = None

        def patched_fetch_all(self):
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

            group_key = (file, line)

            # If inside an active iteration AND this fetch is from a DIFFERENT
            # call site (i.e. it's a child/N+1 query), attach to parent group
            if active_queryset is not None and active_queryset != group_key:
                # This is a child query fired during iteration of active_queryset's loop
                parent_key = active_queryset
                if parent_key not in queryset_groups:
                    # Parent group not yet created (edge case), create it now
                    queryset_groups[parent_key] = {
                        "origin": parent_key,
                        "queries": [],
                        "model": model_name,
                        "child_queries": set()
                    }
                    queryset_order.append(parent_key)

                parent_group = queryset_groups[parent_key]
                for idx in range(q_start, q_end):
                    parent_group["queries"].append(idx)
                    parent_group["child_queries"].add(idx)
                return result

            # this fetch is the parent QuerySet itself
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
            # print(f"group: {group}")
            # group: {
            #   'origin': ('D:\\Projects\\finnovate_project\\fintech_project\\core_APP\\modules\\gl_reviews\\gl_reviews.py', 1015), 
            #   'queries': [3, 4, 5, 6], 
            #   'model': 'ReviewTrail', 
            #   'child_queries': {4, 5, 6}
            # }

            model_name = group["model"]
            origin = group["origin"]

            q_indices = [
                idx for idx in group["queries"]
                if start_queries <= idx < end_queries # filter out queries that were executed outside of this view 
                # Technically, queries cannot have indices outside of the view's range, but whatever~
            ]

            if not q_indices:
                continue

            all_instance_ids = set()
            all_fetched_raw = []
            sql_previews = []

            for q_idx in q_indices:
                local_idx = q_idx - start_queries
                q = view_queries[local_idx]

                sql_previews.append(q["sql"])

                inst_ids = tracker.query_to_instances.get(q_idx, set())
                all_instance_ids.update(inst_ids)

                all_fetched_raw += extract_fields_from_sql(q["sql"])

            # Filter out instance IDs whose memory address has been reused by a
            # different model (Python GC recycles addresses after short-lived .first() calls)
            # We keep an instance if:
            # 1. its recorded model matches this group's model, OR
            # 2. it has a path prefix in instance_to_path, meaning 
            #    it's a related object loaded via select_related (e.g. CustomUser joined into ResponsibilityMatrix)
            #    these legitimately have a different model name
            safe_instance_ids = set()
            for obj_id in all_instance_ids:
                recorded_model = tracker.instance_to_model.get(obj_id)
                if recorded_model == model_name:
                    safe_instance_ids.add(obj_id)
                    continue
                # Keep select_related / prefetch related objects (they have a path prefix)
                if obj_id in tracker.instance_to_path:
                    safe_instance_ids.add(obj_id)

            consumed_paths = set()
            for obj_id in safe_instance_ids:
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

            # For display, pretty_fields stays the same (shows [Nx] multipliers)
            # But efficiency is calculated on unique field name sets
            fetched_name_list = f_clean
            consumed_name_list = filter_id(c_clean)

            over_fetched_count = len(fetched_name_list) - len(consumed_name_list)
            efficiency_pct = (over_fetched_count / len(fetched_name_list) * 100) if fetched_name_list else 0.0

            print(f"\n{BOLD}{SKY}{logical_count}. QuerySet Analysis{RESET}")
            print(f"   {GRAY}Location: {origin[0]}:{origin[1]}{RESET}")

            sql_counter = OrderedDict()

            for q_idx in q_indices:
                raw_sql = view_queries[q_idx - start_queries]["sql"]
                fingerprint = normalize_sql(raw_sql)

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
            print(f"   {LIME}Fields Consumed = {sorted(consumed_name_list)}{RESET}")
            print(f"   {LIME}Efficiency = {len(consumed_name_list)}/{len(fetched_name_list)} | {efficiency_pct:.2f}% over-fetched{RESET}")

            is_n1 = False
            n1_type = None  # 'lazy_fk' or 'loop_query'
            if consumed_name_list and len(q_indices) > 1:
                fingerprints = [
                    normalize_sql(view_queries[q_idx - start_queries]["sql"])
                    for q_idx in q_indices
                ]
                if len(set(fingerprints)) < len(fingerprints):
                    is_n1 = True
                    # Detect type: if repeated SQLs query a DIFFERENT table than the group model,
                    # it's an explicit loop query. If same table, it's a lazy FK lookup.
                    repeated_fp = [fp for fp in set(fingerprints) if fingerprints.count(fp) > 1]
                    first_repeated_sql = next(
                        view_queries[q_idx - start_queries]["sql"]
                        for q_idx in q_indices
                        if normalize_sql(view_queries[q_idx - start_queries]["sql"]) == repeated_fp[0]
                    )
                    # Extract the table name from the repeated SQL's FROM clause
                    table_match = re.search(r'FROM\s+"?(\w+)"?', first_repeated_sql, re.IGNORECASE)
                    repeated_table = table_match.group(1) if table_match else ""
                    # Compare against the group's own model table
                    group_table = model_name.lower().replace(" ", "")  # rough match
                    if repeated_table.lower().replace("_", "") != group_table:
                        n1_type = 'loop_query'
                    else:
                        n1_type = 'lazy_fk'

            if is_n1:
                if n1_type == 'loop_query':
                    print(
                        f"   {CRIMSON}>>> N+1 Query Detected{RESET}\n"
                        f"   {CRIMSON}The same database query is being executed multiple times inside a loop.{RESET}\n"
                        f"   {CRIMSON}This usually happens when querying the database for each item individually.{RESET}"
                    )
                else:
                    print(
                        f"   {CRIMSON}>>> N+1 Query Detected{RESET}\n"
                        f"   {CRIMSON}Related objects are being fetched one-by-one from the database.{RESET}\n"
                        f"   {CRIMSON}This happens when Django lazily loads ForeignKey relationships.{RESET}\n"
                    )

            if efficiency_pct > 0:
                sugg = suggest(list(fetched_name_list), list(consumed_name_list), tracker, safe_instance_ids, qs_id, model_name, is_n1)
                if sugg:
                    print(f"   {WHITE}>>> Suggestion: {sugg}{RESET}")

            logical_count += 1
        print("")
        return response
    
    # Wrapper function executes Django view, collects response, and we monkey-patch the execution
    # response is returned at the end
    return wrapper