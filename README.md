# Django ORM Cost Tracker

A diagnostic decorator for Django views that provides deep visibility into QuerySet efficiency, field consumption, and SQL origins.

## Purpose
To identify and eliminate **Over-fetching** (fetching columns you don't use) and **N+1 problems** by providing an automated audit of every database interaction during a request.

## The Decorator: `@track_orm_cost`
The tool is designed to be wrapped around any Django view (function-based or method-based). Once applied, it automatically instruments the request cycle to profile DB performance.

### Usage
```python
from django_orm_cost.decorator import track_orm_cost

@login_required
@track_orm_cost
def my_view(request):
    # Your logic here
    return render(request, 'template.html') # or RESTful response
```


## Core Concepts

### 1. Django Connections & Logs
Django maintains a `connection.queries` log when `DEBUG` is enabled. This tool hooks into the database cursor using an execution wrapper to capture every raw SQL statement and its duration.
```
{
    "sql" :
    "time" :
}
```

### 2. Lazy QuerySets & Execution Origins
QuerySets are **lazy**; they are often defined in one place (e.g., a manager or variable assignment) but executed elsewhere (e.g., when a loop starts or a list cast occurs). 
* **SQL Executed At:** This identifies the exact line in your project where the database was actually hit. 
* **The Mechanism:** We patch `QuerySet._fetch_all` to capture the call stack at the moment of evaluation, filtering out Django internals to find your project's business logic.

### 3. Field Usage Tracking
By patching the `__getattribute__` method of models during the request cycle, the tracker monitors which fields are accessed by your code after the objects are instantiated.
* **Fetched:** Columns requested in the `SELECT` statement.
* **Consumed:** Fields actually accessed by your Python logic.

---

## Log Analysis Guide

**QuerySet Analysis** : A logical grouping of queries related to a single data request (includes Prefetches).
**SQL Executed At** : The file and line number in your code that triggered the database hit.
 **SQL 1** : The primary query triggered by the QuerySet.
 **SQL 2 (Prefetch)** : Related data fetched via `.prefetch_related()`, grouped under its parent for clarity.
 **Fields Fetched** : The exhaustive list of columns returned by the DB.
 **Fields Consumed** : The specific fields your code actually used.