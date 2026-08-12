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


class Sede(TimeStampedModel):
    """
    Planta/almacén físico de Daryza. Reemplaza al CharField con choices
    que tenía Appointment.lugar_entrega — permite que Appointment y
    AppointmentSlot referencien la misma sede real por FK, en vez de un
    choice de texto sin relación con ningún otro dato del sistema.

    Vive en apps.base (no en apps.appointments) porque se referencia desde
    más de una app de negocio (appointments hoy; operations/scheduling
    potencialmente más adelante), mismo criterio ya usado para
    apps/base/filters.py y apps/base/reporting.py.

    sap_whs_code queda nullable a propósito: la sincronización de OCs desde
    SAP (apps.sap_sync) no trae ningún dato de almacén/WhsCode hoy — el
    campo se deja listo para cuando exista esa sincronización real, sin
    bloquear su uso mientras tanto.
    """
    codigo = models.CharField(
        max_length=20, unique=True,
        help_text="Código corto interno, ej. 'LURIN'."
    )
    nombre = models.CharField(
        max_length=100,
        help_text="Nombre visible para proveedores y staff, ej. 'Planta Lurín'."
    )
    sap_whs_code = models.CharField(
        max_length=20, null=True, blank=True,
        help_text="Código de almacén (WhsCode) en SAP. Sin sincronización real todavía."
    )
    activa = models.BooleanField(
        default=True,
        help_text="Solo las sedes activas se ofrecen al agendar."
    )

    def __str__(self):
        return self.nombre

    class Meta:
        verbose_name = "Sede"
        verbose_name_plural = "Sedes"
        ordering = ['nombre']