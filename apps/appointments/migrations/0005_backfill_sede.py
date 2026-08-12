# Sesión 48 — todos los Appointment/AppointmentSlot existentes apuntan a
# LURIN, por decisión explícita del usuario (confirmada durante esta
# sesión: la BD real ya tenía 2 Appointment con lugar_entrega='PUNTA_NEGRA'
# de 5 totales — se decidió forzarlos a LURIN igual, no preservar ese
# valor, en vez de mapear por el valor real de lugar_entrega).
from django.db import migrations


def backfill_sede_lurin(apps, schema_editor):
    Appointment = apps.get_model('appointments', 'Appointment')
    AppointmentSlot = apps.get_model('appointments', 'AppointmentSlot')
    Sede = apps.get_model('base', 'Sede')
    lurin = Sede.objects.get(codigo='LURIN')
    Appointment.objects.filter(sede__isnull=True).update(sede=lurin)
    AppointmentSlot.objects.filter(sede__isnull=True).update(sede=lurin)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('appointments', '0004_appointment_sede_appointmentslot_sede'),
    ]

    operations = [
        migrations.RunPython(backfill_sede_lurin, noop),
    ]
