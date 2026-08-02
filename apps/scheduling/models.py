# apps/scheduling/models.py
#
# NUEVA APP: scheduling
# Propósito: Gestionar la programación semanal de disponibilidad de citas (Fase 3).
#
# CAPAS AFECTADAS al añadir esta app:
#   - core/settings.py        → Añadir 'apps.scheduling' a INSTALLED_APPS
#   - core/urls.py            → Incluir scheduling.urls bajo /scheduling/
#   - apps/appointments/models.py  → AppointmentSlot se vincula aquí para generación masiva
#   - apps/appointments/services.py → SlotService puede consultar WeeklySlotRule
#
# RELACIÓN CON APPOINTMENTSLOT (apps/appointments/models.py):
#   WeeklySlotRule  ---genera---> AppointmentSlot (vía SchedulingService.generar_semana)
#   Un WeeklySlotRule representa la PLANTILLA (qué días y horas).
#   Un AppointmentSlot representa la INSTANCIA REAL (fecha concreta + estado).

from django.db import models
from django.core.validators import MinValueValidator
from apps.base.models import TimeStampedModel


class ScheduleTemplate(TimeStampedModel):
    """
    Plantilla semanal reutilizable.
    El personal de Almacén crea una plantilla (ej. "Semana tipo estándar"),
    la edita, y desde ella genera los AppointmentSlots de cualquier semana.

    La funcionalidad de 'duplicar semana anterior' opera copiando las
    WeeklySlotRule de una semana ya generada hacia la siguiente.

    Campos heredados de TimeStampedModel: created_at, updated_at
    """
    nombre = models.CharField(
        max_length=100,
        help_text="Nombre descriptivo de la plantilla, ej: 'Semana Estándar Lurin'"
    )
    descripcion = models.TextField(
        blank=True,
        null=True,
        help_text="Notas internas del personal de Almacén."
    )
    activa = models.BooleanField(
        default=True,
        help_text="Solo la plantilla activa se ofrece como opción de base para nuevas semanas."
    )

    def __str__(self):
        return f"{self.nombre} ({'Activa' if self.activa else 'Inactiva'})"

    class Meta:
        verbose_name = "Plantilla de Horario"
        verbose_name_plural = "Plantillas de Horario"
        ordering = ['-activa', 'nombre']


class WeeklySlotRule(models.Model):
    """
    Regla individual dentro de una ScheduleTemplate.
    Define: para un día de la semana, a qué hora, con qué capacidad.

    Ejemplo: Lunes a las 09:00 con capacidad 2.

    NOTA: Esta tabla NO reemplaza a AppointmentSlot. Es la CONFIGURACIÓN.
    AppointmentSlot sigue siendo la fuente de verdad para citas reales.
    SchedulingService.generar_semana() lee estas reglas y crea los Slots concretos.
    """
    DIA_CHOICES = [
        (0, 'Lunes'),
        (1, 'Martes'),
        (2, 'Miércoles'),
        (3, 'Jueves'),
        (4, 'Viernes'),
        (5, 'Sábado'),
    ]

    template = models.ForeignKey(
        ScheduleTemplate,
        on_delete=models.CASCADE,
        related_name='reglas',
        help_text="Plantilla a la que pertenece esta regla."
    )
    dia_semana = models.IntegerField(
        choices=DIA_CHOICES,
        help_text="Día de la semana (0=Lunes, 5=Sábado). Alineado con Python isoweekday()-1."
    )
    hora = models.TimeField(
        help_text="Hora de inicio del slot, ej: 09:00"
    )
    capacidad = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Máximo de citas permitidas en este horario. Por defecto 1."
    )
    activa = models.BooleanField(
        default=True,
        help_text="Si está desactivada, no genera AppointmentSlots al aplicar la plantilla."
    )

    def __str__(self):
        return (
            f"{self.get_dia_semana_display()} "
            f"{self.hora.strftime('%H:%M')} "
            f"(cap: {self.capacidad})"
        )

    class Meta:
        verbose_name = "Regla de Horario Semanal"
        verbose_name_plural = "Reglas de Horario Semanal"
        # Garantiza que en una misma plantilla no existan dos reglas iguales (día+hora)
        unique_together = [('template', 'dia_semana', 'hora')]
        ordering = ['dia_semana', 'hora']
