# apps/operations/migrations/0009_backfill_entrada_mercaderia.py
"""
Backfill (sesión 58): crea EntradaMercaderia/EntradaMercaderiaLinea
retroactivamente para los Tickets que ya estaban FINALIZADO antes de que
el trigger de la sesión 57 existiera — registrar_salida solo dispara la
creación hacia adelante, así que sin este backfill esos Tickets se
quedarían sin ningún borrador de Entrada de Mercancía para siempre.

Replica EXACTAMENTE la misma lógica de OperationsService.
_generar_entrada_mercaderia (cantidad = la fila de TicketLineInspection
más reciente por po_line_id, sin importar la etapa que la generó — mismo
criterio de get_estado_actual_por_oc, sesión 6/9) usando los modelos
históricos de la migración (no se puede importar OperationsService aquí:
usa las clases de modelo "reales", no las versionadas de esta migración).
estado_sap se fija en 'L' — "como si acabaran de finalizar", según lo
pedido explícitamente.
"""
from django.db import migrations


def backfill_entradas(apps, schema_editor):
    Ticket = apps.get_model('operations', 'Ticket')
    TicketLineInspection = apps.get_model('operations', 'TicketLineInspection')
    EntradaMercaderia = apps.get_model('operations', 'EntradaMercaderia')
    EntradaMercaderiaLinea = apps.get_model('operations', 'EntradaMercaderiaLinea')

    tickets_finalizados = Ticket.objects.filter(estado='FINALIZADO')

    for ticket in tickets_finalizados:
        if EntradaMercaderia.objects.filter(ticket=ticket).exists():
            continue  # ya generado por el trigger real, no tocar

        entrada = EntradaMercaderia.objects.create(ticket=ticket, estado_sap='L')

        inspecciones = TicketLineInspection.objects.filter(
            ticket=ticket
        ).order_by('po_line_id', '-fecha_registro')

        mas_reciente_por_linea = {}
        for insp in inspecciones:
            mas_reciente_por_linea.setdefault(insp.po_line_id, insp)

        for po_line_id, insp in mas_reciente_por_linea.items():
            EntradaMercaderiaLinea.objects.update_or_create(
                entrada=entrada, po_line_id=po_line_id,
                defaults={'cantidad': insp.cantidad_modificada},
            )


def eliminar_backfill(apps, schema_editor):
    """
    Reversión: solo elimina las EntradaMercaderia que este backfill pudo
    haber creado (estado_sap='L', sin doc_entry_borrador/definitivo — si
    el daemon ya avanzó alguna a 'B'/'Y', se preserva a propósito, para no
    borrar progreso real de sincronización con SAP en un rollback).
    """
    EntradaMercaderia = apps.get_model('operations', 'EntradaMercaderia')
    EntradaMercaderia.objects.filter(
        estado_sap='L', doc_entry_borrador__isnull=True, doc_entry_definitivo__isnull=True,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('operations', '0008_entradamercaderia_entradamercaderialinea'),
    ]

    operations = [
        migrations.RunPython(backfill_entradas, eliminar_backfill),
    ]
