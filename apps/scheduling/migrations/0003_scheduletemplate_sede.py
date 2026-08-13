# Sesión 48b — agrega ScheduleTemplate.sede (FK a base.Sede).
#
# Escrita a mano en vez de generada por `makemigrations` (que pide un
# default interactivo para un AddField no-nulo): la BD real tiene 0 filas
# de ScheduleTemplate en este momento (confirmado antes de escribir esta
# migración), así que un AddField directo sin default es seguro — no hay
# ninguna fila existente que backfillear, a diferencia de Ticket.muelle
# (sesión 27) o Appointment.sede (sesión 48), que sí requirieron el patrón
# de 3 pasos (nullable → RunPython de backfill → not-null) por tener datos
# reales que preservar.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0002_remove_weeklyslotrule_unique_regla_por_plantilla_dia_hora_and_more'),
        ('base', '0002_seed_sedes'),
    ]

    operations = [
        migrations.AddField(
            model_name='scheduletemplate',
            name='sede',
            field=models.ForeignKey(
                help_text=(
                    'Sede a la que pertenece esta plantilla. Cada sede configura su '
                    'propia disponibilidad semanal de forma independiente — '
                    'generar_semana() crea AppointmentSlots solo para esta sede.'
                ),
                on_delete=django.db.models.deletion.PROTECT,
                related_name='templates',
                to='base.sede',
            ),
        ),
    ]
