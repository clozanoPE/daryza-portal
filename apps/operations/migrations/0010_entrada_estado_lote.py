# apps/operations/migrations/0010_entrada_estado_lote.py
"""
Sesión 99 — rediseño del flujo de Entrada de Mercadería (GRPO):

  - EntradaMercaderia.estado (PENDIENTE / ENVIADO / CREADO_SAP): estado de
    negocio, separado de estado_sap (sincronización con SAP, sin tocar).
  - EntradaMercaderia.doc_num_sap: DocNum visible del GRPO en SAP.
  - EntradaMercaderiaLinea: numero_lote / fecha_vencimiento_lote /
    fecha_fabricacion_lote — el LOTE que Almacén/MP completa en el Portal.

RunPython para las filas históricas (5 al momento de esta sesión, todas
con estado_sap='L' — ver sesión 58): el AddField de `estado` les pone
'PENDIENTE' por default; acá se corrige según el estado_sap real, para no
mandarlas de vuelta a "falta el lote" cuando ya fueron enviadas/creadas.
  estado_sap in ('L','B')  -> estado='ENVIADO'
  estado_sap == 'Y'        -> estado='CREADO_SAP'
  estado_sap == ''         -> queda en 'PENDIENTE' (nunca debería haber
                              una fila así hoy, pero es el default seguro).
"""
from django.db import migrations, models


def set_estado_desde_estado_sap(apps, schema_editor):
    EntradaMercaderia = apps.get_model('operations', 'EntradaMercaderia')
    EntradaMercaderia.objects.filter(estado_sap__in=['L', 'B']).update(estado='ENVIADO')
    EntradaMercaderia.objects.filter(estado_sap='Y').update(estado='CREADO_SAP')


def noop(apps, schema_editor):
    # Reversión: el AddField de `estado` se elimina de todos modos al
    # revertir la migración; no hay nada que deshacer acá.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('operations', '0009_backfill_entrada_mercaderia'),
    ]

    operations = [
        migrations.AddField(
            model_name='entradamercaderia',
            name='doc_num_sap',
            field=models.CharField(blank=True, help_text='DocNum visible del GRPO en SAP B1 (número humano), devuelto por el daemon junto con el DocEntry.', max_length=30, null=True),
        ),
        migrations.AddField(
            model_name='entradamercaderia',
            name='estado',
            field=models.CharField(choices=[('PENDIENTE', 'Pendiente (falta completar el lote en el Portal)'), ('ENVIADO', 'Enviado a SAP B1'), ('CREADO_SAP', 'Creado en SAP B1')], default='PENDIENTE', help_text='Estado de negocio: PENDIENTE → ENVIADO → CREADO_SAP (ver docstring del modelo).', max_length=12),
        ),
        migrations.AddField(
            model_name='entradamercaderialinea',
            name='fecha_fabricacion_lote',
            field=models.DateField(blank=True, help_text='Fecha de fabricación del lote — opcional, solo si el artículo la requiere en SAP.', null=True),
        ),
        migrations.AddField(
            model_name='entradamercaderialinea',
            name='fecha_vencimiento_lote',
            field=models.DateField(blank=True, help_text='Fecha de vencimiento del lote — opcional, solo si el artículo la requiere en SAP.', null=True),
        ),
        migrations.AddField(
            model_name='entradamercaderialinea',
            name='numero_lote',
            field=models.CharField(blank=True, default='', help_text='Número de lote del artículo recibido (un solo lote por línea).', max_length=60),
        ),
        migrations.RunPython(set_estado_desde_estado_sap, noop),
    ]
