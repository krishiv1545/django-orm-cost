from django.apps import AppConfig


class DjangoEndpointProfilerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "django_orm_cost"
    verbose_name = "Django ORM Cost"