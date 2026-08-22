# apps/invoicing/models.py
"""
Sub-fase 3.1 (modelo de datos): Factura + tablas relacionadas del flujo de
Facturación. Estructura y máquina de estados solamente — sin lógica de
validación de archivos (hash/firma XAdES, eso ya vive de forma aislada en
services_validacion.py desde la sesión anterior) ni endpoints todavía
(Sub-fase 3.2/3.3). El único servicio implementado en esta fase es el
candado de saldo por línea (apps/invoicing/services.py).

DECISIÓN — proveedor (Factura.proveedor): reemplazado por FK a
`apps.base.SupplierProfile` (sesión posterior a la creación de este
modelo). El TODO original de este docstring ("si se construye
SupplierProfile, migrar de User a esa FK") ya se resolvió — el modelo
mínimo de `SupplierProfile` se construyó específicamente para esto, con
`sap_card_code` sincronizado automáticamente desde `apps.sap_sync` en
cada OC entrante (ver `apps/base/supplier_sync.py`). Confirmado antes de
migrar que no había ninguna `Factura` real (ni local ni en producción) —
la migración de esquema (`0002_alter_factura_proveedor.py`) es una
`AlterField` limpia, sin `RunPython` de datos.

DECISIÓN — candado de unicidad de OC (FacturaOrdenCompra): NO se
implementó como `UniqueConstraint` de Postgres con `condition`. Un índice
parcial de Postgres solo puede referenciar columnas de la propia tabla
que indexa (FacturaOrdenCompra); "estado != CANCELADO" vive en `Factura`
(tabla relacionada, alcanzable solo vía JOIN) — no es expresable como
predicado de un índice parcial sin desnormalizar una copia de
`Factura.estado` en `FacturaOrdenCompra`, con el riesgo real de que esa
copia quede desincronizada si `estado` cambia por un camino que no la
actualice (bulk update, futuro código que olvide el paso). En su lugar,
el candado vive en `apps/invoicing/services.py::InvoicingService.
validar_oc_disponible`, con `select_for_update()` dentro del
`transaction.atomic()` del llamador — mismo principio que
`AppointmentService.solicitar_cita_borrador` ya usa para el problema
estructuralmente idéntico de "OC ya en uso" en una Appointment (sesión
28), generalizado aquí con locking explícito.
"""
from django.conf import settings
from django.db import models

from apps.base.models import Sede, SupplierProfile, TimeStampedModel
from apps.operations.models import EntradaMercaderia
from apps.sap_sync.models import PurchaseOrder, PurchaseOrderLine


class Factura(TimeStampedModel):
    """
    Comprobante de pago electrónico (factura) de un proveedor, en revisión
    por Compras antes de sincronizarse a SAP como Entrada de Mercancía +
    documento de compra.

    MÁQUINA DE ESTADOS (estado, de negocio/UI — candado de servicio
    pendiente de Sub-fase 3.4, igual que Ticket.etapa_actual lo tuvo desde
    la sesión 4):
        BORRADOR             → el proveedor está cargando/corrigiendo datos
        EN_REVISION_COMPRAS  → enviada, pendiente de que Compras la revise
        OBSERVADA            → Compras encontró un problema (ver
                                FacturaObservacion), vuelve al proveedor
        APROBADA_COMPRAS     → Compras aprobó, lista para sincronizar a SAP
        CANCELADO             → anulada; libera el candado de unicidad de OC
                                (única excepción explícita en
                                validar_oc_disponible/saldo_disponible)

    estado_sap sigue el mismo patrón de ciclo ya establecido por
    EntradaMercaderia.estado_sap (sesión 57), con un valor extra 'C'
    (cancelado en SAP) que EntradaMercaderia no necesita porque una
    Entrada de Mercancía nunca se cancela desde el Portal.
    """
    ESTADO_CHOICES = [
        ('BORRADOR', 'Borrador'),
        ('EN_REVISION_COMPRAS', 'En Revisión (Compras)'),
        ('OBSERVADA', 'Observada'),
        ('APROBADA_COMPRAS', 'Aprobada (Compras)'),
        ('CANCELADO', 'Cancelado'),
    ]

    ESTADO_SAP_CHOICES = [
        ('', 'Sin generar'),
        ('L', 'Pendiente de confirmar en SAP'),
        ('B', 'Borrador confirmado en SAP'),
        ('Y', 'Definitivo confirmado en SAP'),
        ('C', 'Cancelado en SAP'),
    ]

    ESTADO_CDR_CHOICES = [
        ('ACEPTADO', 'Aceptado'),
        ('OBSERVADO', 'Observado'),
        ('RECHAZADO', 'Rechazado'),
    ]

    # Catálogo acotado a un solo valor por ahora (confirmado por el
    # usuario) — el resto del catálogo SUNAT se agrega bajo demanda.
    TIPO_OPERACION_CHOICES = [
        ('02', 'Compra Nacional'),
    ]

    # Código 5 = "Otros gastos": confirmado por el usuario en la sesión
    # anterior (siguiente entero libre tras 1-4, la fuente del usuario
    # tenía "Otros gastos" duplicado bajo el código 4 junto con "Gastos
    # de educación" — ya resuelto, no queda como TODO).
    CLASIFICACION_BIENES_SERVICIOS_CHOICES = [
        (1, 'Mercadería'),
        (2, 'Activo'),
        (3, 'Otros activos'),
        (4, 'Gastos de educación'),
        (5, 'Otros gastos'),
    ]

    proveedor = models.ForeignKey(
        SupplierProfile,
        on_delete=models.PROTECT,
        related_name='facturas',
        help_text="Perfil de proveedor dueño de esta factura.",
    )
    sede = models.ForeignKey(
        Sede, on_delete=models.PROTECT, related_name='facturas',
    )
    entrada_mercaderia = models.ForeignKey(
        EntradaMercaderia,
        on_delete=models.PROTECT,
        related_name='facturas',
        null=True, blank=True,
        help_text="Respaldo del 3-way match (OC / Entrada de Mercancía / Factura).",
    )

    estado = models.CharField(max_length=25, choices=ESTADO_CHOICES, default='BORRADOR')
    estado_sap = models.CharField(
        max_length=1, choices=ESTADO_SAP_CHOICES, default='', blank=True,
    )

    doc_entry_preliminar = models.CharField(max_length=50, null=True, blank=True)
    doc_entry_definitivo = models.CharField(max_length=50, null=True, blank=True)

    doc_cur = models.CharField(
        max_length=50, null=True, blank=True,
        help_text="DocCur de SAP (moneda del documento).",
    )
    tax_date = models.DateField(null=True, blank=True)
    doc_due_date = models.DateField(null=True, blank=True)
    num_at_card = models.CharField(
        max_length=100, null=True, blank=True,
        help_text="NumAtCard de SAP (número de documento del proveedor).",
    )
    serie_comprobante = models.CharField(max_length=10, null=True, blank=True)
    numero_comprobante = models.CharField(max_length=20, null=True, blank=True)

    tipo_operacion = models.CharField(
        max_length=2, choices=TIPO_OPERACION_CHOICES, default='02',
        help_text="U_MSSL_TOP (UDF SAP): Tipo de Operación.",
    )
    clasificacion_bienes_servicios = models.IntegerField(
        choices=CLASIFICACION_BIENES_SERVICIOS_CHOICES,
        null=True, blank=True,
        help_text="U_MSSL_CBS (UDF SAP): Clasificación de Bienes/Servicios Adquiridos.",
    )

    tipo_cambio = models.DecimalField(
        max_digits=10, decimal_places=6, null=True, blank=True,
    )

    # Archivos + hash (llenados en Sub-fase 3.2 — sin validación todavía).
    xml_file = models.FileField(upload_to='facturas/xml/%Y/%m/%d/', null=True, blank=True)
    hash_xml = models.CharField(max_length=128, blank=True, default='')
    pdf_file = models.FileField(upload_to='facturas/pdf/%Y/%m/%d/', null=True, blank=True)
    hash_pdf = models.CharField(max_length=128, blank=True, default='')
    cdr_xml_file = models.FileField(upload_to='facturas/cdr/%Y/%m/%d/', null=True, blank=True)
    hash_cdr = models.CharField(max_length=128, blank=True, default='')

    estado_cdr = models.CharField(
        max_length=10, choices=ESTADO_CDR_CHOICES, null=True, blank=True,
        help_text="Resultado de services_validacion.extraer_estado_cdr (Sub-fase 3.2).",
    )
    firma_valida = models.BooleanField(
        null=True, blank=True,
        help_text="Resultado de services_validacion.validar_firma_xml (Sub-fase 3.2).",
    )

    def __str__(self):
        comprobante = f"{self.serie_comprobante}-{self.numero_comprobante}" if self.serie_comprobante else f"#{self.pk}"
        return f"Factura {comprobante} [{self.estado}]"

    class Meta:
        verbose_name = "Factura"
        verbose_name_plural = "Facturas"
        ordering = ['-created_at']


class FacturaObservacion(models.Model):
    """
    Historial de observaciones de Compras sobre una Factura — cada ronda
    de revisión queda registrada por separado (no se sobreescribe un
    único campo de texto), para conservar el historial completo de
    idas y vueltas con el proveedor.
    """
    factura = models.ForeignKey(Factura, on_delete=models.CASCADE, related_name='observaciones')
    autor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='observaciones_factura',
    )
    texto = models.TextField()
    fecha = models.DateTimeField(auto_now_add=True)
    ronda = models.PositiveIntegerField(
        default=1,
        help_text="Número de ronda de observación/corrección de esta Factura.",
    )

    def __str__(self):
        return f"Observación Factura {self.factura_id} (ronda {self.ronda})"

    class Meta:
        verbose_name = "Observación de Factura"
        verbose_name_plural = "Observaciones de Factura"
        ordering = ['fecha']


class FacturaOrdenCompra(models.Model):
    """
    Tabla intermedia explícita Factura↔PurchaseOrder (no un ManyToManyField
    directo) — permite auditar cuándo se vinculó cada OC y sirve como el
    punto de bloqueo (select_for_update) del candado de unicidad descrito
    en el docstring del módulo.

    unique_together evita duplicar la misma OC dentro de UNA factura; NO
    impide que la misma OC aparezca en Facturas DISTINTAS a lo largo del
    tiempo (eso es intencional — una OC puede tener varias entregas/
    facturas parciales) — la regla de "solo una Factura activa a la vez"
    vive en InvoicingService.validar_oc_disponible, no aquí.
    """
    factura = models.ForeignKey(Factura, on_delete=models.CASCADE, related_name='ordenes_compra')
    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.PROTECT, related_name='facturas_oc',
    )

    def __str__(self):
        return f"Factura {self.factura_id} | OC {self.purchase_order.doc_num}"

    class Meta:
        unique_together = ('factura', 'purchase_order')
        verbose_name = "OC de Factura"
        verbose_name_plural = "OCs de Factura"


class FacturaLinea(models.Model):
    """
    Línea de Factura por línea de OC (po_line). cantidad_oc/precio_oc son
    una FOTO tomada al crear la línea (editable=False — no se actualizan
    después aunque la OC cambie en SAP), distinta de cantidad/precio (lo
    que el proveedor confirma/edita). La comparación entre ambos pares se
    recalcula en save() → difiere_de_oc.

    NOTA sobre precio_oc: PurchaseOrderLine (apps.sap_sync) no sincroniza
    ningún precio unitario desde SAP hoy — a diferencia de cantidad_oc
    (que sí puede copiarse de po_line.quantity_sap), precio_oc no tiene
    ninguna fuente automática en el Portal todavía. Por eso esta fase NO
    incluye ninguna lógica de auto-población en save() para estos 2
    campos — quien cree la FacturaLinea (la orquestación de Sub-fase 3.2/
    3.3, todavía no construida) es responsable de pasarlos explícitamente
    al crear el registro. Ambos son obligatorios (sin null=True) porque
    son la fuente de comparación de difiere_de_oc.

    po_line usa on_delete=PROTECT — mismo criterio ya establecido para
    TicketLineInspection/TicketLineCOA/EntradaMercaderiaLinea (sesión 49):
    una línea de OC nunca se borra físicamente, solo se desactiva
    (PurchaseOrderLine.activa=False).
    """
    factura = models.ForeignKey(Factura, on_delete=models.CASCADE, related_name='lineas')
    po_line = models.ForeignKey(
        PurchaseOrderLine, on_delete=models.PROTECT, related_name='facturas_lineas',
    )

    cantidad_oc = models.DecimalField(
        max_digits=18, decimal_places=4, editable=False,
        help_text="Snapshot de la cantidad de OC al crear esta línea. No se actualiza después.",
    )
    precio_oc = models.DecimalField(
        max_digits=18, decimal_places=4, editable=False,
        help_text="Snapshot del precio de OC al crear esta línea. No se actualiza después.",
    )

    cantidad = models.DecimalField(max_digits=18, decimal_places=4)
    precio = models.DecimalField(max_digits=18, decimal_places=4)

    difiere_de_oc = models.BooleanField(
        default=False, editable=False,
        help_text="Calculado en save(): True si cantidad o precio difieren del snapshot de OC.",
    )

    aplica_retencion = models.BooleanField(default=False)
    documento_retencion = models.FileField(
        upload_to='facturas/retenciones/%Y/%m/%d/', null=True, blank=True,
    )

    aplica_detraccion = models.BooleanField(default=False)
    documento_detraccion = models.FileField(
        upload_to='facturas/detracciones/%Y/%m/%d/', null=True, blank=True,
    )

    def save(self, *args, **kwargs):
        self.difiere_de_oc = (
            self.cantidad != self.cantidad_oc or self.precio != self.precio_oc
        )
        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"Factura {self.factura_id} | OC {self.po_line.purchase_order.doc_num} "
            f"L{self.po_line.line_num}"
        )

    class Meta:
        unique_together = ('factura', 'po_line')
        verbose_name = "Línea de Factura"
        verbose_name_plural = "Líneas de Factura"
