# apps/sap_sync/models.py
from decimal import Decimal

from django.db import models
from apps.base.models import TimeStampedModel

class PurchaseOrder(TimeStampedModel):
    doc_entry = models.IntegerField(unique=True, help_text="ID interno SAP")
    doc_num = models.IntegerField(unique=True, help_text="Número de documento SAP")
    card_code = models.CharField(max_length=50)
    card_name = models.CharField(max_length=255)
    e_mail = models.CharField(max_length=255)
    status = models.CharField(max_length=20, default='O')

    # UDF de SAP que identifica el tipo de documento.
    # 'MP' = Materia Prima → requiere inspección de Calidad y COA por línea
    # Cualquier otro valor → Comercial (flujo directo a Almacén, sin Calidad)
    u_mss_tdb = models.CharField(
        max_length=10,
        blank=True,
        default='',
        help_text="UDF SAP: 'MP' para Materia Prima, vacío/otro para Comercial."
    )

    # Sesión 93: DocCur de SAP — confirmado por el usuario que trae texto
    # directo ('USD', 'SOL'), sin catálogo/mapeo intermedio necesario.
    # blank=True/default='' (no choices) porque no hay una lista cerrada
    # de valores posibles confirmada; una OC vieja sin este dato queda en
    # '' hasta el próximo resync (mismo criterio ya usado para 'activa'
    # y los campos de precio/IGV, sesiones 49/92).
    doc_cur = models.CharField(
        max_length=5, blank=True, default='',
        help_text="DocCur de SAP: moneda del documento (texto directo, ej. 'USD'/'SOL').",
    )

    @property
    def es_materia_prima(self):
        """True si la OC es de tipo Materia Prima (requiere Calidad)."""
        return self.u_mss_tdb == 'MP'

    def __str__(self):
        return f"OC {self.doc_num} - {self.card_name}"


class PurchaseOrderLine(models.Model):
    # Catálogo de impuesto confirmado por el usuario (sesión 92): solo 2
    # estados en el SAP real de Daryza, no los 3 típicos de SUNAT
    # (Gravado/Exonerado/Inafecto) — 'IGV' cubre gravado, 'IGV_EXE' cubre
    # exonerado. Si en el futuro aparece un 3er estado real en SAP, se
    # agrega acá sin romper nada (choices, no un booleano).
    TAX_CODE_IGV = 'IGV'
    TAX_CODE_IGV_EXE = 'IGV_EXE'
    TAX_CODE_CHOICES = [
        (TAX_CODE_IGV, 'IGV (gravado)'),
        (TAX_CODE_IGV_EXE, 'IGV Exonerado'),
    ]

    purchase_order = models.ForeignKey(PurchaseOrder, related_name='lines', on_delete=models.CASCADE)
    line_num = models.IntegerField(help_text="LineNum de SAP: identificador estable de la línea dentro de la OC, usado como clave de upsert en cada sincronización.")
    item_code = models.CharField(max_length=50)
    description = models.CharField(max_length=255)
    quantity_sap = models.DecimalField(max_digits=18, decimal_places=4)
    und_medida = models.CharField(max_length=50)

    # Sesión 92: datos de precio/impuesto que ya existen en SAP al momento
    # de aceptar la OC — no calculados ni inventados por el demonio.
    # Defaults transitorios (0 / IGV) solo para que la migración no falle
    # sobre filas ya existentes (sincronizadas antes de este campo) — el
    # próximo resync real desde SAP los refresca de inmediato, mismo
    # criterio ya usado para "activa" cuando se agregó (sesión 49).
    precio_unitario = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal('0'),
        help_text="PriceBefDi de SAP: precio unitario, antes de descuento.",
    )
    precio_total_linea = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal('0'),
        help_text=(
            "LineTotal de SAP: total NETO de la línea (cantidad × precio "
            "unitario, después de descuento de línea si aplica) — SIN "
            "IGV incluido. No confundir con un total final para el "
            "proveedor, que sí incluiría el impuesto."
        ),
    )
    tax_code = models.CharField(
        max_length=10, choices=TAX_CODE_CHOICES, default=TAX_CODE_IGV,
        help_text="TaxCode de SAP: código de impuesto aplicado a la línea.",
    )

    # Granularidad de inspección a nivel de línea.
    # Pre-marcado True automáticamente si OC padre tiene u_mss_tdb == 'MP'.
    # Compras puede ajustarlo manualmente al validar la cita
    # (ej. desmarcar líneas de envases que no necesitan COA).
    requiere_coa = models.BooleanField(
        default=False,
        help_text=(
            "True → esta línea necesita inspección de Calidad y COA. "
            "Se pre-marca automáticamente para OCs MP; ajustable por Compras."
        )
    )

    # False cuando SAP deja de enviar esta línea en una resincronización
    # (cancelada/eliminada en el documento origen). Nunca se borra la fila:
    # TicketLineInspection/TicketLineCOA apuntan a ella con on_delete=PROTECT,
    # así que un DELETE real rompería esas FK en cuanto la línea ya tuviera
    # inspecciones registradas. Ver PurchaseOrderSerializer.create.
    activa = models.BooleanField(default=True)

    class Meta:
        unique_together = ('purchase_order', 'line_num')

    def __str__(self):
        return f"OC {self.purchase_order.doc_num} - Línea {self.line_num} ({self.item_code})" 