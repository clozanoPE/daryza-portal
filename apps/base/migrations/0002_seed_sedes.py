# Sesión 48 — siembra las 2 sedes reales (LURIN, PUNTA_NEGRA). DISTRIBUCION
# se descarta a propósito: era un choice vestigial de Appointment.SEDES sin
# ningún dato real asociado (confirmado antes de escribir esta migración).
from django.db import migrations


def seed_sedes(apps, schema_editor):
    Sede = apps.get_model('base', 'Sede')
    Sede.objects.get_or_create(codigo='LURIN', defaults={'nombre': 'Planta Lurín', 'activa': True})
    Sede.objects.get_or_create(codigo='PUNTA_NEGRA', defaults={'nombre': 'Almacén Punta Negra', 'activa': True})


def eliminar_sedes(apps, schema_editor):
    Sede = apps.get_model('base', 'Sede')
    Sede.objects.filter(codigo__in=['LURIN', 'PUNTA_NEGRA']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed_sedes, eliminar_sedes),
    ]
