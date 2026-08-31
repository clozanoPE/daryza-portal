# apps/operations/serializers.py
"""
Serializers de EntradaMercaderia/EntradaMercaderiaLinea, consumidos por el
endpoint del daemon SAP (api/entrada_mercaderia_api.py, sesión 57) — mismo
criterio de ubicación que apps.sap_sync.serializers para PurchaseOrder:
el serializer vive en la app dueña del modelo, no en api/.
"""
from rest_framework import serializers

from apps.operations.models import EntradaMercaderia, EntradaMercaderiaLinea


class EntradaMercaderiaLineaSerializer(serializers.ModelSerializer):
    # BaseType=22 (Purchase Order) / BaseEntry / BaseLine son exactamente lo
    # que SAP B1 necesita para basar una Entrada de Mercancía en la OC
    # original — doc_entry ya es el DocEntry real de SAP (sap_sync,
    # sincronizado por el propio daemon); line_num es el LineNum real de
    # SAP (confirmado estable sesión 49, usado ahí como clave de upsert).
    #
    # Sesión 99: numero_lote / fecha_vencimiento_lote / fecha_fabricacion_
    # lote — el LOTE que Almacén/MP completó en el Portal. El daemon los
    # envía a SAP como BatchNumbers=[{BatchNumber, Quantity, ExpiryDate?,
    # ManufactureDate?}] SOLO si numero_lote no está vacío. Las fechas
    # viajan como 'YYYY-MM-DD' o null (DateField de DRF).
    doc_entry_oc = serializers.IntegerField(source='po_line.purchase_order.doc_entry', read_only=True)
    doc_num_oc = serializers.IntegerField(source='po_line.purchase_order.doc_num', read_only=True)
    base_line = serializers.IntegerField(source='po_line.line_num', read_only=True)
    item_code = serializers.CharField(source='po_line.item_code', read_only=True)

    class Meta:
        model = EntradaMercaderiaLinea
        fields = [
            'doc_entry_oc', 'doc_num_oc', 'base_line', 'item_code', 'cantidad',
            'numero_lote', 'fecha_vencimiento_lote', 'fecha_fabricacion_lote',
        ]


class EntradaMercaderiaSerializer(serializers.ModelSerializer):
    ticket_id = serializers.IntegerField(source='ticket.id', read_only=True)
    card_code = serializers.SerializerMethodField()
    card_name = serializers.SerializerMethodField()
    lineas = EntradaMercaderiaLineaSerializer(many=True, read_only=True)

    class Meta:
        model = EntradaMercaderia
        fields = [
            'id', 'ticket_id', 'card_code', 'card_name', 'estado', 'estado_sap',
            'doc_entry_borrador', 'doc_entry_definitivo', 'doc_num_sap', 'error_mensaje',
            'fecha_generada', 'fecha_borrador_confirmado', 'fecha_definitivo_confirmado',
            'lineas',
        ]

    def _primera_oc(self, obj):
        primera_linea = obj.lineas.first()
        return primera_linea.po_line.purchase_order if primera_linea else None

    def get_card_code(self, obj):
        oc = self._primera_oc(obj)
        return oc.card_code if oc else ''

    def get_card_name(self, obj):
        oc = self._primera_oc(obj)
        return oc.card_name if oc else ''
