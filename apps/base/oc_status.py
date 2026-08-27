# apps/base/oc_status.py
"""
Estado de DESPACHO (recepción física) y de FACTURACIÓN de una
PurchaseOrder — Panel de Consulta de OC (sesión 52 para despacho, sesión
83 para facturación — cierre de la Fase 3 completa), en 2 vistas: Compras
(todas las OC) y el Portal del Proveedor (solo las suyas). Vive en
apps.base, mismo motivo que apps.base.filters/apps.base.reporting (Fase
10/15): lo consume tanto apps.operations (vista de Compras) como
apps.appointments (vista del proveedor) — evita el import cruzado entre
apps de negocio.

DECISIÓN — no se renombró EstadoDespachoOC pese a que ahora también
carga el estado de facturación: el nombre sigue siendo preciso para el
CAMPO `estado` (despacho, la columna original), y renombrar la clase
habría tocado 2 vistas + 2 templates sin necesidad real — los 2 campos
nuevos (`estado_facturacion`/`factura_id`) se agregan al mismo dataclass
en vez de crear uno paralelo, para no forzar a cada caller a combinar 2
resultados por fila.
"""
from dataclasses import dataclass
from typing import Optional

from django.db.models import Prefetch, QuerySet

# Sentinel real (no `None`) para distinguir "el caller no pasó nada,
# resuélvelo con una query" de "el caller SÍ resolvió el valor y
# genuinamente es None/vacío" — ver el bug de N+1 corregido en la sesión
# 83, docstring de calcular_estado_despacho/calcular_estado_facturacion.
_SIN_RESOLVER = object()

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

# ── Facturación (Sub-fase 3.7, sesión 83) ────────────────────────────────

ESTADO_FACT_SIN_FACTURAR = 'SIN_FACTURAR'
ESTADO_FACT_EN_CURSO = 'FACTURACION_EN_CURSO'
ESTADO_FACT_FACTURADA = 'FACTURADA'

ESTADO_FACTURACION_LABELS = {
    ESTADO_FACT_SIN_FACTURAR: 'Sin Facturar',
    ESTADO_FACT_EN_CURSO: 'Facturación en Curso',
    ESTADO_FACT_FACTURADA: 'Facturada',
}

# Estados de Factura considerados "en curso" (ni sin facturar ni ya
# facturada) — el proveedor la está armando (BORRADOR) o ya la envió y
# está en revisión/fue observada por Compras. APROBADA_COMPRAS queda
# fuera de esta lista a propósito: esa es FACTURADA (ver
# calcular_estado_facturacion). CANCELADO tampoco entra aquí — una
# Factura cancelada no cuenta como "activa" en absoluto (mismo criterio
# ya establecido en InvoicingService.validar_oc_disponible/
# saldo_disponible: estado != CANCELADO), así que una OC cuya única
# Factura fue cancelada vuelve a SIN_FACTURAR, no queda "en curso".
FACTURA_ESTADOS_EN_CURSO = ['BORRADOR', 'EN_REVISION_COMPRAS', 'OBSERVADA']


@dataclass
class EstadoDespachoOC:
    purchase_order: object
    estado: str
    appointment: Optional[object] = None
    ticket_id: Optional[int] = None
    estado_facturacion: str = ESTADO_FACT_SIN_FACTURAR
    factura_id: Optional[int] = None

    @property
    def estado_display(self) -> str:
        return ESTADO_LABELS[self.estado]

    @property
    def estado_facturacion_display(self) -> str:
        return ESTADO_FACTURACION_LABELS[self.estado_facturacion]

    @property
    def sede_nombre(self) -> str:
        return self.appointment.sede.nombre if self.appointment else ''

    @property
    def fecha_cita(self):
        if self.appointment and self.appointment.slot:
            return self.appointment.slot.date
        return None


def calcular_estado_despacho(po, appointment_activo=_SIN_RESOLVER) -> EstadoDespachoOC:
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
    no se pasa NADA, se resuelve aquí con una query directa.

    BUG REAL corregido (sesión 83, encontrado al verificar N+1 para la
    columna de facturación — punto 2 del pedido, "verifica que esto NO
    reintroduce el mismo problema de N+1 ya cuidado en la sesión 52"):
    el default anterior era `appointment_activo=None`, el mismo valor que
    `construir_filas_estado_oc` pasa explícitamente para una OC que SÍ
    fue prefetcheada pero no tiene ningún Appointment activo (PENDIENTE)
    — `None` no distinguía "no resuelto todavía" de "resuelto, y
    genuinamente no hay ninguno", así que el `if appointment_activo is
    None` de abajo volvía a disparar la query de fallback en CADA fila
    PENDIENTE, sin importar el prefetch ya hecho. Confirmado
    empíricamente antes de corregir: 5 OC PENDIENTE generaban 14 queries
    en vez de las 4 esperadas (1 principal + 3 prefetch). Corregido con
    un sentinel real (`_SIN_RESOLVER`, un `object()` que nunca es igual
    a `None`) — el default del parámetro, no `None`.
    """
    if appointment_activo is _SIN_RESOLVER:
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


def calcular_estado_facturacion(po, factura_activa=_SIN_RESOLVER):
    """
    Determina el estado de FACTURACIÓN de una OC — Sub-fase 3.7 (cierre
    de la Fase 3 de Facturación completa). Reutiliza directamente
    Factura/FacturaOrdenCompra, ya construidos en las Sub-fases 3.1-3.6,
    sin ninguna lógica de negocio nueva:

      SIN_FACTURAR          → ninguna FacturaOrdenCompra ACTIVA (Factura.
                               estado != 'CANCELADO', mismo criterio de
                               "activa" ya establecido en InvoicingService.
                               validar_oc_disponible/saldo_disponible)
                               vinculada a esta OC.
      FACTURACION_EN_CURSO  → tiene una Factura activa en BORRADOR/
                               EN_REVISION_COMPRAS/OBSERVADA — el
                               proveedor la está armando, Compras
                               todavía la está revisando, o la observó.
      FACTURADA              → tiene una Factura activa en
                               APROBADA_COMPRAS.

    DECISIÓN sobre el corte exacto (pedido explícito: "ajusta si tiene
    más sentido otro punto de la máquina de estados... justifícalo"): se
    usa `Factura.estado == 'APROBADA_COMPRAS'`, NO `estado_sap in
    ('B','Y')`. Ambos criterios son equivalentes en la práctica —
    InvoicingService.aprobar_factura (Sub-fase 3.5) fija
    `estado='APROBADA_COMPRAS'` y `estado_sap='L'` en el MISMO
    `.save()`, así que no existe ninguna Factura aprobada por Compras
    con `estado_sap` todavía en '' — pero `estado` es la señal de
    negocio primaria, siempre disponible y estable, mientras que
    `estado_sap` puede quedarse en 'L' indefinidamente si el daemon SAP
    (Sub-fase 3.6) todavía no la tomó, sin que eso cambie el hecho de
    que, desde la perspectiva de este Portal, la OC YA está facturada
    (Compras ya la aprobó). Mismo principio ya aplicado en el proyecto
    para preferir una señal de negocio estable sobre un campo de
    sincronización externa (p. ej. `Ticket.requiere_calidad` sobre
    `tipo_flujo`, rediseño Materia Prima).

    `factura_activa` puede pasarse ya resuelta (evita una query por OC
    cuando el caller ya la prefetcheó vía construir_filas_estado_oc) —
    si no se pasa NADA, se resuelve aquí con una query directa. Mismo
    sentinel `_SIN_RESOLVER` (no `None`) que calcular_estado_despacho,
    y por el mismo motivo — ver su docstring para el bug real que esto
    corrige (confirmado empírico, sesión 83).

    Devuelve (estado_facturacion: str, factura_id: int | None).
    """
    if factura_activa is _SIN_RESOLVER:
        from apps.invoicing.models import FacturaOrdenCompra

        fac_oc = FacturaOrdenCompra.objects.filter(purchase_order=po).exclude(
            factura__estado='CANCELADO'
        ).select_related('factura').order_by('-factura__created_at').first()
        factura_activa = fac_oc.factura if fac_oc else None

    if factura_activa is None:
        return ESTADO_FACT_SIN_FACTURAR, None
    if factura_activa.estado == 'APROBADA_COMPRAS':
        return ESTADO_FACT_FACTURADA, factura_activa.id
    return ESTADO_FACT_EN_CURSO, factura_activa.id


def construir_filas_estado_oc(pos_qs: QuerySet) -> list[EstadoDespachoOC]:
    """
    Dado un queryset de PurchaseOrder YA FILTRADO (período/proveedor/etc.
    por el caller), calcula el estado de despacho Y de facturación de
    cada una en bloque (sin N+1: un prefetch de Appointments activos +
    líneas + Facturas activas, 3 en total — mismo cuidado ya aplicado en
    la sesión 52 al construir este mismo helper, extendido aquí sin
    reintroducir el patrón N+1 que esa sesión evitó).

    Excluye del resultado las OC que quedarían PENDIENTE (despacho) pero
    no tienen ninguna línea activa (PurchaseOrderLine.activa=True, sesión
    49/50) — una OC totalmente cancelada en SAP no debe aparecer como
    "pendiente" de nada. Si la OC ya tiene un Appointment activo
    (EN_PROCESO/DESPACHADA), se sigue mostrando aunque sus líneas se
    hayan cancelado después — es historial real de algo que ya ocurrió,
    no un fantasma. Este filtro sigue siendo solo sobre el estado de
    DESPACHO — una OC con Factura activa pero sin líneas activas (caso
    de borde no observado en datos reales) también queda excluida, ya
    que el filtro corre antes de anexar la facturación; no se agregó un
    segundo criterio de exclusión basado en facturación (no pedido, y
    esa combinación no es alcanzable por el flujo real: una Factura solo
    puede crearse contra líneas ya recibidas físicamente, ver
    services_borrador.lineas_disponibles_de_oc).
    """
    from apps.appointments.models import Appointment
    from apps.invoicing.models import FacturaOrdenCompra

    pos_qs = pos_qs.prefetch_related(
        Prefetch(
            'appointment_set',
            queryset=Appointment.objects.filter(
                status__in=APPOINTMENT_ESTADOS_ACTIVOS
            ).select_related('ticket', 'sede', 'slot').order_by('-created_at'),
            to_attr='_citas_activas',
        ),
        'lines',
        Prefetch(
            'facturas_oc',
            queryset=FacturaOrdenCompra.objects.exclude(
                factura__estado='CANCELADO'
            ).select_related('factura').order_by('-factura__created_at'),
            to_attr='_facturas_activas',
        ),
    )

    filas = []
    for po in pos_qs:
        appointment_activo = po._citas_activas[0] if po._citas_activas else None
        fila = calcular_estado_despacho(po, appointment_activo=appointment_activo)
        if fila.estado == ESTADO_PENDIENTE and not any(l.activa for l in po.lines.all()):
            continue

        factura_activa = po._facturas_activas[0].factura if po._facturas_activas else None
        fila.estado_facturacion, fila.factura_id = calcular_estado_facturacion(
            po, factura_activa=factura_activa,
        )
        filas.append(fila)
    return filas
