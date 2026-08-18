# apps/base/oc_status.py
"""
Estado de DESPACHO (recepción física) de una PurchaseOrder — Panel de
Consulta de OC (sesión 52), en 2 vistas: Compras (todas las OC) y el
Portal del Proveedor (solo las suyas). Vive en apps.base, mismo motivo que
apps.base.filters/apps.base.reporting (Fase 10/15): lo consume tanto
apps.operations (vista de Compras) como apps.appointments (vista del
proveedor) — evita el import cruzado entre apps de negocio.

ALCANCE DE ESTA FASE: solo estado de despacho. El estado de FACTURACIÓN
queda fuera a propósito — el modelo Factura/FacturaLinea todavía no existe
(Fase 3 futura). No hay ningún campo/placeholder de facturación aquí; la
UI que consuma EstadoDespachoOC decide cómo mostrar esa columna vacía.
"""
from dataclasses import dataclass
from typing import Optional

from django.db.models import Prefetch, QuerySet

ESTADO_PENDIENTE = 'PENDIENTE'
ESTADO_EN_PROCESO = 'EN_PROCESO'
ESTADO_DESPACHADA = 'DESPACHADA'

ESTADO_LABELS = {
    ESTADO_PENDIENTE: 'Pendiente',
    ESTADO_EN_PROCESO: 'En Proceso',
    ESTADO_DESPACHADA: 'Despachada',
}

# Estados de Appointment considerados "en curso" para efectos de esta
# consulta — misma señal que ya usa AppointmentService.solicitar_cita_
# borrador para bloquear una OC de volver a solicitarse (apps/appointments/
# services.py). RECHAZADO/CANCELADA no cuentan: la OC queda libre otra vez,
# como si nunca se hubiera solicitado, así que no debe verse como "en curso"
# en este panel tampoco — vuelve a PENDIENTE.
APPOINTMENT_ESTADOS_ACTIVOS = ['SOLICITADO', 'CONFIRMADA', 'FINALIZADA']


@dataclass
class EstadoDespachoOC:
    purchase_order: object
    estado: str
    appointment: Optional[object] = None
    ticket_id: Optional[int] = None

    @property
    def estado_display(self) -> str:
        return ESTADO_LABELS[self.estado]

    @property
    def sede_nombre(self) -> str:
        return self.appointment.sede.nombre if self.appointment else ''

    @property
    def fecha_cita(self):
        if self.appointment and self.appointment.slot:
            return self.appointment.slot.date
        return None


def calcular_estado_despacho(po, appointment_activo=None) -> EstadoDespachoOC:
    """
    Determina el estado de despacho de una OC:
      PENDIENTE   → sin ningún Appointment activo (ver APPOINTMENT_ESTADOS_
                    ACTIVOS) vinculado todavía.
      EN_PROCESO  → tiene un Appointment activo, pero su Ticket (si ya
                    existe — se crea recién al confirmar la cita, no al
                    solicitarla) todavía no llegó a FINALIZADO.
      DESPACHADA  → el Ticket del Appointment activo está FINALIZADO
                    (coincide 1:1 con Appointment.status='FINALIZADA',
                    fijado por OperationsService.registrar_salida).

    `appointment_activo` puede pasarse ya resuelto (evita una query por OC
    cuando el caller ya lo prefetcheó vía construir_filas_estado_oc) — si
    no, se resuelve aquí con una query directa.
    """
    if appointment_activo is None:
        appointment_activo = po.appointment_set.filter(
            status__in=APPOINTMENT_ESTADOS_ACTIVOS
        ).select_related('ticket', 'sede', 'slot').order_by('-created_at').first()

    if appointment_activo is None:
        return EstadoDespachoOC(purchase_order=po, estado=ESTADO_PENDIENTE)

    ticket = getattr(appointment_activo, 'ticket', None)
    estado = ESTADO_DESPACHADA if (ticket and ticket.estado == 'FINALIZADO') else ESTADO_EN_PROCESO

    return EstadoDespachoOC(
        purchase_order=po,
        estado=estado,
        appointment=appointment_activo,
        ticket_id=ticket.id if ticket else None,
    )


def construir_filas_estado_oc(pos_qs: QuerySet) -> list[EstadoDespachoOC]:
    """
    Dado un queryset de PurchaseOrder YA FILTRADO (período/proveedor/etc.
    por el caller), calcula el estado de despacho de cada una en bloque
    (sin N+1: un solo prefetch de Appointments activos + líneas).

    Excluye del resultado las OC que quedarían PENDIENTE pero no tienen
    ninguna línea activa (PurchaseOrderLine.activa=True, sesión 49/50) —
    una OC totalmente cancelada en SAP no debe aparecer como "pendiente"
    de nada. Si la OC ya tiene un Appointment activo (EN_PROCESO/
    DESPACHADA), se sigue mostrando aunque sus líneas se hayan cancelado
    después — es historial real de algo que ya ocurrió, no un fantasma.
    """
    from apps.appointments.models import Appointment

    pos_qs = pos_qs.prefetch_related(
        Prefetch(
            'appointment_set',
            queryset=Appointment.objects.filter(
                status__in=APPOINTMENT_ESTADOS_ACTIVOS
            ).select_related('ticket', 'sede', 'slot').order_by('-created_at'),
            to_attr='_citas_activas',
        ),
        'lines',
    )

    filas = []
    for po in pos_qs:
        appointment_activo = po._citas_activas[0] if po._citas_activas else None
        fila = calcular_estado_despacho(po, appointment_activo=appointment_activo)
        if fila.estado == ESTADO_PENDIENTE and not any(l.activa for l in po.lines.all()):
            continue
        filas.append(fila)
    return filas
