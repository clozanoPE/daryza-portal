# apps/invoicing/services.py
"""
Sub-fase 3.1: servicio de cálculo de saldo por línea de OC + los 2
candados de negocio que el usuario pidió explícitamente que vivieran en
el servicio, no solo en el formulario (mismo principio ya establecido en
OperationsService/AppointmentService en todo el proyecto).

Sub-fase 3.3 (esta sesión): consume services_validacion.py (Sub-fase 3.0)
para poblar los datos reales de la Factura una vez que sus 3 archivos
están completos, y agrega el candado de envío a revisión
(enviar_a_revision) — bloquea el avance a EN_REVISION_COMPRAS si la firma
XML es inválida, el CDR fue RECHAZADO, o el importe del XML no coincide
con el total de las líneas ya cargadas (decisión de negocio confirmada
explícitamente por el usuario: BLOQUEA, no solo marca el flag).

Todavía sin orquestación de creación de Factura/"Copiar de OC(s)" ni el
flujo real de revisión Compras — eso es Sub-fase 3.4/3.5.

Todos los métodos asumen que ya corren dentro de un transaction.atomic()
abierto por el llamador (mismo criterio que el resto de servicios de la
app: el atomic() lo abre el método de más alto nivel, no cada función de
validación por separado) — select_for_update() sin una transacción activa
alrededor no bloquea nada.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import F, Sum

from apps.operations.models import EntradaMercaderiaLinea
from apps.sap_sync.models import PurchaseOrder

from . import services_validacion as sv
from .models import Factura, FacturaLinea

# Tolerancia de redondeo al comparar importe_total_xml (2 decimales
# reales en cualquier comprobante peruano) contra la suma calculada de
# FacturaLinea (4 decimales de precisión interna) — evita falsos
# positivos por diferencias de centavos que no son un error de negocio.
TOLERANCIA_IMPORTE = Decimal('0.02')


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

        BUG REAL corregido (reportado y verificado empíricamente antes de
        arreglar, no solo sospechado — 2 hilos con conexiones/transacciones
        separadas contra Postgres real, uno manteniendo la transacción
        abierta antes de comitear): la versión anterior solo aplicaba
        select_for_update() sobre `Factura.objects.filter(ordenes_compra__
        purchase_order=...)` — un SELECT ... FOR UPDATE sobre un queryset
        VACÍO no bloquea nada (no hay fila que lockear). Esto es exactamente
        el caso que más importa: 2 requests intentando crear la PRIMERA
        Factura para la misma OC casi al mismo tiempo — ninguna Factura
        existe todavía para ninguno de los 2, así que ambos pasaban la
        validación sin esperarse entre sí. Confirmado con el escenario real:
        el segundo hilo pasó su validación en 46ms mientras el primero
        seguía con la transacción abierta (sin comitear su Factura nueva).

        FIX: se bloquea primero la fila de `PurchaseOrder` misma —
        `SELECT ... FOR UPDATE` sobre `PurchaseOrder.objects.get(pk=...)`,
        una fila que SIEMPRE existe (se recibe como parámetro ya
        persistido), a diferencia de la Factura que todavía no existe la
        primera vez. Esto serializa a cualquier llamador concurrente sobre
        la MISMA OC en este punto: el primero que llega retiene el lock
        hasta comitear/revertir; el segundo queda bloqueado en esta misma
        línea hasta que el primero termine, y solo entonces re-lee el
        estado real (ya con la Factura del primero, si se creó). Re-
        verificado con el mismo escenario de 2 hilos tras el fix: el
        segundo hilo ahora espera ~1s (el tiempo que el primero mantuvo la
        transacción abierta) y correctamente es rechazado.
        """
        PurchaseOrder.objects.select_for_update().get(pk=purchase_order.pk)

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

    @staticmethod
    def notificar_factura_observada(factura, texto_observacion: str):
        """
        Envía al proveedor el aviso de que Compras observó su Factura.

        Servicio ya listo para que la Sub-fase 3.5 (el flujo real de
        observación — endpoint/vista que crea el FacturaObservacion y
        cambia Factura.estado a OBSERVADA, todavía sin construir) lo
        consuma sin duplicar esta lógica; esta función no dispara nada
        por sí sola — nadie la llama todavía desde ningún endpoint.

        Destinatario: el email de la cuenta de Portal si ya existe
        (SupplierProfile.user), si no el correo_electronico del propio
        SupplierProfile (el que llega del sync de OC, ver
        apps/base/supplier_sync.py) — el proveedor puede no tener cuenta
        de Portal activada todavía (SupplierProfile.user es nullable,
        sesión anterior) y aun así necesitar este aviso.

        Nunca lanza — mismo criterio que enviar_correo: el resultado
        (éxito/error) se devuelve para que el llamador decida dónde
        dejarlo visible.
        """
        from apps.base.services_correo import enviar_correo

        proveedor = factura.proveedor
        destinatario = ''
        if proveedor.user_id and proveedor.user.email:
            destinatario = proveedor.user.email
        elif proveedor.correo_electronico:
            destinatario = proveedor.correo_electronico

        comprobante = (
            f"{factura.serie_comprobante}-{factura.numero_comprobante}"
            if factura.serie_comprobante else f"#{factura.pk}"
        )
        cuerpo_html = (
            f"<p>Su factura <strong>{comprobante}</strong> fue observada por Compras "
            f"y requiere corrección.</p>"
            f"<p><strong>Motivo:</strong></p>"
            f"<p>{texto_observacion}</p>"
            f"<p>Ingrese al Portal para corregirla y volver a enviarla.</p>"
        )
        return enviar_correo(
            destinatario=destinatario,
            asunto='[Daryza VBS] Factura observada — corrección requerida',
            cuerpo_html=cuerpo_html,
        )

    @staticmethod
    def _partir_serie_numero(numero_completo: str):
        """
        DatosFactura.numero llega como un único string ('F001-123', ver
        cbc:ID de la fixture real de la Sub-fase 3.0) — Factura.
        serie_comprobante/numero_comprobante son 2 campos separados
        (max_length 10/20). Se parte por el primer '-'; si no hay
        ninguno, se guarda todo como numero_comprobante y serie vacía en
        vez de fallar (un XML con un formato de numeración distinto al
        esperado no debería romper la carga del archivo).
        """
        if not numero_completo:
            return '', ''
        if '-' in numero_completo:
            serie, numero = numero_completo.split('-', 1)
            return serie[:10], numero[:20]
        return '', numero_completo[:20]

    @staticmethod
    def procesar_validacion_documentos(factura):
        """
        Sub-fase 3.3: dispara la validación de negocio sobre los 3
        archivos de una Factura (xml_file/pdf_file/cdr_xml_file) una vez
        que los 3 están presentes — llamado automáticamente desde
        services_archivos.cargar_archivo_factura, sin importar cuál de
        los 3 archivos fue el que completó el set.

        Descarga el contenido real de XML y CDR desde OneDrive
        (services_archivos.descargar_archivo_factura — reconstruye la
        ruta determinística, no depende del webUrl de solo-lectura
        guardado en el campo) y puebla:

            firma_valida                 <- validar_firma_xml(xml)
            serie_comprobante/
            numero_comprobante/
            doc_cur/
            importe_total_xml/
            moneda_xml                   <- extraer_datos_factura(xml)
            estado_cdr                   <- extraer_estado_cdr(cdr)
            importe_no_coincide          <- importe_total_xml vs. suma
                                             de FacturaLinea.cantidad*
                                             precio ya cargadas (solo si
                                             la Factura ya tiene líneas;
                                             si todavía no tiene ninguna
                                             —normal en BORRADOR antes de
                                             la Sub-fase 3.4— no hay nada
                                             que comparar, se deja en
                                             False sin marcar)
            mensaje_validacion_documentos <- resumen legible de las 3
                                             validaciones, para que la
                                             razón de un bloqueo (o de
                                             que todo esté OK) no quede
                                             oculta en 3 campos sueltos

        No-op silencioso si todavía falta alguno de los 3 archivos —
        permite invocarla también de forma defensiva/idempotente desde
        cualquier punto que quiera forzar un recálculo, no solo desde el
        trigger automático de la 3ª carga.

        No captura errores de descarga/parseo — un archivo que ya pasó
        `services_archivos._validar_contenido_real` (XML bien formado /
        PDF con estructura válida) no debería fallar aquí salvo por un
        problema real de red/Graph al día siguiente de haberse subido;
        se prefiere que ese caso se vea como un error explícito en el
        request que completó la 3ª carga, en vez de guardar un estado
        a medias sin que nadie se entere.
        """
        from . import services_archivos as sa

        if not (factura.xml_file and factura.pdf_file and factura.cdr_xml_file):
            return

        xml_bytes = sa.descargar_archivo_factura(factura, 'xml')
        cdr_bytes = sa.descargar_archivo_factura(factura, 'cdr')

        resultado_firma = sv.validar_firma_xml(xml_bytes)
        datos_factura = sv.extraer_datos_factura(xml_bytes)
        estado_cdr = sv.extraer_estado_cdr(cdr_bytes)

        serie, numero = InvoicingService._partir_serie_numero(datos_factura.numero)

        mensajes = []
        if resultado_firma.valido:
            mensajes.append("Firma XML: válida.")
        else:
            mensajes.append(f"Firma XML: INVÁLIDA — {resultado_firma.error}")

        mensajes.append(
            f"CDR SUNAT: {estado_cdr.estado} (código {estado_cdr.response_code}) — {estado_cdr.descripcion}"
        )

        importe_no_coincide = False
        if datos_factura.importe_total is not None:
            total_lineas = FacturaLinea.objects.filter(factura=factura).aggregate(
                total=Sum(F('cantidad') * F('precio'))
            )['total']
            if total_lineas is not None:
                diferencia = abs(datos_factura.importe_total - total_lineas)
                importe_no_coincide = diferencia > TOLERANCIA_IMPORTE
                if importe_no_coincide:
                    mensajes.append(
                        f"Importe: NO COINCIDE — XML declara {datos_factura.importe_total} "
                        f"{datos_factura.moneda}, suma de líneas = {total_lineas}."
                    )
                else:
                    mensajes.append("Importe: coincide con la suma de líneas.")
            else:
                mensajes.append("Importe: sin líneas cargadas todavía, no se comparó.")
        else:
            mensajes.append("Importe: el XML no trae PayableAmount, no se comparó.")

        factura.firma_valida = resultado_firma.valido
        factura.serie_comprobante = serie
        factura.numero_comprobante = numero
        factura.doc_cur = datos_factura.moneda or factura.doc_cur
        factura.importe_total_xml = datos_factura.importe_total
        factura.moneda_xml = datos_factura.moneda
        factura.estado_cdr = estado_cdr.estado
        factura.importe_no_coincide = importe_no_coincide
        factura.mensaje_validacion_documentos = '\n'.join(mensajes)
        factura.save(update_fields=[
            'firma_valida', 'serie_comprobante', 'numero_comprobante', 'doc_cur',
            'importe_total_xml', 'moneda_xml', 'estado_cdr', 'importe_no_coincide',
            'mensaje_validacion_documentos', 'updated_at',
        ])
        return factura

    @staticmethod
    def enviar_a_revision(factura):
        """
        Candado de servicio (pedido explícito, punto 2/3): una Factura no
        puede avanzar a EN_REVISION_COMPRAS si firma_valida=False,
        estado_cdr=RECHAZADO, o importe_no_coincide=True — con un mensaje
        claro indicando cuál de las validaciones falló (pueden fallar
        varias a la vez, se listan todas, no solo la primera).

        Sin endpoint que la llame todavía (Sub-fase 3.4/3.5, no
        construida en esta sesión) — mismo criterio ya establecido para
        notificar_factura_observada: el servicio queda listo para que el
        flujo real lo consuma sin duplicar esta lógica.
        """
        if factura.estado not in ('BORRADOR', 'OBSERVADA'):
            raise ValidationError(
                f"No se puede enviar a revisión una Factura en estado "
                f"'{factura.get_estado_display()}'."
            )

        errores = []
        if factura.firma_valida is not True:
            errores.append(
                "la firma digital del XML no es válida (o todavía no se completaron/"
                "validaron los 3 archivos)."
            )
        if factura.estado_cdr == 'RECHAZADO':
            errores.append(
                f"el CDR de SUNAT indica RECHAZADO: {factura.mensaje_validacion_documentos or ''}"
            )
        if factura.importe_no_coincide:
            errores.append(
                "el importe del XML no coincide con el total calculado de las líneas de la Factura."
            )

        if errores:
            raise ValidationError(
                "No se puede enviar la Factura a revisión — " + " ".join(errores)
            )

        factura.estado = 'EN_REVISION_COMPRAS'
        factura.save(update_fields=['estado', 'updated_at'])
        return factura
