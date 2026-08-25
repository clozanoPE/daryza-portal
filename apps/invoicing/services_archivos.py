# apps/invoicing/services_archivos.py
"""
Carga segura de archivos para Factura/FacturaLinea. Reutiliza
services_validacion.py (Sub-fase 3.0, sesión 59) para el parseo seguro
de XML (protección XXE ya validada ahí) sin duplicar esa lógica, y
OneDriveClient (apps/base/utils.py) para el almacenamiento — mismo
patrón ya usado para los COA de Ticket.

DECISIÓN — tipo de contenido real sin python-magic: libmagic no está
instalado (ni localmente ni en el entorno de despliegue) y agregarlo
introduciría una dependencia de SISTEMA nueva (mismo tipo de riesgo ya
documentado para xmlsec/lxml en la sesión 59 — un paquete Nix/Debian
adicional a coordinar con el builder de Railway). Para los 2 formatos
que este servicio acepta, hay una verificación de contenido real MÁS
estricta que una firma MIME genérica, sin ninguna librería nueva:
  - XML/CDR: debe parsear como XML bien formado con el parser seguro ya
    validado (services_validacion._parse_xml_seguro — protección XXE
    incluida). Un .exe renombrado a .xml no es XML válido en absoluto,
    así que esto ya lo rechaza — cubre el caso adversarial pedido sin
    necesitar libmagic.
  - PDF: 2 pasos. (1) firma de archivo — los primeros bytes deben ser
    literalmente b'%PDF-'; (2) estructura completa — se abre con pypdf
    (puro Python, sin dependencia de sistema) y se recorre el árbol de
    páginas. Un archivo con la firma correcta pero corrupto/truncado
    (pasaba el paso 1 solo) ahora se rechaza también — cierre de la
    Sub-fase 3.2. pypdf sí se agregó a requirements.txt (a diferencia de
    libmagic, es una librería puro-Python, sin el mismo riesgo de
    despliegue).

DECISIÓN — la validación con pypdf aplica a los 3 slots de PDF (Factura.
pdf_file y también FacturaLinea.documento_retencion/documento_detraccion),
no solo a Factura.pdf_file como se nombró explícitamente en el pedido:
los 3 son "un PDF cargado" con el mismo riesgo de corrupción/truncamiento,
y la función que valida contenido (_validar_contenido_real) ya es
compartida por los 5 tipos — limitar la verificación estructural a un
único slot habría exigido un parámetro nuevo solo para dejar 2 de los 3
PDF con una validación deliberadamente más débil, sin ninguna razón de
negocio para esa asimetría.

DECISIÓN — tamaño máximo: 10 MB por archivo, igual para los 5 tipos
(sugerido explícitamente, sin objeción).

Candado de estado (pedido explícito, en el servicio no solo en la UI):
solo el proveedor dueño de la Factura, mientras Factura.estado esté en
BORRADOR u OBSERVADA, puede cargar/reemplazar archivos —
EN_REVISION_COMPRAS/APROBADA_COMPRAS/CANCELADO bloquean la carga.

Sub-fase 3.3: `cargar_archivo_factura` dispara automáticamente
InvoicingService.procesar_validacion_documentos (apps/invoicing/
services.py) en cuanto los 3 archivos de Factura (xml/pdf/cdr) quedan
presentes, sin importar el orden en que se cargaron. `descargar_archivo_
factura` (al final de este módulo) es lo que esa orquestación usa para
re-obtener el contenido real del XML/CDR ya subidos.
"""
import hashlib
import io

import pypdf
from django.core.exceptions import ValidationError
from pypdf.errors import PyPdfError

from apps.base.utils import OneDriveClient

from . import services_validacion as sv

TAMANO_MAXIMO_BYTES = 10 * 1024 * 1024  # 10 MB

ESTADOS_CARGA_PERMITIDA = {'BORRADOR', 'OBSERVADA'}

# tipo -> config. Una sola tabla por cada "nivel" (Factura vs.
# FacturaLinea) en vez de 5 funciones casi idénticas — la diferencia
# real entre tipos del mismo nivel es solo esta configuración.
CONFIG_FACTURA = {
    'xml': {
        'campo_archivo': 'xml_file', 'campo_hash': 'hash_xml',
        'es_xml': True, 'extension': 'xml', 'content_type': 'application/xml',
    },
    'pdf': {
        'campo_archivo': 'pdf_file', 'campo_hash': 'hash_pdf',
        'es_xml': False, 'extension': 'pdf', 'content_type': 'application/pdf',
    },
    'cdr': {
        'campo_archivo': 'cdr_xml_file', 'campo_hash': 'hash_cdr',
        'es_xml': True, 'extension': 'xml', 'content_type': 'application/xml',
    },
}

CONFIG_FACTURA_LINEA = {
    'retencion': {
        'campo_archivo': 'documento_retencion', 'campo_hash': 'hash_documento_retencion',
        'es_xml': False, 'extension': 'pdf', 'content_type': 'application/pdf',
        'campo_flag': 'aplica_retencion',
    },
    'detraccion': {
        'campo_archivo': 'documento_detraccion', 'campo_hash': 'hash_documento_detraccion',
        'es_xml': False, 'extension': 'pdf', 'content_type': 'application/pdf',
        'campo_flag': 'aplica_detraccion',
    },
}


def validar_permiso_edicion(factura, usuario):
    """
    Candado de servicio (Sub-fase 3.2, punto 4 pedido explícito entonces):
    dueño + estado. Se llama SIEMPRE al inicio de cargar_archivo_factura/
    cargar_archivo_factura_linea, sin importar si el llamador (la vista)
    ya filtró por dueño en su queryset — defensa en profundidad, y la
    única fuente de verdad real para cualquier consumidor futuro del
    servicio que no pase por esa vista.

    Renombrado de `_validar_permiso_carga` (Sub-fase 3.4, "Copiar de
    OC(s)"): mismo candado, ahora reutilizado también por
    services_borrador.py para la edición de cabecera/línea de una Factura
    ya creada — el nombre "carga" ya no describía todos sus usos. Sin
    cambio de lógica, solo de nombre/visibilidad (pública, no privada).
    """
    if not factura.proveedor.user_id or factura.proveedor.user_id != usuario.pk:
        raise ValidationError("Solo el proveedor dueño de esta Factura puede cargar archivos.")
    if factura.estado not in ESTADOS_CARGA_PERMITIDA:
        raise ValidationError(
            f"No se pueden cargar archivos con la Factura en estado '{factura.get_estado_display()}' "
            "— solo mientras está en Borrador u Observada."
        )


def _leer_y_validar_tamano(archivo) -> bytes:
    contenido = archivo.read()
    if not contenido:
        raise ValidationError("El archivo está vacío.")
    if len(contenido) > TAMANO_MAXIMO_BYTES:
        raise ValidationError(
            f"El archivo supera el tamaño máximo permitido "
            f"({TAMANO_MAXIMO_BYTES // (1024 * 1024)} MB)."
        )
    return contenido


def _validar_contenido_real(contenido: bytes, es_xml: bool):
    """Verificación de tipo por CONTENIDO real, no por extensión — ver docstring del módulo."""
    if es_xml:
        try:
            sv._parse_xml_seguro(contenido)
        except sv.ValidacionXMLError as e:
            raise ValidationError(f"El archivo no es un XML válido: {e}")
    else:
        if not contenido.startswith(b'%PDF-'):
            raise ValidationError("El archivo no es un PDF válido (firma de archivo incorrecta).")
        try:
            lector = pypdf.PdfReader(io.BytesIO(contenido))
            len(lector.pages)  # fuerza a recorrer el árbol de páginas, no solo abrir el archivo
        except PyPdfError as e:
            raise ValidationError(f"El archivo no es un PDF válido o está corrupto: {e}")


def cargar_archivo_factura(factura, tipo: str, archivo, usuario):
    """
    Valida y carga uno de los 3 archivos a nivel de Factura (xml/pdf/cdr).
    Guarda el link de OneDrive + hash SHA-256 en los campos
    correspondientes. Lanza ValidationError en cualquier rechazo, sin
    tocar la BD ni subir nada a OneDrive si la validación falla.
    """
    validar_permiso_edicion(factura, usuario)

    config = CONFIG_FACTURA.get(tipo)
    if config is None:
        raise ValidationError(f"Tipo de archivo desconocido: '{tipo}'.")

    contenido = _leer_y_validar_tamano(archivo)
    _validar_contenido_real(contenido, config['es_xml'])
    hash_sha256 = hashlib.sha256(contenido).hexdigest()

    onedrive = OneDriveClient()
    ruc = factura.proveedor.ruc or factura.proveedor.sap_card_code
    url = onedrive.upload_documento_factura(
        contenido=contenido,
        sede=factura.sede.codigo,
        ruc=ruc,
        identificador=str(factura.pk),
        nombre_archivo=f"{tipo}.{config['extension']}",
        content_type=config['content_type'],
    )

    setattr(factura, config['campo_archivo'], url)
    setattr(factura, config['campo_hash'], hash_sha256)
    factura.save(update_fields=[config['campo_archivo'], config['campo_hash'], 'updated_at'])

    # Sub-fase 3.3: apenas los 3 archivos de Factura quedan presentes
    # (sin importar cuál de los 3 se acaba de subir), dispara la
    # orquestación de validación de negocio. Import local (no a nivel de
    # módulo) para no crear un ciclo con services.py, que a su vez llama
    # de vuelta a este módulo (descargar_archivo_factura) — mismo
    # principio ya usado por services.py::notificar_factura_observada
    # para su import de apps.base.services_correo.
    if factura.xml_file and factura.pdf_file and factura.cdr_xml_file:
        from .services import InvoicingService
        InvoicingService.procesar_validacion_documentos(factura)

    return factura


def descargar_archivo_factura(factura, tipo: str) -> bytes:
    """
    Descarga el contenido real de un archivo de Factura ya cargado
    (xml/pdf/cdr), reconstruyendo la misma ruta determinística que
    `cargar_archivo_factura` usó para subirlo — usada por
    InvoicingService.procesar_validacion_documentos (Sub-fase 3.3) para
    re-obtener el XML/CDR y ejecutar la validación de negocio sobre su
    contenido real (necesita los bytes crudos, no solo el link
    compartido guardado en el campo — ver OneDriveClient._descargar).
    """
    config = CONFIG_FACTURA.get(tipo)
    if config is None:
        raise ValidationError(f"Tipo de archivo desconocido: '{tipo}'.")

    onedrive = OneDriveClient()
    ruc = factura.proveedor.ruc or factura.proveedor.sap_card_code
    return onedrive.descargar_documento_factura(
        sede=factura.sede.codigo,
        ruc=ruc,
        identificador=str(factura.pk),
        nombre_archivo=f"{tipo}.{config['extension']}",
    )


def cargar_archivo_factura_linea(factura_linea, tipo: str, archivo, usuario):
    """
    Valida y carga uno de los 2 archivos a nivel de FacturaLinea
    (retencion/detraccion). Exige que la bandera correspondiente
    (aplica_retencion/aplica_detraccion) ya esté activa — no tiene
    sentido cargar un documento de retención en una línea que no la
    aplica.
    """
    factura = factura_linea.factura
    validar_permiso_edicion(factura, usuario)

    config = CONFIG_FACTURA_LINEA.get(tipo)
    if config is None:
        raise ValidationError(f"Tipo de archivo desconocido: '{tipo}'.")
    if not getattr(factura_linea, config['campo_flag']):
        raise ValidationError(
            f"Esta línea no tiene {config['campo_flag']}=True — no corresponde cargar este documento."
        )

    contenido = _leer_y_validar_tamano(archivo)
    _validar_contenido_real(contenido, config['es_xml'])
    hash_sha256 = hashlib.sha256(contenido).hexdigest()

    onedrive = OneDriveClient()
    ruc = factura.proveedor.ruc or factura.proveedor.sap_card_code
    identificador = f"{factura.pk}/L{factura_linea.po_line.line_num}"
    url = onedrive.upload_documento_factura(
        contenido=contenido,
        sede=factura.sede.codigo,
        ruc=ruc,
        identificador=identificador,
        nombre_archivo=f"{tipo}.{config['extension']}",
        content_type=config['content_type'],
    )

    setattr(factura_linea, config['campo_archivo'], url)
    setattr(factura_linea, config['campo_hash'], hash_sha256)
    factura_linea.save(update_fields=[config['campo_archivo'], config['campo_hash']])
    return factura_linea
