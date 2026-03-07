# Django ORM Cost Tracker

A diagnostic decorator for Django views that provides deep visibility into QuerySet efficiency, field consumption, and SQL origins.

## Purpose
To identify and eliminate **Over-fetching** (fetching columns you don't use) and **N+1 problems** by providing an automated audit of every database interaction during a request.

## The Decorator: `@track_orm_cost`
The tool is designed to be wrapped around any Django view (function-based or method-based). Once applied, it automatically instruments the request cycle to profile DB performance.

## The Middleware: `ORMCostMiddleware`
Automatically applies `track_orm_cost` to every view in your project without touching view code. Supports include/exclude glob patterns for scoped profiling.

---

## Installation

### 1. Clone this repository
```bash
git clone https://github.com/krishiv1545/django-orm-cost
```

### 2. Install within your project env
```bash
pip install -e /path/to/django-orm-cost
```

### 3 (a). Import and use @track_orm_cost
```python
from django_orm_cost import track_orm_cost

@login_required
@track_orm_cost
def my_view(request):
    # Your logic here
    return render(request, 'template.html') # or RESTful response
```

### 3 (b). Use ORMCostMiddleware

Add to `settings.py`:

```python
MIDDLEWARE = [
    # ... your existing middleware ...
    "django_orm_cost.middleware.ORMCostMiddleware",
]

# Default: False
ORM_COST_DEBUG_ONLY = True

# Optional whitelisting to include modules and/or files
# Default: All
ORM_COST_INCLUDE = [
    "*/invoices/*",
    "*/invoices/views.py",
]

# Optional blacklisting to exclude modules and/or files
# Default: None
ORM_COST_EXCLUDE = [
    "django.contrib.admin.*",
    "*/invoices/*",
    "*/invoices/views.py",
]
```

---

## Core Concepts

### 1. Django Connections & Logs
Django maintains a `connection.queries` log when `DEBUG` is enabled. This tool hooks into the database cursor using an execution wrapper to capture every raw SQL statement and its duration.
```
{
    "sql" :
    "time" :
}
```

### 2. Lazy QuerySets & Call Site Resolution
QuerySets are lazy; they are often defined in one place but evaluated elsewhere. The tracker inspects the Python call stack at the moment of execution, skipping Django internals and `site-packages` frames, to find the exact line in your project code that triggered the database hit.
* **SQL Executed At:** This identifies the exact line in your project where the database was actually hit.
* **The Mechanism:** We patch `QuerySet._fetch_all` to capture the call stack at the moment of evaluation, filtering out Django internals to find your project's business logic.

### 3. Field Usage Tracking
The tracker monkey-patches `__getattribute__` on every registered Django model for the duration of the request. Every field access on a model instance is intercepted and recorded against that instance's memory address. This produces a map of which fields your code actually touched after the query returned.

Three categories of field access are tracked:

* **Direct fields** — e.g. `trail.action`: caught by checking the field name against `meta.fields`.
* **FK attnames** — e.g. `trail.reviewer_id`: caught via an `attname` map built from `meta.fields`, since `meta.get_field("reviewer_id")` raises `FieldDoesNotExist` in Django.
* **Relation traversals** — e.g. `trail.reviewer.first_name`: when a relation field is accessed, the FK attname (e.g. `reviewer_id`) is recorded as consumed on the parent instance, and a path prefix (e.g. `reviewer__`) is stamped onto the related object so subsequent field accesses on it are recorded with their full path (e.g. `reviewer__first_name`).

### 4. Iterator Patching & N+1 Grouping
`QuerySet.__iter__` is patched to detect when a QuerySet is being iterated in a `for` loop. At that moment, the file and line number of the loop are captured and set as the **active group key**. Any QuerySet that fires *during* that iteration from a *different* call site is classified as a child query and grouped under the parent loop — this is how N+1 queries are detected and attributed to their origin loop rather than reported as independent QuerySets.

### 5. N+1 Classification
- **Loop query** — The same database query is being executed multiple times inside a loop. This usually happens when querying the database for each item individually.
- **Lazy FK** — Related objects are being fetched one-by-one from the database. This happens when Django lazily loads ForeignKey relationships.

---

## Log Analysis Guide

Output reference
```
═══ Analysis: get_review_trail ═══
Time: 74.05ms | Total Queries: 6

1. QuerySet Analysis
   Location: D:\Projects\finnovate_project\fintech_project\core_APP\modules\gl_reviews\gl_reviews.py:1015
   SQL 1: SELECT "review_trails"."id", "review_trails"."reviewer_id", "review_trails"."reviewer_responsibility_matrix_id", "review_trails"."gl_review_id", "review_trails"."previous_trail_id", "review_trails"."gl_code", "review_trails"."gl_name", "review_trails"."reconciliation_notes", "review_trails"."action", "review_trails"."created_at" FROM "review_trails" WHERE "review_trails"."gl_code" = '11290300' ORDER BY "review_trails"."created_at" ASC
   SQL 2: [3x] SELECT "Users"."id", "Users"."password", "Users"."last_login", "Users"."is_superuser", "Users"."username", "Users"."first_name", "Users"."last_name", "Users"."email", "Users"."is_staff", "Users"."is_active", "Users"."date_joined", "Users"."user_type" FROM "Users" WHERE "Users"."id" = 27 LIMIT 21
   Fields Fetched  = ['gl_code', 'gl_name', 'reconciliation_notes', 'action', 'created_at', '[3x] password', '[3x] last_login', '[3x] is_superuser', '[3x] username', '[3x] first_name', '[3x] last_name', '[3x] email', '[3x] is_staff', '[3x] is_active', '[3x] date_joined', '[3x] user_type']
   Fields Consumed = ['action', 'created_at']
   Efficiency = 2/16 | 87.50% over-fetched
   >>> N+1 Query Detected
   The same database query is being executed multiple times inside a loop.
   This usually happens when querying the database for each item individually.
   >>> Suggestion: ReviewTrail.objects.only('action', 'created_at')

2. QuerySet Analysis
   Location: D:\Projects\finnovate_project\fintech_project\core_APP\modules\gl_reviews\gl_reviews.py:1027
   SQL 1: SELECT "review_trails"."id", "review_trails"."reviewer_id", "Users"."id", "Users"."first_name" FROM "review_trails" LEFT OUTER JOIN "Users" ON ("review_trails"."reviewer_id" = "Users"."id") WHERE "review_trails"."gl_code" = '11290300' ORDER BY "review_trails"."created_at" ASC
   Fields Fetched  = ['first_name']
   Fields Consumed = ['reviewer__first_name']
   Efficiency = 1/1 | 100.00% over-fetched
   >>> Suggestion: ReviewTrail.objects.select_related('reviewer').only('reviewer__first_name')
```

| Field | Description |
|---|---|
| **Location** | File and line in your project code where the QuerySet was evaluated |
| **SQL N** | The raw SQL that fired, deduplicated and annotated with a repeat count (e.g. `[12x]`) |
| **Fields Fetched** | All columns returned by the `SELECT` statement |
| **Fields Consumed** | Fields your code actually accessed on the returned instances |
| **Efficiency** | `consumed / fetched` — the fraction of fetched data that was used |
| **% over-fetched** | Percentage of fetched fields that went unused |
| **N+1 warning** | Fires only when fields are consumed and the same query pattern repeats |
| **Suggestion** | A concrete `.only()` / `select_related()` rewrite based on observed usage |