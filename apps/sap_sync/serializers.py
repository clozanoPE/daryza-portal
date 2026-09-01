# apps/sap_sync/serializers.py
from rest_framework import serializers
from django.db import transaction
from apps.base.supplier_sync import sincronizar_supplier_profile
from apps.sap_sync.models import PurchaseOrder, PurchaseOrderLine

class PurchaseOrderLineSerializer(serializers.ModelSerializer):
    doc_num = serializers.ReadOnlyField(source='purchase_order.doc_num')
    class Meta:
        model = PurchaseOrderLine
        # requiere_coa se omite del payload SAP (se gestiona internamente)
        # precio_unitario/precio_total_linea/tax_code (sesión 92): datos
        # reales de SAP (PriceBefDi/LineTotal/TaxCode), obligatorios en
        # el payload. HALLAZGO real durante la implementación: como el
        # modelo les da un `default=` (para que la migración no fallara
        # sobre filas ya existentes, ver models.py), DRF los infiere
        # automáticamente como required=False — sin este extra_kwargs
        # explícito, un daemon desactualizado que los omitiera habría
        # recibido 201 igual, con estos 3 campos silenciosamente en su
        # default transitorio (0/'IGV'), no un 400 claro.
        fields = [
            'doc_num', 'line_num', 'item_code', 'description', 'quantity_sap', 'und_medida',
            'precio_unitario', 'precio_total_linea', 'tax_code', 'gestionado_por_lote',
        ]
        extra_kwargs = {
            'precio_unitario': {'required': True},
            'precio_total_linea': {'required': True},
            'tax_code': {'required': True},
            # Sesión 99b: mismo criterio — el modelo le da default=False, así
            # que sin este required=True explícito DRF lo infiere opcional y
            # un daemon desactualizado que lo omitiera pasaría silencioso.
            'gestionado_por_lote': {'required': True},
        }

class PurchaseOrderSerializer(serializers.ModelSerializer):
    lines = PurchaseOrderLineSerializer(many=True)

    class Meta:
        model = PurchaseOrder
        # u_mss_tdb viene del UDF de SAP; requerido para la lógica de ruteo
        # doc_cur (sesión 93): DocCur de SAP, moneda del documento — mismo
        # criterio ya aplicado a precio_unitario/precio_total_linea/
        # tax_code (extra_kwargs abajo): required=True explícito, porque
        # el default='' del modelo haría que DRF lo infiera required=False
        # por su cuenta.
        fields = [
            'doc_entry', 'doc_num', 'card_code', 'card_name', 'e_mail', 'status',
            'u_mss_tdb', 'doc_cur', 'lines',
        ]
        # doc_entry/doc_num son unique=True en el modelo: DRF les agrega
        # automáticamente un UniqueValidator, que rechaza con 400 CUALQUIER
        # POST cuyo doc_entry/doc_num ya exista en BD — antes de que create()
        # llegue a ejecutarse. Esto rompía la idempotencia del endpoint para
        # TODA OC ya sincronizada (no solo las que tienen líneas protegidas):
        # el daemon nunca podía re-enviar una OC existente vía HTTP real sin
        # un 400, pese a que create() ya implementaba update_or_create.
        # La unicidad real la sigue garantizando la constraint de BD (además
        # de update_or_create); no hace falta que el serializer la valide.
        extra_kwargs = {
            'doc_entry': {'validators': []},
            'doc_num': {'validators': []},
            'doc_cur': {'required': True},
        }

    def create(self, validated_data):
        """
        Lógica Senior: Implementa Idempotencia.
        Si la OC ya existe, se actualiza. Si no, se crea.

        Sincronización de líneas por upsert (line_num = LineNum de SAP,
        identificador estable de la línea dentro del documento, confirmado
        contra datos reales: 0-indexado, secuencial, sin repetirse dentro
        de una misma OC). Antes se hacía `lines.all().delete()` +
        `bulk_create()` en cada sync — con PurchaseOrderLine.po_line siendo
        PROTECT desde TicketLineInspection/TicketLineCOA, ese delete()
        lanzaba ProtectedError apenas se resincronizara una OC que ya
        tuviera un Ticket con inspecciones registradas. El upsert por
        línea preserva el id de PurchaseOrderLine (y por lo tanto esas FK)
        y, como efecto colateral correcto, ya no resetea `requiere_coa`
        a False en cada sync (nunca se incluye en `defaults`, así que el
        ajuste manual de Compras sobrevive a la resincronización).
        """
        lines_data = validated_data.pop('lines')
        doc_entry = validated_data.get('doc_entry')

        with transaction.atomic():
            # 1. Upsert de la cabecera (PurchaseOrder)
            purchase_order, _created = PurchaseOrder.objects.update_or_create(
                doc_entry=doc_entry,
                defaults=validated_data
            )

            # 1b. Upsert de SupplierProfile (sesión: SupplierProfile mínimo)
            # — sin endpoint separado todavía, aprovecha que card_code/
            # card_name/e_mail ya vienen en este mismo payload.
            sincronizar_supplier_profile(
                card_code=purchase_order.card_code,
                card_name=purchase_order.card_name,
                e_mail=purchase_order.e_mail,
            )

            # 2. Upsert de líneas, una por una, por (purchase_order, line_num)
            line_nums_recibidos = set()
            for line_data in lines_data:
                line_num = line_data['line_num']
                line_nums_recibidos.add(line_num)
                defaults = {k: v for k, v in line_data.items() if k != 'line_num'}
                defaults['activa'] = True
                PurchaseOrderLine.objects.update_or_create(
                    purchase_order=purchase_order,
                    line_num=line_num,
                    defaults=defaults,
                )

            # 3. Líneas que ya no vienen en el payload (canceladas/eliminadas
            # en SAP): se desactivan, nunca se borran.
            purchase_order.lines.exclude(line_num__in=line_nums_recibidos).update(activa=False)

        return purchase_order