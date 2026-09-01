# apps/sap_sync/migrations/0005_purchaseorderline_gestionado_por_lote.py
"""
Sesión 99b — PurchaseOrderLine.gestionado_por_lote (OITM.ManBtchNum de SAP).

Sin RunPython: `default=False` es un valor transitorio seguro para las
líneas ya sincronizadas antes de este campo (las 5 OCs de prueba
anteriores incluidas) — se auto-corrigen en el próximo resync real desde
SAP, que ya trae `gestionado_por_lote` en el payload de /sync-oc/. Mismo
criterio ya usado para `activa` (0002) y `precio_unitario`/`tax_code`
(0003).

Mientras una línea siga en `False` por no haberse resincronizado, la
pantalla de Entrada de Mercadería simplemente no le exige lote — el peor
caso es que un artículo que SÍ se gestiona por lote no pida el dato hasta
el resync; SAP lo rechazaría entonces y el mensaje llega vía reportar-error
(mismo mecanismo ya existente). No hay riesgo de dato corrupto.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sap_sync', '0004_purchaseorder_doc_cur'),
    ]

    operations = [
        migrations.AddField(
            model_name='purchaseorderline',
            name='gestionado_por_lote',
            field=models.BooleanField(default=False, help_text='ManBtchNum de SAP: True si el artículo se gestiona por lote.'),
        ),
    ]
