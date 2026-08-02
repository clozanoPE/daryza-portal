# apps/base/models.py
from django.db import models

class TimeStampedModel(models.Model):
    """
    Clase abstracta para heredar campos de auditoría (creado/actualizado).
    Útil para trazabilidad en auditorías de Daryza.
    """
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True