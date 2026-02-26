from django.apps import AppConfig
import os


class DjangoOrmCostConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "django_orm_cost"
    verbose_name = "Django ORM Cost"

    path = os.path.dirname(os.path.abspath(__file__))