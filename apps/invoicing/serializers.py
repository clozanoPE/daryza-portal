# apps/invoicing/serializers.py
"""
Serializers de Factura consumidos por los endpoints del daemon SAP para
la Sub-fase 3.6 (api/factura_api.py) — mismo criterio de ubicación ya
usado por apps.operations.serializers para EntradaMercaderia: el
serializer vive en la app dueña del modelo, no en api/.

3 serializers, uno por endpoint de "lista" (cada uno expone solo lo que
el daemon necesita en esa etapa del ciclo, no el objeto Factura completo):

  FacturaPreliminarSerializer   -> GET facturas-pendientes-preliminar/
                                    Payload completo para que el daemon
                                    arme el documento en SAP B1 (Copy
                                    From GRPO): datos de cabecera
                                    mapeados a los nombres reales de los
                                    UDF de SAP (U_MSSL_FPC/FNC/TOP/CBS,
                                    mismo criterio ya usado en el modelo
                                    para tipo_operacion/clasificacion_
                                    bienes_servicios) + líneas con la
                                    referencia al GRPO (EntradaMercaderia)
                                    correspondiente.

  FacturaReconciliacionSerializer -> GET facturas-preliminares/
                                    Solo lo mínimo para que el daemon
                                    identifique qué Preliminar debe
                                    revisar en SAP (doc_entry_preliminar).

  FacturaCancelacionSerializer  -> GET facturas-pendientes-cancelacion/
                                    Solo lo mínimo para que el daemon
                                    sepa qué Preliminar debe anular en SAP.

DECISIÓN — referencia al GRPO (grpo_doc_entry/grpo_estado_sap), no
filtrado por completitud: en vez de EXCLUIR de facturas-pendientes-
preliminar/ cualquier Factura cuya EntradaMercaderia todavía no esté
confirmada en SAP (estado_sap != 'Y'), se expone el dato tal cual
(potencialmente None/'L'/'B') y se deja que el daemon decida — mismo
principio ya establecido en este proyecto (sesión 57/58: "el daemon es
la fuente de verdad de SAP, no tiene sentido que el Portal le imponga
una secuencia estricta"). Un filtro implícito en el Portal sobre datos
que en realidad dependen del propio SAP sería una fuente de verdad
paralela con riesgo real de quedar desincronizada.

DECISIÓN — base_line = po_line.line_num (mismo supuesto ya aceptado en
EntradaMercaderiaLineaSerializer, apps/operations/serializers.py, sesión
57): SAP preserva el orden de línea al copiar un documento completo
desde su origen (PO -> GRPO -> Factura), así que el LineNum de la OC
sigue sirviendo como índice también en el GRPO. No es un supuesto nuevo
de esta sesión, es el mismo ya usado un escalón antes en el mismo pipeline.
"""
from rest_framework import serializers

from apps.operations.models import EntradaMercaderiaLinea

from .models import Factura, FacturaLinea


def _entrada_linea_de(po_line):
    """
    EntradaMercaderiaLinea real de una PurchaseOrderLine — la fuente del
    GRPO (BaseEntry) contra el cual el daemon debe copiar la Factura.
    `.order_by('-entrada__fecha_generada').first()`: en el diseño actual
    una OC solo puede estar vinculada a un Ticket FINALIZADO a la vez
    (candado de unicidad de Appointment/Ticket ya existente en el resto
    del proyecto), así que en la práctica hay como máximo una — el
    order_by es una defensa, no una ambigüedad real esperada.
    """
    return EntradaMercaderiaLinea.objects.filter(
        po_line=po_line
    ).select_related('entrada').order_by('-entrada__fecha_generada').first()


class FacturaLineaPreliminarSerializer(serializers.ModelSerializer):
    item_code = serializers.CharField(source='po_line.item_code', read_only=True)
    base_line = serializers.IntegerField(source='po_line.line_num', read_only=True)
    grpo_doc_entry = serializers.SerializerMethodField()
    grpo_estado_sap = serializers.SerializerMethodField()

    class Meta:
        model = FacturaLinea
        fields = [
            'item_code', 'base_line', 'cantidad', 'precio',
            'grpo_doc_entry', 'grpo_estado_sap',
        ]

    def get_grpo_doc_entry(self, obj):
        entrada_linea = _entrada_linea_de(obj.po_line)
        return entrada_linea.entrada.doc_entry_definitivo if entrada_linea else None

    def get_grpo_estado_sap(self, obj):
        entrada_linea = _entrada_linea_de(obj.po_line)
        return entrada_linea.entrada.estado_sap if entrada_linea else ''


class FacturaPreliminarSerializer(serializers.ModelSerializer):
    card_code = serializers.CharField(source='proveedor.sap_card_code', read_only=True)
    sede_codigo = serializers.CharField(source='sede.codigo', read_only=True)
    sede_sap_whs_code = serializers.CharField(source='sede.sap_whs_code', read_only=True)
    # UDF de SAP B1 — mismos nombres reales ya documentados en el modelo
    # (Factura.tipo_operacion.help_text / clasificacion_bienes_servicios.
    # help_text) para U_MSSL_TOP/U_MSSL_CBS; U_MSSL_FPC/FNC (Factura -
    # Punto/Comprobante y Factura - Número Comprobante) son los mismos
    # 2 campos de serie/número ya usados en el resto del Portal, solo
    # renombrados al UDF real que el daemon debe escribir en SAP.
    U_MSSL_FPC = serializers.CharField(source='serie_comprobante', read_only=True)
    U_MSSL_FNC = serializers.CharField(source='numero_comprobante', read_only=True)
    U_MSSL_TOP = serializers.CharField(source='tipo_operacion', read_only=True)
    U_MSSL_CBS = serializers.IntegerField(source='clasificacion_bienes_servicios', read_only=True)
    lineas = FacturaLineaPreliminarSerializer(many=True, read_only=True)

    class Meta:
        model = Factura
        fields = [
            'id', 'card_code', 'sede_codigo', 'sede_sap_whs_code',
            'doc_cur', 'tax_date', 'doc_due_date', 'num_at_card',
            'U_MSSL_FPC', 'U_MSSL_FNC', 'U_MSSL_TOP', 'U_MSSL_CBS',
            'tipo_cambio', 'lineas',
        ]


class FacturaReconciliacionSerializer(serializers.ModelSerializer):
    card_code = serializers.CharField(source='proveedor.sap_card_code', read_only=True)

    class Meta:
        model = Factura
        fields = ['id', 'card_code', 'doc_entry_preliminar', 'estado_sap']


class FacturaCancelacionSerializer(serializers.ModelSerializer):
    card_code = serializers.CharField(source='proveedor.sap_card_code', read_only=True)

    class Meta:
        model = Factura
        fields = ['id', 'card_code', 'doc_entry_preliminar', 'estado_sap']
