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
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from django.db.models import Prefetch, Q, QuerySet, Sum

# Sentinel real (no `None`) para distinguir "el caller no pasó nada,
# resuélvelo con una query" de "el caller SÍ resolvió el valor y
# genuinamente es None/vacío" — ver el bug de N+1 corregido en la sesión
# 83, docstring de calcular_estado_despacho/calcular_estado_facturacion.
_SIN_RESOLVER = object()

ESTADO_PENDIENTE = 'PENDIENTE'
ESTADO_EN_PROCESO = 'EN_PROCESO'
ESTADO_DESPACHO_PARCIAL = 'DESPACHO_PARCIAL'
ESTADO_DESPACHADA = 'DESPACHADA'

ESTADO_LABELS = {
    ESTADO_PENDIENTE: 'Pendiente',
    ESTADO_EN_PROCESO: 'En Proceso',
    ESTADO_DESPACHO_PARCIAL: 'Despacho Parcial',
    ESTADO_DESPACHADA: 'Despachada',
}

# Estados de Appointment que resuelven "la cita más reciente a mostrar" en
# el panel (para la sede/fecha/link de trazabilidad). Incluye FINALIZADA a
# propósito — el link de trazabilidad de la última entrega sigue siendo
# útil aunque ese ciclo ya terminó. RECHAZADO/CANCELADA no cuentan.
# NOTA: desde la sesión 99c el ESTADO de despacho ya NO se deriva de esto
# (se deriva de las cantidades recibidas vs. ordenadas) — esta lista solo
# elige qué cita mostrar.
APPOINTMENT_ESTADOS_ACTIVOS = ['SOLICITADO', 'CONFIRMADA', 'FINALIZADA']

# Un ciclo de entrega está EN CURSO ahora mismo si hay una cita en uno de
# estos estados (aún no finalizada).
APPOINTMENT_ESTADOS_EN_CURSO = ['SOLICITADO', 'CONFIRMADA']

# ── Facturación (Sub-fase 3.7, sesión 83; parcial sesión 99c) ────────────

ESTADO_FACT_SIN_FACTURAR = 'SIN_FACTURAR'
ESTADO_FACT_PARCIAL = 'FACTURACION_PARCIAL'
ESTADO_FACT_EN_CURSO = 'FACTURACION_EN_CURSO'
ESTADO_FACT_FACTURADA = 'FACTURADA'

ESTADO_FACTURACION_LABELS = {
    ESTADO_FACT_SIN_FACTURAR: 'Sin Facturar',
    ESTADO_FACT_PARCIAL: 'Facturación Parcial',
    ESTADO_FACT_EN_CURSO: 'Facturación en Curso',
    ESTADO_FACT_FACTURADA: 'Facturada',
}

# Estados de Factura "en vuelo" — el proveedor la está armando (BORRADOR)
# o ya la envió y está en revisión/fue observada por Compras. Una OC no
# puede tener 2 de estas a la vez. APROBADA_COMPRAS NO está acá: una OC
# abierta con despachos parciales se factura en varias APROBADA_COMPRAS a
# lo largo del tiempo (sesión 99c). Espejo de
# InvoicingService.FACTURA_ESTADOS_EN_VUELO.
FACTURA_ESTADOS_EN_CURSO = ['BORRADOR', 'EN_REVISION_COMPRAS', 'OBSERVADA']

# Estado de EntradaMercaderia que cuenta como "recibido y confirmado en
# SAP" para la fracción FACTURABLE (mismo criterio que
# InvoicingService.saldo_disponible / punto (c) sesión 99c).
_EM_ESTADO_CONFIRMADO = 'CREADO_SAP'


@dataclass
class EstadoDespachoOC:
    purchase_order: object
    estado: str
    appointment: Optional[object] = None
    ticket_id: Optional[int] = None
    estado_facturacion: str = ESTADO_FACT_SIN_FACTURAR
    factura_id: Optional[int] = None
    # Sesión 99c — progreso cuantitativo (OC abierta con N despachos parciales).
    # Sumas sobre las líneas ACTIVAS de la OC:
    cantidad_ordenada: Decimal = field(default_factory=lambda: Decimal('0'))
    cantidad_recibida: Decimal = field(default_factory=lambda: Decimal('0'))   # todas las rondas
    cantidad_facturada: Decimal = field(default_factory=lambda: Decimal('0'))  # Facturas no canceladas

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

    @staticmethod
    def _pct(parte: Decimal, total: Decimal) -> int:
        if total <= 0:
            return 0
        return min(100, int((parte / total) * 100))

    @property
    def pct_recibido(self) -> int:
        return self._pct(self.cantidad_recibida, self.cantidad_ordenada)

    @property
    def pct_facturado(self) -> int:
        return self._pct(self.cantidad_facturada, self.cantidad_ordenada)

    @property
    def progreso_despacho(self) -> str:
        """'140 / 200' — sin decimales sobrantes."""
        return f"{_num(self.cantidad_recibida)} / {_num(self.cantidad_ordenada)}"

    @property
    def progreso_facturacion(self) -> str:
        return f"{_num(self.cantidad_facturada)} / {_num(self.cantidad_ordenada)}"


def _num(d: Decimal) -> str:
    """Formatea un Decimal sin ceros de cola (200.0000 -> '200', 12.5000 -> '12.5')."""
    d = Decimal(d or 0)
    entero = d.to_integral_value()
    if d == entero:
        return str(int(entero))
    return format(d.normalize(), 'f')


# ── Agregación de cantidades (1 query, sirva para 1 OC o para un lote) ───

def _clave_por_linea(qs) -> dict:
    return {
        (r['po_line__purchase_order_id'], r['po_line_id']): (r['t'] or Decimal('0'))
        for r in qs.values('po_line__purchase_order_id', 'po_line_id').annotate(t=Sum('cantidad'))
    }


def recibido_por_linea(pos, *, solo_confirmado: bool) -> dict:
    """
    {(po_id, po_line_id): Decimal} — Σ EntradaMercaderiaLinea.cantidad de
    TODAS las rondas de recepción. `pos` = un PurchaseOrder o un
    iterable/queryset de ellos. Una sola query.

      solo_confirmado=True  → solo rondas con Entrada en estado CREADO_SAP
                              (la fracción FACTURABLE — mismo criterio que
                              InvoicingService.saldo_disponible / punto (c)
                              de la sesión 99c).
      solo_confirmado=False → todas las rondas, sin importar estado_sap
                              (lo físicamente recibido — la cantidad ya es
                              real desde que Vigilancia registró la salida).
    """
    from apps.operations.models import EntradaMercaderiaLinea

    po_ids = [pos.pk] if hasattr(pos, 'pk') else [p.pk for p in pos]
    if not po_ids:
        return {}
    qs = EntradaMercaderiaLinea.objects.filter(po_line__purchase_order_id__in=po_ids)
    if solo_confirmado:
        qs = qs.filter(entrada__estado=_EM_ESTADO_CONFIRMADO)
    return _clave_por_linea(qs)


def facturado_por_linea(pos) -> dict:
    """{(po_id, po_line_id): Decimal} — Σ FacturaLinea.cantidad de Facturas
    NO canceladas. Una sola query."""
    from apps.invoicing.models import FacturaLinea

    po_ids = [pos.pk] if hasattr(pos, 'pk') else [p.pk for p in pos]
    if not po_ids:
        return {}
    qs = FacturaLinea.objects.filter(
        po_line__purchase_order_id__in=po_ids
    ).exclude(factura__estado='CANCELADO')
    return _clave_por_linea(qs)


def oc_totalmente_recibida(po) -> bool:
    """
    True si TODA línea ACTIVA de la OC ya recibió (sumando TODAS las
    rondas, sin importar estado_sap) al menos su `quantity_sap`. Una OC
    sin ninguna línea activa devuelve False (no hay nada que recibir —
    no cuenta como "completa"). Usado por
    AppointmentService.solicitar_cita_borrador para decidir si se puede
    agendar otra cita sobre el saldo pendiente.
    """
    activas = [l for l in po.lines.all() if l.activa]
    if not activas:
        return False
    recibido = recibido_por_linea(po, solo_confirmado=False)
    return all(recibido.get((po.pk, l.pk), Decimal('0')) >= l.quantity_sap for l in activas)


def calcular_estado_despacho(
    po, appointment_activo=_SIN_RESOLVER, recibido=_SIN_RESOLVER,
) -> EstadoDespachoOC:
    """
    Estado de DESPACHO de una OC — sesión 99c: se deriva de las CANTIDADES
    (recibido vs. ordenado, sumando todas las rondas), no de "el último
    Ticket finalizó":

      PENDIENTE        → 0 recibido y sin ninguna cita en curso.
      EN_PROCESO       → hay una cita EN CURSO ahora mismo (SOLICITADO/
                         CONFIRMADA, o Ticket aún no FINALIZADO).
      DESPACHO_PARCIAL → recibido > 0 y < ordenado, sin cita en curso
                         ("esperando más entregas").
      DESPACHADA       → recibido >= ordenado (todas las líneas activas).

    `appointment_activo` (la cita más reciente en un estado de
    APPOINTMENT_ESTADOS_ACTIVOS, incl. FINALIZADA) solo aporta sede /
    fecha / link de trazabilidad — YA NO define el estado.

    `recibido` = dict {(po_id, po_line_id): Decimal} de recibido-todas-las-
    rondas; si no se pasa, se resuelve con 1 query. Sentinel `_SIN_RESOLVER`
    (no `None`) — mismo motivo que en la sesión 83 (un `None` legítimo no
    debe re-disparar la query).
    """
    if appointment_activo is _SIN_RESOLVER:
        appointment_activo = po.appointment_set.filter(
            status__in=APPOINTMENT_ESTADOS_ACTIVOS
        ).select_related('ticket', 'sede', 'slot').order_by('-created_at').first()
    if recibido is _SIN_RESOLVER:
        recibido = recibido_por_linea(po, solo_confirmado=False)

    # po.lines viene prefetcheado desde construir_filas; en una llamada
    # directa .all() hace su propia query (aceptable, contexto de 1 OC).
    activas = [l for l in po.lines.all() if l.activa]

    ordenada = sum((l.quantity_sap for l in activas), Decimal('0'))
    recibida = sum((recibido.get((po.pk, l.pk), Decimal('0')) for l in activas), Decimal('0'))

    ticket = getattr(appointment_activo, 'ticket', None) if appointment_activo else None
    hay_cita_en_curso = appointment_activo is not None and (
        appointment_activo.status in APPOINTMENT_ESTADOS_EN_CURSO
        or (ticket is not None and ticket.estado != 'FINALIZADO')
    )

    if hay_cita_en_curso:
        estado = ESTADO_EN_PROCESO
    elif ordenada > 0 and recibida >= ordenada:
        estado = ESTADO_DESPACHADA
    elif recibida > 0:
        estado = ESTADO_DESPACHO_PARCIAL
    else:
        estado = ESTADO_PENDIENTE

    return EstadoDespachoOC(
        purchase_order=po,
        estado=estado,
        appointment=appointment_activo,
        ticket_id=ticket.id if ticket else None,
        cantidad_ordenada=ordenada,
        cantidad_recibida=recibida,
    )


def calcular_estado_facturacion(po, factura_activa=_SIN_RESOLVER,
                                facturado=_SIN_RESOLVER, cantidad_ordenada=_SIN_RESOLVER):
    """
    Estado de FACTURACIÓN de una OC — sesión 99c: se deriva de las
    CANTIDADES (facturado vs. ordenado), con la existencia de una Factura
    EN VUELO como matiz:

      SIN_FACTURAR         → 0 facturado y ninguna Factura en vuelo.
      FACTURACION_EN_CURSO → hay una Factura en vuelo (BORRADOR/
                             EN_REVISION_COMPRAS/OBSERVADA) y todavía no se
                             facturó todo.
      FACTURACION_PARCIAL  → facturado > 0 y < ordenado, sin Factura en
                             vuelo ("esperando la siguiente entrega para
                             seguir facturando").
      FACTURADA            → facturado >= ordenado (OC cerrable).

    Antes (Sub-fase 3.7) se derivaba solo de `Factura.estado` de la más
    reciente — no distinguía "facturé 80 de 200 y la Factura ya está
    aprobada" (parcial, la OC sigue abierta) de "facturé todo". Con OC
    abierta y N Facturas parciales eso era insuficiente.

    Devuelve (estado: str, factura_id: int | None, cantidad_facturada: Decimal).
    """
    if factura_activa is _SIN_RESOLVER:
        from apps.invoicing.models import FacturaOrdenCompra

        fac_oc = FacturaOrdenCompra.objects.filter(purchase_order=po).exclude(
            factura__estado='CANCELADO'
        ).select_related('factura').order_by('-factura__created_at').first()
        factura_activa = fac_oc.factura if fac_oc else None

    activas = [l for l in po.lines.all() if l.activa]
    if cantidad_ordenada is _SIN_RESOLVER:
        cantidad_ordenada = sum((l.quantity_sap for l in activas), Decimal('0'))
    if facturado is _SIN_RESOLVER:
        facturado = facturado_por_linea(po)

    total_facturado = sum(
        (facturado.get((po.pk, l.pk), Decimal('0')) for l in activas), Decimal('0')
    )
    factura_id = factura_activa.id if factura_activa is not None else None
    hay_en_vuelo = (
        factura_activa is not None
        and factura_activa.estado in FACTURA_ESTADOS_EN_CURSO
    )

    # Una Factura EN VUELO manda sobre las cantidades: hay trabajo de
    # facturación en curso ahora mismo, aunque la línea ya cubra el total.
    if hay_en_vuelo:
        estado = ESTADO_FACT_EN_CURSO
    elif total_facturado <= 0:
        estado = ESTADO_FACT_SIN_FACTURAR
    elif cantidad_ordenada > 0 and total_facturado >= cantidad_ordenada:
        estado = ESTADO_FACT_FACTURADA
    else:
        estado = ESTADO_FACT_PARCIAL

    return estado, factura_id, total_facturado


def construir_filas_estado_oc(pos_qs: QuerySet) -> list[EstadoDespachoOC]:
    """
    Dado un queryset de PurchaseOrder YA FILTRADO (período/proveedor/etc.
    por el caller), calcula despacho + facturación de cada una en bloque,
    SIN N+1: 3 prefetch (citas activas + líneas + facturas activas) + 2
    queries de agregación (recibido por línea todas-las-rondas, facturado
    por línea) — 5 en total, constante, sin importar cuántas OC.

    Excluye del resultado las OC que quedarían PENDIENTE (despacho) pero
    no tienen ninguna línea activa (PurchaseOrderLine.activa=True, sesión
    49/50). Una OC con despacho ya en curso/parcial/completo se sigue
    mostrando aunque sus líneas se hayan cancelado después.
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

    pos = list(pos_qs)
    recibido = recibido_por_linea(pos, solo_confirmado=False)
    facturado = facturado_por_linea(pos)

    filas = []
    for po in pos:
        appointment_activo = po._citas_activas[0] if po._citas_activas else None
        fila = calcular_estado_despacho(
            po, appointment_activo=appointment_activo, recibido=recibido,
        )
        if fila.estado == ESTADO_PENDIENTE and not any(l.activa for l in po.lines.all()):
            continue

        factura_activa = po._facturas_activas[0].factura if po._facturas_activas else None
        fila.estado_facturacion, fila.factura_id, fila.cantidad_facturada = (
            calcular_estado_facturacion(
                po, factura_activa=factura_activa, facturado=facturado,
                cantidad_ordenada=fila.cantidad_ordenada,
            )
        )
        filas.append(fila)
    return filas
