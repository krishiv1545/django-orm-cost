import fnmatch
import inspect
from .decorator import track_orm_cost
from django.conf import settings


class ORMCostMiddleware:
    """
    Middleware that automatically applies track_orm_cost to every view.

    settings.py:-

    # Whitelisting
    ORM_COST_INCLUDE = [
        "*/invoices/*",         # whitelist modules
        "*/invoices/views.py",  # whitelist files
    ]

    # Blacklisting
    ORM_COST_EXCLUDE = [
        "*/invoices/*",         # blacklist modules
        "*/invoices/views.py",  # blacklist files
    ]

    # This shit will run only if ORM_COST_DEBUG_ONLY is True
    ORM_COST_DEBUG_ONLY = True  # default: False
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self._cache = {}  # view_func -> wrapped or original


    def __call__(self, request):
        return self.get_response(request)


    def process_view(self, request, view_func, view_args, view_kwargs):

        if not getattr(settings, "ORM_COST_DEBUG_ONLY", False): # prevent execution in production
            return None

        # Check cache to avoid re-wrapping the same view repeatedly
        if view_func in self._cache:
            cached = self._cache[view_func]
            if cached is view_func:
                return None  # was excluded
            return cached(request, *view_args, **view_kwargs)

        view_module = getattr(view_func, "__module__", "") or ""
        view_name = f"{view_module}.{getattr(view_func, '__name__', '')}"
        # e.g, "core_APP.modules.gl_reviews.gl_reviews.get_review_trail"

        # Resolve the file path of the view for glob matching
        try:
            view_file = inspect.getfile(view_func)
        except (TypeError, OSError):
            view_file = ""

        # Blacklist check
        excludes = getattr(settings, "ORM_COST_EXCLUDE", [])
        for pattern in excludes:
            if fnmatch.fnmatch(view_name, pattern) or fnmatch.fnmatch(view_file, pattern):
                self._cache[view_func] = view_func  # mark as excluded
                return None

        # Whitelist check
        includes = getattr(settings, "ORM_COST_INCLUDE", [])
        if includes:
            matched = any(
                fnmatch.fnmatch(view_name, p) or fnmatch.fnmatch(view_file, p)
                for p in includes
            )
            if not matched:
                self._cache[view_func] = view_func  # not in whitelist, skip
                return None

        # Wrap and cache
        wrapped = track_orm_cost(view_func)
        self._cache[view_func] = wrapped
        return wrapped(request, *view_args, **view_kwargs)