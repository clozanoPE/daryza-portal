# apps/invoicing/services.py
"""
Sub-fase 3.1: servicio de cálculo de saldo por línea de OC + los 2
candados de negocio que el usuario pidió explícitamente que vivieran en
el servicio, no solo en el formulario (mismo principio ya establecido en
OperationsService/AppointmentService en todo el proyecto). Sin
orquestación de creación/aprobación de Factura todavía — eso es Sub-fase
3.2/3.3/3.4, que consumirá estas funciones sin duplicar su lógica.

Todos los métodos asumen que ya corren dentro de un transaction.atomic()
abierto por el llamador (mismo criterio que el resto de servicios de la
app: el atomic() lo abre el método de más alto nivel, no cada función de
validación por separado) — select_for_update() sin una transacción activa
alrededor no bloquea nada.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError

from apps.operations.models import EntradaMercaderiaLinea

from .models import Factura, FacturaLinea


class InvoicingService:

    @staticmethod
    def saldo_disponible(po_line, excluir_factura=None):
        """
        Cantidad de po_line todavía disponible para facturar:

            EntradaMercaderiaLinea.cantidad (lo REAL recibido, la fuente ya
            establecida por OperationsService.get_estado_actual_por_oc /
            EntradaMercaderiaLinea, sesión 6/9/57 — nunca
            PurchaseOrderLine.quantity_sap)
            − Sum(FacturaLinea.cantidad de toda Factura con estado != CANCELADO
              que referencia esa línea)

        `excluir_factura` permite recalcular el saldo "como si" la propia
        Factura que se está editando no existiera todavía — evita contar
        dos veces su propia línea al validar una edición sobre una Factura
        ya existente.

        Lanza ValidationError si la línea todavía no tiene ninguna Entrada
        de Mercancía registrada — no se puede facturar sin una recepción
        física confirmada.

        NOTA de implementación (select_for_update + exclude cross-tabla):
        NO se usa `FacturaLinea.objects.select_for_update().exclude(
        factura__estado='CANCELADO')` — verificado que Django puede resolver
        un exclude() a través de una relación FK como un LEFT OUTER JOIN, y
        Postgres rechaza "SELECT ... FOR UPDATE" sobre el lado nullable de
        un outer join (`FOR UPDATE cannot be applied to the nullable side
        of an outer join`). En su lugar, se bloquean las filas con
        select_for_update()+select_related('factura') (INNER JOIN, porque
        FacturaLinea.factura es NOT NULL) y el filtro por estado se aplica
        en Python sobre filas ya cargadas — sin ningún exclude() cruzando
        tablas bajo FOR UPDATE.
        """
        try:
            entrada_linea = EntradaMercaderiaLinea.objects.select_for_update().get(po_line=po_line)
        except EntradaMercaderiaLinea.DoesNotExist:
            raise ValidationError(
                f"La línea {po_line.item_code} (OC {po_line.purchase_order.doc_num}) "
                "todavía no tiene una Entrada de Mercancía registrada — no se puede "
                "facturar sin una recepción física confirmada."
            )

        lineas_qs = FacturaLinea.objects.select_for_update().select_related('factura').filter(
            po_line=po_line,
        )
        if excluir_factura is not None:
            lineas_qs = lineas_qs.exclude(factura_id=getattr(excluir_factura, 'pk', excluir_factura))

        ya_facturado = sum(
            (linea.cantidad for linea in lineas_qs if linea.factura.estado != 'CANCELADO'),
            Decimal('0'),
        )

        return entrada_linea.cantidad - ya_facturado

    @staticmethod
    def validar_oc_disponible(purchase_order, excluir_factura=None):
        """
        Candado de unicidad de OC: bloquea que una misma OC quede vinculada
        a más de una Factura activa (estado != CANCELADO) a la vez — ver
        docstring de apps/invoicing/models.py para la justificación
        completa de por qué esto vive aquí (select_for_update) y no como
        UniqueConstraint de Postgres.

        `excluir_factura` permite validar una edición sobre una Factura ya
        existente sin que se bloquee a sí misma.

        NOTA de implementación: se bloquean filas de `Factura` (no de
        `FacturaOrdenCompra`) — `Factura.objects.select_for_update().filter(
        ordenes_compra__purchase_order=...)` resuelve el join como INNER
        JOIN (vía `.filter()` sobre la relación inversa, no `.exclude()`),
        y el filtro `estado != CANCELADO` es sobre el propio campo de
        Factura, no uno relacionado — evita por completo el mismo problema
        de outer join + FOR UPDATE documentado en saldo_disponible.
        """
        facturas_bloqueando = Factura.objects.select_for_update().filter(
            ordenes_compra__purchase_order=purchase_order,
        ).exclude(estado='CANCELADO')

        if excluir_factura is not None:
            facturas_bloqueando = facturas_bloqueando.exclude(
                pk=getattr(excluir_factura, 'pk', excluir_factura)
            )

        if facturas_bloqueando.exists():
            raise ValidationError(
                f"La OC {purchase_order.doc_num} ya está vinculada a otra Factura "
                "activa. Cancele esa Factura antes de crear una nueva para la misma OC."
            )

    @staticmethod
    def validar_retencion_detraccion(factura_linea):
        """
        Candado de servicio (no solo de formulario, pedido explícitamente):
        si aplica_retencion=True, documento_retencion es obligatorio; mismo
        criterio para aplica_detraccion/documento_detraccion.
        """
        errores = {}
        if factura_linea.aplica_retencion and not factura_linea.documento_retencion:
            errores['documento_retencion'] = (
                "Debe adjuntar el documento de retención si aplica_retencion está activo."
            )
        if factura_linea.aplica_detraccion and not factura_linea.documento_detraccion:
            errores['documento_detraccion'] = (
                "Debe adjuntar el documento de detracción si aplica_detraccion está activo."
            )
        if errores:
            raise ValidationError(errores)
