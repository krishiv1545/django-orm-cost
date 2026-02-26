import time
from django.db import connection


class ORMQueryTracker:
    """
    Context manager that tracks:
    - Total query count
    - Total DB time
    - Duplicate queries
    """

    def __init__(self):
        self.queries = []
        self.start_time = None
        self.end_time = None

    def _wrapper(self, execute, sql, params, many, context):
        start = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            duration = time.perf_counter() - start
            self.queries.append({
                "sql": sql,
                "time": duration,
            })

    def __enter__(self):
        self.start_time = time.perf_counter()
        self._cm = connection.execute_wrapper(self._wrapper)
        self._cm.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._cm.__exit__(exc_type, exc_value, traceback)
        self.end_time = time.perf_counter()

    # --------- Reporting ---------

    @property
    def total_queries(self):
        return len(self.queries)

    @property
    def total_db_time(self):
        return sum(q["time"] for q in self.queries)

    @property
    def total_execution_time(self):
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return 0

    def duplicate_summary(self):
        from collections import Counter

        normalized = [self._normalize_sql(q["sql"]) for q in self.queries]
        counts = Counter(normalized)

        return {sql: count for sql, count in counts.items() if count > 1}

    def _normalize_sql(self, sql):
        return " ".join(sql.split())