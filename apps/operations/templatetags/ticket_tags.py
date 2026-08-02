# apps/operations/templatetags/ticket_tags.py
"""
Template tags para VBS Portal.
SOLO referencia campos REALES de los modelos del proyecto:

Appointment:
  - coa_pdf (FileField) — un solo COA por cita
  - status: SOLICITADO|CONFIRMADA|RECHAZADO|CANCELADA|FINALIZADA

Ticket:
  - estado: PROGRAMADO|EN_PLANTA|FINALIZADO|CANCELADO
  - requiere_coa (BooleanField)
  - codigo_qr, fecha_creacion, tiempo_total_planta

TicketLineInspection:
  - cantidad_sap (editable=False)
  - cantidad_modificada  ← campo real (NO cantidad_real)
  - estado: PENDIENTE|EN_PROCESO|CONFORME|RECHAZADO
  - comentario, evidencia_url, fecha_registro
  - etapa: VIGILANCIA|ALMACEN|CALIDAD

TicketStage:
  - etapa: VIGILANCIA_ENTRADA|ALMACEN_RECEPCION|CALIDAD_INSPECCION|VIGILANCIA_SALIDA
"""
from django import template
from apps.operations.models import TicketLineInspection

register = template.Library()


@register.filter(name='get_inspeccion')
def get_inspeccion(po_line, ticket):
    """
    Uso: {{ linea|get_inspeccion:ticket }}
    Retorna la inspección más reciente para esa línea en ese ticket
    (sin importar etapa — visualización según grupo se hace en la vista).
    """
    if not ticket or not po_line:
        return None
    return (
        TicketLineInspection.objects
        .filter(ticket=ticket, po_line=po_line)
        .order_by('-fecha_registro')
        .first()
    )


@register.filter(name='get_inspeccion_etapa')
def get_inspeccion_etapa(po_line, ticket_etapa):
    """
    Uso: {{ linea|get_inspeccion_etapa:ticket_etapa_str }}
    ticket_etapa_str debe ser "ticket_id|ETAPA", construido en la vista.
    Se usa en inspeccion_detalle.html.
    """
    if not po_line or not ticket_etapa:
        return None
    try:
        parts = str(ticket_etapa).split('|')
        if len(parts) != 2:
            return None
        ticket_id, etapa = int(parts[0]), parts[1]
        return TicketLineInspection.objects.filter(
            ticket_id=ticket_id,
            po_line=po_line,
            etapa=etapa
        ).first()
    except (ValueError, TypeError):
        return None


@register.filter(name='variacion')
def variacion(inspeccion):
    """
    Uso: {{ inspeccion|variacion }}
    Calcula cantidad_modificada - cantidad_sap.
    Campo real: cantidad_modificada (no cantidad_real).
    """
    if not inspeccion:
        return None
    try:
        return inspeccion.cantidad_modificada - inspeccion.cantidad_sap
    except (TypeError, AttributeError):
        return None


@register.filter(name='porcentaje')
def porcentaje(valor, total):
    """Uso: {{ conformes|porcentaje:total }}"""
    try:
        t = float(total)
        return round((float(valor) / t) * 100) if t > 0 else 0
    except (TypeError, ZeroDivisionError):
        return 0


@register.filter(name='slot_css')
def slot_css(slot):
    """
    Uso: {{ slot|slot_css }}
    Retorna clase CSS del slot según estado real.
    """
    if not slot:
        return 'slot-empty'
    if slot.is_full_override:
        return 'slot-blocked'
    try:
        ocupacion = slot.appointments.filter(
            status__in=['SOLICITADO', 'CONFIRMADA']
        ).count()
    except Exception:
        return 'slot-empty'
    if ocupacion >= slot.max_capacity:
        return 'slot-full'
    if ocupacion >= slot.max_capacity * 0.75:
        return 'slot-partial'
    return 'slot-available'


@register.filter(name='slot_disponibles')
def slot_disponibles(slot):
    """Uso: {{ slot|slot_disponibles }} — retorna cuántos cupos quedan."""
    if not slot:
        return 0
    try:
        ocupacion = slot.appointments.filter(
            status__in=['SOLICITADO', 'CONFIRMADA']
        ).count()
        return max(0, slot.max_capacity - ocupacion)
    except Exception:
        return 0
