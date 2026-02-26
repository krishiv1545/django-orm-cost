import time
from functools import wraps
from collections import Counter
from django.test.utils import override_settings
from django.db import connection
# connection.queries is a list of dicts {"sql" :, "time" : } executed during the request cycle
# it is like a logbook that we can inspect to see what SQL was run and how long it took


PURPLE = "\033[95m"
PINK = "\033[94m"
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
ORANGE = "\033[38;5;208m"
RED = "\033[91m"
RESET = "\033[0m"


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

        with override_settings(DEBUG=True):
            # *args = URL params, **kwargs = query params
            start = time.perf_counter()
            start_queries = len(connection.queries)
            # perf_counter = high resolution timer, good for measuring short durations

            response = view_func(request, *args, **kwargs) # Call the original view

            end = time.perf_counter()
            end_queries = len(connection.queries)

        total_time = (end - start) * 1000  # ms
        view_queries = connection.queries[start_queries:end_queries]

        print(f"\n{PURPLE}--- Analysis for {view_func.__name__} ---{RESET}")
        print(f"{PURPLE}Time: {total_time:.2f}ms | Queries: {len(view_queries)}{RESET}")
        
        # 1. Track Exact Duplicates
        sql_statements = [q['sql'] for q in view_queries]
        sql_counts = Counter(sql_statements)
        # Counter creates a dict {sql_statements[i]: count} for each unique SQL statement

        for i, q in enumerate(view_queries, 1):
            sql = q['sql']
            color = PURPLE
            
            # Highlight duplicates in Red
            if sql_counts[sql] > 1:
                color = ORANGE
            
            print(f"{color}{i}. ({q['time']}s) SQL: {sql[:100]}...{RESET}")

        # 2. Summary of inefficiencies
        duplicates = {sql: count for sql, count in sql_counts.items() if count > 1}
        if duplicates:
            if len(duplicates) > 1:
                print(f"\n{ORANGE}⚠ CRITICAL: {len(duplicates)} unique queries were repeated!{RESET}")
                for sql, count in duplicates.items():
                    print(f"{ORANGE}- Repeated {count}x: {sql[:102]}...{RESET}")
            if len(duplicates) == 1:
                sql, count = list(duplicates.items())[0]
                print(f"\n{ORANGE}⚠ CRITICAL: 1 unique query was repeated!{RESET}")
                print(f"{ORANGE}- Repeated {count}x: {sql[:102]}...{RESET}")
        return response

    return wrapper