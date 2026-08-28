# apps/invoicing/services.py
"""
Sub-fase 3.1: servicio de cálculo de saldo por línea de OC + los 2
candados de negocio que el usuario pidió explícitamente que vivieran en
el servicio, no solo en el formulario (mismo principio ya establecido en
OperationsService/AppointmentService en todo el proyecto).

Sub-fase 3.3: consume services_validacion.py (Sub-fase 3.0) para poblar
los datos reales de la Factura una vez que sus 3 archivos están
completos, y agrega el candado de envío a revisión (enviar_a_revision) —
bloquea el avance a EN_REVISION_COMPRAS si la firma XML es inválida, el
CDR fue RECHAZADO, o el importe del XML no coincide con el total de las
líneas ya cargadas (decisión de negocio confirmada explícitamente por el
usuario: BLOQUEA, no solo marca el flag).

Sub-fase 3.5 (esta sesión): el lado de Compras — aprobar_factura /
observar_factura. Ambas reutilizan _errores_validacion_documentos (el
mismo cuerpo de reglas que ya usaba enviar_a_revision, extraído a un
helper compartido) — pedido explícito de "defensa en profundidad, no
confíes en que el estado anterior ya lo garantizó": aprobar_factura
vuelve a evaluar firma_valida/estado_cdr/importe_no_coincide contra el
estado REAL de la Factura (refresh_from_db() al entrar), no contra lo que
enviar_a_revision ya validó en su momento — nada impide, en teoría, que
esos campos cambien entre el envío a revisión y la aprobación (un
recálculo forzado, un dato corregido a mano). Candado de rol
(_validar_permiso_compras, reutiliza apps.base.decorators.en_grupo, la
misma función que ya usan los decoradores de vista) vive en el servicio,
no solo en la vista — igual criterio ya establecido en todo el proyecto
para OperationsService (ver CLAUDE.md, sesión 5: grupo_requerido_por_etapa).

observar_factura incrementa la ronda de FacturaObservacion tomando el
máximo ya existente + 1 en cada llamada — así "la próxima observación, si
la hay" queda numerada correctamente sin necesitar ningún contador
separado en Factura: si la Factura se observa, se corrige, se reenvía
(sin crear ninguna fila nueva) y se observa de nuevo, el máximo ya
reflejará la ronda anterior.

Todos los métodos asumen que ya corren dentro de un transaction.atomic()
abierto por el llamador (mismo criterio que el resto de servicios de la
app: el atomic() lo abre el método de más alto nivel, no cada función de
validación por separado) — select_for_update() sin una transacción activa
alrededor no bloquea nada.
"""
import hashlib
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max

from apps.base.decorators import en_grupo
from apps.operations.models import EntradaMercaderiaLinea
from apps.sap_sync.models import PurchaseOrder, PurchaseOrderLine

from . import services_validacion as sv
from .models import Factura, FacturaLinea

# Tolerancia de redondeo al comparar importe_total_xml (2 decimales
# reales en cualquier comprobante peruano) contra la suma calculada de
# FacturaLinea (4 decimales de precisión interna) — evita falsos
# positivos por diferencias de centavos que no son un error de negocio.
TOLERANCIA_IMPORTE = Decimal('0.02')

# Tasa de IGV vigente en Perú desde 2011 (18%) — sin catálogo/
# configuración propia todavía: es un valor estable y de aplicación
# nacional, no específico de esta instalación de SAP (a diferencia de
# TaxCode, que sí varía por línea vía PurchaseOrderLine.tax_code). Si
# alguna vez cambiara o se necesitara por-línea, es el punto a
# extender — hoy es una tasa única aplicada solo a líneas 'IGV'
# (gravadas); las 'IGV_EXE' (exoneradas) no suman nada acá.
TASA_IGV = Decimal('0.18')


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
            importe_no_coincide          <- importe_total_xml vs. suma de
                                             FacturaLinea.cantidad*precio
                                             (base NETA, sin IGV) MÁS el
                                             IGV calculado línea por
                                             línea según tax_code (sesión
                                             92 — antes comparaba directo
                                             contra la base neta, sin
                                             sumar impuesto, pese a que
                                             el XML real de una factura
                                             trae el importe CON IGV
                                             incluido; ver CLAUDE.md,
                                             sesión 92, para el detalle
                                             de por qué era inconsistente
                                             y cómo se corrigió). Solo si
                                             la Factura ya tiene líneas;
                                             si todavía no tiene ninguna
                                             —normal en BORRADOR antes de
                                             la Sub-fase 3.4— no hay nada
                                             que comparar, se deja en
                                             False sin marcar
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

        Verificación de integridad (pedida explícitamente antes de
        producción): `descargar_documento_factura` reconstruye la ruta
        de OneDrive de forma determinística a partir de sede/ruc/pk —
        nunca depende del nombre real del archivo subido (el nombre
        siempre es `{tipo}.{extension}`, fijo, así que un "archivo
        re-cargado con otro nombre" no puede ocurrir a través de este
        servicio). El riesgo real y sí alcanzable es que la carpeta ya
        no exista donde se espera (p. ej. si `factura.sede` o
        `proveedor.ruc` cambiaran entre la carga y esta descarga) — eso
        ya falla fuerte y explícito (`requests.raise_for_status()` →
        `HTTPError`, propagado sin capturar, ver arriba), nunca trae un
        archivo vacío/incorrecto en silencio. Como defensa adicional, se
        compara el hash SHA-256 de lo descargado contra el hash que se
        guardó al cargar el archivo (`Factura.hash_xml`/`hash_cdr`) —
        cualquier desajuste (contenido distinto al que se validó y
        aceptó en su momento) aborta antes de procesar nada.
        """
        from . import services_archivos as sa

        if not (factura.xml_file and factura.pdf_file and factura.cdr_xml_file):
            return

        xml_bytes = sa.descargar_archivo_factura(factura, 'xml')
        cdr_bytes = sa.descargar_archivo_factura(factura, 'cdr')

        if hashlib.sha256(xml_bytes).hexdigest() != factura.hash_xml:
            raise ValidationError(
                "El contenido del XML descargado de OneDrive no coincide con el hash "
                "guardado al cargarlo — posible archivo incorrecto o corrupto. Abortando "
                "la validación de negocio sin modificar la Factura."
            )
        if hashlib.sha256(cdr_bytes).hexdigest() != factura.hash_cdr:
            raise ValidationError(
                "El contenido del CDR descargado de OneDrive no coincide con el hash "
                "guardado al cargarlo — posible archivo incorrecto o corrupto. Abortando "
                "la validación de negocio sin modificar la Factura."
            )

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
            # Sesión 92: se lee tax_code por línea (no un Sum() agregado
            # con Case/When en SQL) porque el volumen de líneas por
            # Factura es bajo y la lógica en Python es mucho más clara
            # de leer/testear que una agregación condicional compleja.
            lineas = list(
                FacturaLinea.objects.filter(factura=factura).only('cantidad', 'precio', 'tax_code')
            )
            if lineas:
                total_neto = sum((linea.cantidad * linea.precio for linea in lineas), Decimal('0'))
                total_igv = sum(
                    (
                        linea.cantidad * linea.precio * TASA_IGV
                        for linea in lineas
                        if linea.tax_code == PurchaseOrderLine.TAX_CODE_IGV
                    ),
                    Decimal('0'),
                )
                total_calculado = total_neto + total_igv
                diferencia = abs(datos_factura.importe_total - total_calculado)
                importe_no_coincide = diferencia > TOLERANCIA_IMPORTE
                if importe_no_coincide:
                    mensajes.append(
                        f"Importe: NO COINCIDE — XML declara {datos_factura.importe_total} "
                        f"{datos_factura.moneda}, calculado = {total_calculado} "
                        f"(líneas netas {total_neto} + IGV {total_igv})."
                    )
                else:
                    mensajes.append(
                        f"Importe: coincide (líneas netas {total_neto} + IGV {total_igv} "
                        f"= {total_calculado})."
                    )
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
    def _errores_validacion_documentos(factura) -> list:
        """
        Cuerpo de reglas compartido por enviar_a_revision (el proveedor no
        puede avanzar a EN_REVISION_COMPRAS) y aprobar_factura (Compras no
        puede aprobar) — extraído a un único lugar para que ambos candados
        evalúen exactamente la misma regla de negocio, nunca 2 copias que
        puedan desalinearse.
        """
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
        return errores

    @staticmethod
    def enviar_a_revision(factura):
        """
        Candado de servicio (pedido explícito, punto 2/3): una Factura no
        puede avanzar a EN_REVISION_COMPRAS si firma_valida=False,
        estado_cdr=RECHAZADO, o importe_no_coincide=True — con un mensaje
        claro indicando cuál de las validaciones falló (pueden fallar
        varias a la vez, se listan todas, no solo la primera).
        """
        if factura.estado not in ('BORRADOR', 'OBSERVADA'):
            raise ValidationError(
                f"No se puede enviar a revisión una Factura en estado "
                f"'{factura.get_estado_display()}'."
            )

        errores = InvoicingService._errores_validacion_documentos(factura)
        if errores:
            raise ValidationError(
                "No se puede enviar la Factura a revisión — " + " ".join(errores)
            )

        factura.estado = 'EN_REVISION_COMPRAS'
        factura.save(update_fields=['estado', 'updated_at'])
        return factura

    # ═══════════════════════════════════════════════════════════════════
    # Sub-fase 3.5 — lado de Compras: aprobar / observar
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _validar_permiso_compras(usuario):
        """
        Candado de rol en el servicio (pedido explícito, punto 3: "no solo
        en la vista"). Reutiliza en_grupo (apps/base/decorators.py) — el
        mismo predicado que ya arma compras_required — en vez de
        reimplementar la comprobación de grupo/superusuario en un segundo
        lugar.
        """
        if not en_grupo('COMPRAS')(usuario):
            raise ValidationError(
                "Solo una cuenta del grupo COMPRAS (o superusuario) puede ejecutar esta acción."
            )

    @staticmethod
    def aprobar_factura(factura, usuario):
        """
        Aprueba una Factura EN_REVISION_COMPRAS: pasa a APROBADA_COMPRAS y
        marca estado_sap='L' (queda lista para que el demonio SAP la tome
        en la Sub-fase 3.6, mismo patrón ya usado por EntradaMercaderia
        desde la sesión 57).

        DEFENSA EN PROFUNDIDAD (pedido explícito, punto 3): antes de
        evaluar nada, refresh_from_db() — nunca confía en que el objeto
        `factura` que recibió el llamador siga reflejando el estado real
        en BD (podría venir de una consulta anterior en la misma request,
        o de un test que mutó campos en memoria sin guardar). Las 3
        validaciones de _errores_validacion_documentos (firma/CDR/
        importe) son las MISMAS que ya bloquean enviar_a_revision — no se
        confía en que ese paso anterior ya las haya garantizado; se
        vuelven a evaluar aquí, contra el estado ya refrescado.
        """
        InvoicingService._validar_permiso_compras(usuario)

        factura.refresh_from_db()
        if factura.estado != 'EN_REVISION_COMPRAS':
            raise ValidationError(
                f"No se puede aprobar una Factura en estado '{factura.get_estado_display()}' "
                "— solo mientras está 'En Revisión (Compras)'."
            )

        errores = InvoicingService._errores_validacion_documentos(factura)
        if errores:
            raise ValidationError(
                "No se puede aprobar la Factura — " + " ".join(errores)
            )

        factura.estado = 'APROBADA_COMPRAS'
        factura.estado_sap = 'L'
        factura.save(update_fields=['estado', 'estado_sap', 'updated_at'])
        return factura

    @staticmethod
    @transaction.atomic
    def _crear_observacion_y_marcar(factura, usuario, texto):
        """Parte transaccional (2 escrituras: FacturaObservacion + Factura) de observar_factura."""
        from .models import FacturaObservacion

        ultima_ronda = factura.observaciones.aggregate(m=Max('ronda'))['m'] or 0
        FacturaObservacion.objects.create(
            factura=factura, autor=usuario, texto=texto, ronda=ultima_ronda + 1,
        )

        factura.estado = 'OBSERVADA'
        factura.save(update_fields=['estado', 'updated_at'])
        return factura

    @staticmethod
    def observar_factura(factura, usuario, texto_observacion: str):
        """
        Observa una Factura EN_REVISION_COMPRAS: exige un texto no vacío
        (mismo candado ya aplicado al motivo de rechazo de citas, sesión
        20 — aquí es texto libre, no un choice cerrado, pero la misma
        idea de "obligatorio, nunca en silencio"), crea una
        FacturaObservacion NUEVA (nunca sobreescribe una ronda anterior —
        ver docstring del módulo sobre cómo se numera la ronda), cambia
        el estado a OBSERVADA, y dispara la notificación por correo real
        (notificar_factura_observada, sesión anterior) — FUERA de la
        transacción de escritura (llamada de red real a Graph, mismo
        criterio ya aplicado en toda la app: no retener ninguna
        transacción abierta durante una llamada externa).

        El resultado del correo NUNCA bloquea la operación (mismo
        criterio que notificar_factura_observada/enviar_correo) — si
        falla, se deja registrado en un atributo transitorio
        `factura._email_observacion_error` (no persistido, no es un
        campo del modelo) para que el llamador (la vista) decida si lo
        expone en la respuesta — mismo patrón ya usado por
        Ticket.email_notificacion_error (apps.appointments.services,
        sesión 73).
        """
        InvoicingService._validar_permiso_compras(usuario)

        factura.refresh_from_db()
        if factura.estado != 'EN_REVISION_COMPRAS':
            raise ValidationError(
                f"No se puede observar una Factura en estado '{factura.get_estado_display()}' "
                "— solo mientras está 'En Revisión (Compras)'."
            )

        texto = (texto_observacion or '').strip()
        if not texto:
            raise ValidationError("Debe ingresar un texto de observación.")

        factura = InvoicingService._crear_observacion_y_marcar(factura, usuario, texto)

        resultado_correo = InvoicingService.notificar_factura_observada(factura, texto)
        factura._email_observacion_error = None if resultado_correo.enviado else resultado_correo.error
        return factura
