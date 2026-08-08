# Generated manually — backfill de datos, no schema.
"""
Marca coa_configurado=True en TODAS las Appointment que ya existían antes
de esta fase (sesión 42) — sin este backfill, AppointmentService.
preparar_detalle_compras (recién conectado a ajax_get_lineas_oc) podría
re-sugerir/sobrescribir el pre-marcado de Materia Prima en citas antiguas
que Compras ya configuró manualmente en el pasado, antes de que existiera
este campo. Solo las Appointment creadas DESPUÉS de este backfill arrancan
en False (default del campo) y se benefician del pre-marcado automático.
"""
from django.db import migrations


def marcar_existentes_como_configuradas(apps, schema_editor):
    Appointment = apps.get_model('appointments', 'Appointment')
    Appointment.objects.update(coa_configurado=True)


def revertir(apps, schema_editor):
    # No hay forma de distinguir "ya estaba en True antes de este backfill"
    # de "se puso en True por este backfill" — no se intenta revertir el
    # valor, es una migración de datos de un solo sentido.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('appointments', '0002_appointment_coa_configurado'),
    ]

    operations = [
        migrations.RunPython(marcar_existentes_como_configuradas, revertir),
    ]
