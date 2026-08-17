# apps/sap_sync/models.py
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

    @property
    def es_materia_prima(self):
        """True si la OC es de tipo Materia Prima (requiere Calidad)."""
        return self.u_mss_tdb == 'MP'

    def __str__(self):
        return f"OC {self.doc_num} - {self.card_name}"


class PurchaseOrderLine(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, related_name='lines', on_delete=models.CASCADE)
    line_num = models.IntegerField(help_text="LineNum de SAP: identificador estable de la línea dentro de la OC, usado como clave de upsert en cada sincronización.")
    item_code = models.CharField(max_length=50)
    description = models.CharField(max_length=255)
    quantity_sap = models.DecimalField(max_digits=18, decimal_places=4)
    und_medida = models.CharField(max_length=50)

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