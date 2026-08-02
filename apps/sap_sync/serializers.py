# apps/sap_sync/serializers.py
from rest_framework import serializers
from django.db import transaction
from apps.sap_sync.models import PurchaseOrder, PurchaseOrderLine

class PurchaseOrderLineSerializer(serializers.ModelSerializer):
    doc_num = serializers.ReadOnlyField(source='purchase_order.doc_num')
    class Meta:
        model = PurchaseOrderLine
        # requiere_coa se omite del payload SAP (se gestiona internamente)
        fields = ['doc_num','line_num', 'item_code', 'description', 'quantity_sap', 'und_medida']

class PurchaseOrderSerializer(serializers.ModelSerializer):
    lines = PurchaseOrderLineSerializer(many=True)

    class Meta:
        model = PurchaseOrder
        # u_mss_tdb viene del UDF de SAP; requerido para la lógica de ruteo
        fields = ['doc_entry', 'doc_num', 'card_code', 'card_name', 'e_mail', 'status', 'u_mss_tdb', 'lines']

    def create(self, validated_data):
        """
        Lógica Senior: Implementa Idempotencia.
        Si la OC ya existe, se actualiza. Si no, se crea.
        """
        lines_data = validated_data.pop('lines')
        doc_entry = validated_data.get('doc_entry')

        with transaction.atomic():
            # 1. Upsert de la cabecera (PurchaseOrder)
            purchase_order, created = PurchaseOrder.objects.update_or_create(
                doc_entry=doc_entry,
                defaults=validated_data
            )

            # 2. Sincronización de Líneas
            # Eliminamos las líneas existentes para asegurar que coincidan 1:1 con SAP
            if not created:
                purchase_order.lines.all().delete()

            # 3. Creación de nuevas líneas
            line_objects = [
                PurchaseOrderLine(purchase_order=purchase_order, **line_data)
                for line_data in lines_data
            ]
            PurchaseOrderLine.objects.bulk_create(line_objects)

        return purchase_order