# apps/invoicing/services_validacion.py
"""
Validación de firma XAdES + extracción de datos UBL 2.1 para comprobantes
de pago electrónicos peruanos (factura del proveedor y su CDR de SUNAT) —
Sub-fase 3.1 de Facturación. Servicio aislado, sin ninguna dependencia del
modelo Factura (Sub-fase 3.2/3.3, todavía no existe) ni de ningún modelo
de Django — recibe/devuelve solo tipos de Python (bytes, dataclasses).

Depende de `xmlsec` (bindings de libxmlsec1) + `lxml` — ver requirements.txt
para el detalle de la dependencia de sistema en el entorno de despliegue.

Regla de seguridad no negociable: TODO parseo de un XML que llega de un
tercero (factura del proveedor, CDR de SUNAT) pasa por `_parser_seguro()`
(resolve_entities=False, no_network=True, sin DTD) — nunca
etree.parse()/fromstring() con el parser por defecto de lxml, que sí
resuelve entidades externas y es vulnerable a XXE.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional, Union

import xmlsec
from lxml import etree

# ── Namespaces UBL 2.1 / SUNAT ───────────────────────────────────────────
NS = {
    'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
    'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
    'ds':  'http://www.w3.org/2000/09/xmldsig#',
    'ext': 'urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2',
}

# Atributo que SUNAT usa como ID del nodo raíz del comprobante
# (Invoice/@Id="comprobante" o similar), referenciado por
# ds:Reference/@URI="#comprobante" en la firma enveloped. xmlsec necesita
# saber explícitamente qué atributo tratar como ID — normalmente eso lo
# declara un DTD/XSD, pero el parser seguro (XXE) deshabilita DTD a
# propósito, así que se registra a mano vía xmlsec.tree.add_ids().
_ATRIBUTOS_ID = ['Id']


class ValidacionXMLError(Exception):
    """XML mal formado, sin firma, o estructura UBL inesperada."""


# bytes | ruta (str/Path) | objeto con .read() (UploadedFile de Django, o
# un open(..., 'rb') de test) — ver _leer_bytes().
ArchivoLike = Union[bytes, str, os.PathLike]


def _leer_bytes(archivo: ArchivoLike) -> bytes:
    """
    Acepta bytes directos, una ruta (str/Path) o un objeto tipo-archivo
    (UploadedFile de Django, o un open(..., 'rb') de test) — para no atar
    la firma de las funciones públicas a un tipo concreto de "archivo".
    """
    if isinstance(archivo, bytes):
        return archivo
    if hasattr(archivo, 'read'):
        contenido = archivo.read()
        if hasattr(archivo, 'seek'):
            archivo.seek(0)
        return contenido if isinstance(contenido, bytes) else contenido.encode('utf-8')
    with open(archivo, 'rb') as f:
        return f.read()


def _parser_seguro() -> etree.XMLParser:
    """
    Parser lxml endurecido contra XXE (XML External Entity) — usar SIEMPRE
    para XML que llega de un tercero. `resolve_entities=False` impide que
    una entidad externa declarada en el propio XML se resuelva (lectura de
    archivos locales / SSRF); `no_network=True` bloquea cualquier fetch de
    red que el parser pudiera intentar; `load_dtd=False` evita cargar un
    DTD externo referenciado por el XML (que podría, a su vez, declarar
    entidades maliciosas).
    """
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
    )


def _parse_xml_seguro(archivo: ArchivoLike) -> etree._Element:
    contenido = _leer_bytes(archivo)
    try:
        return etree.fromstring(contenido, parser=_parser_seguro())
    except etree.XMLSyntaxError as e:
        raise ValidacionXMLError(f"XML mal formado: {e}") from e


def _texto(nodo: etree._Element, xpath: str) -> str:
    """findtext() con namespaces ya aplicados, siempre str (nunca None)."""
    valor = nodo.findtext(xpath, namespaces=NS)
    return (valor or '').strip()


def _decimal(texto: str) -> Optional[Decimal]:
    if not texto:
        return None
    try:
        return Decimal(texto)
    except InvalidOperation:
        return None


# ═══════════════════════════════════════════════════════════════════════
# 1. Validación de firma XAdES
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class ResultadoValidacionFirma:
    valido: bool
    certificado_pem: Optional[str] = None
    error: Optional[str] = None


def _der_a_pem(cert_der: bytes) -> str:
    """Envuelve un certificado X.509 en DER crudo con el formato PEM estándar."""
    b64 = base64.b64encode(cert_der).decode('ascii')
    lineas = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return "-----BEGIN CERTIFICATE-----\n" + "\n".join(lineas) + "\n-----END CERTIFICATE-----\n"


def validar_firma_xml(archivo: ArchivoLike) -> ResultadoValidacionFirma:
    """
    Verifica la firma XAdES enveloped de un comprobante UBL peruano contra
    el certificado EMBEBIDO en el propio XML
    (ds:Signature/.../ds:KeyInfo/ds:X509Data/ds:X509Certificate) — no se
    valida ninguna cadena de confianza externa, OCSP ni CRL (fuera de
    alcance de este spike: SUNAT ya validó al emisor al aceptar el
    comprobante; esto solo confirma que el documento no fue alterado
    después de firmarse, usando el certificado que el propio XML declara).

    Devuelve ResultadoValidacionFirma — nunca lanza por firma inválida
    (eso es un resultado esperado, no un error de sistema); sí puede
    lanzar ValidacionXMLError si el XML está mal formado.
    """
    root = _parse_xml_seguro(archivo)
    xmlsec.tree.add_ids(root, _ATRIBUTOS_ID)

    nodo_firma = root.find('.//ds:Signature', namespaces=NS)
    if nodo_firma is None:
        return ResultadoValidacionFirma(valido=False, error="El XML no contiene ninguna firma (ds:Signature).")

    cert_b64 = _texto(nodo_firma, './/ds:KeyInfo/ds:X509Data/ds:X509Certificate')
    if not cert_b64:
        return ResultadoValidacionFirma(
            valido=False,
            error="La firma no incluye un certificado embebido (ds:KeyInfo/ds:X509Data/ds:X509Certificate).",
        )

    try:
        cert_der = base64.b64decode(cert_b64)
    except (ValueError, TypeError) as e:
        return ResultadoValidacionFirma(valido=False, error=f"Certificado embebido con base64 inválido: {e}")

    cert_pem = _der_a_pem(cert_der)

    try:
        key = xmlsec.Key.from_memory(cert_der, xmlsec.constants.KeyDataFormatCertDer)
        ctx = xmlsec.SignatureContext()
        ctx.key = key
        ctx.verify(nodo_firma)
    except xmlsec.Error as e:
        return ResultadoValidacionFirma(valido=False, certificado_pem=cert_pem, error=f"Firma inválida: {e}")

    return ResultadoValidacionFirma(valido=True, certificado_pem=cert_pem)


# ═══════════════════════════════════════════════════════════════════════
# 2. Extracción de datos de la factura (UBL Invoice-2)
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class DatosFactura:
    numero: str                    # cbc:ID, ej. "F001-123"
    fecha_emision: str              # cbc:IssueDate, 'YYYY-MM-DD'
    importe_total: Optional[Decimal]  # cac:LegalMonetaryTotal/cbc:PayableAmount
    moneda: str                     # @currencyID del PayableAmount
    ruc_emisor: str                 # RUC del proveedor (AccountingSupplierParty)
    razon_social_emisor: str


def extraer_datos_factura(xml: ArchivoLike) -> DatosFactura:
    """
    Extrae los datos mínimos de una factura UBL 2.1 peruana: número, fecha
    de emisión, importe total, moneda y el RUC/razón social del emisor.
    No valida la firma (usar validar_firma_xml aparte) — es puramente
    lectura de estructura, sobre el mismo parser seguro contra XXE.
    """
    root = _parse_xml_seguro(xml)

    numero = _texto(root, './/cbc:ID')
    fecha_emision = _texto(root, './/cbc:IssueDate')

    nodo_importe = root.find('.//cac:LegalMonetaryTotal/cbc:PayableAmount', namespaces=NS)
    importe_total = _decimal(nodo_importe.text.strip()) if nodo_importe is not None and nodo_importe.text else None
    moneda = (nodo_importe.get('currencyID') or '') if nodo_importe is not None else ''

    # RUC del emisor: cac:AccountingSupplierParty/cac:Party/cac:PartyIdentification/cbc:ID
    # (schemeID="6" = RUC en el catálogo SUNAT de tipos de documento de identidad,
    # pero en la práctica la factura del proveedor solo trae un PartyIdentification).
    ruc_emisor = _texto(
        root, './/cac:AccountingSupplierParty/cac:Party/cac:PartyIdentification/cbc:ID'
    )
    razon_social_emisor = _texto(
        root,
        './/cac:AccountingSupplierParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName',
    )

    return DatosFactura(
        numero=numero,
        fecha_emision=fecha_emision,
        importe_total=importe_total,
        moneda=moneda,
        ruc_emisor=ruc_emisor,
        razon_social_emisor=razon_social_emisor,
    )


# ═══════════════════════════════════════════════════════════════════════
# 3. Extracción del estado del CDR (ApplicationResponse-2)
# ═══════════════════════════════════════════════════════════════════════

ESTADO_ACEPTADO = 'ACEPTADO'
ESTADO_OBSERVADO = 'OBSERVADO'
ESTADO_RECHAZADO = 'RECHAZADO'
ESTADO_DESCONOCIDO = 'DESCONOCIDO'


@dataclass
class EstadoCDR:
    response_code: str          # cbc:ResponseCode crudo, ej. '0', '2335', '4003'
    estado: str                 # ESTADO_ACEPTADO / _OBSERVADO / _RECHAZADO / _DESCONOCIDO
    descripcion: str            # cbc:Description del propio CDR — SUNAT ya
                                 # trae el mensaje humano, no hace falta un
                                 # catálogo propio de descripciones por código.


def _clasificar_response_code(codigo: str) -> str:
    """
    NOTA sobre el "catálogo 21": se investigó antes de implementar esto —
    el Catálogo N.° 21 de SUNAT (Anexo 7, Estándar UBL Perú) es
    "Documentos Relacionados" (guía de remisión), NO tiene nada que ver
    con los códigos de respuesta del CDR. No existe un catálogo numerado
    oficial de SUNAT para esto; el propio Manual del Programador de SUNAT
    documenta los códigos por RANGO:
      '0'        -> Aceptado
      2000-3999  -> error que genera RECHAZO del comprobante
      4000+      -> OBSERVACIÓN (aceptado con observaciones)
    Por eso esta función clasifica por rango en vez de mantener una tabla
    propia de código->descripción — la descripción humana ya viene en
    cbc:Description del propio CDR (ver EstadoCDR.descripcion), que es la
    fuente real y siempre actualizada, no algo que este servicio deba
    intentar replicar.
    """
    if codigo == '0':
        return ESTADO_ACEPTADO
    if not codigo.isdigit():
        return ESTADO_DESCONOCIDO
    numero = int(codigo)
    if 2000 <= numero <= 3999:
        return ESTADO_RECHAZADO
    if numero >= 4000:
        return ESTADO_OBSERVADO
    return ESTADO_DESCONOCIDO


def extraer_estado_cdr(cdr_xml: ArchivoLike) -> EstadoCDR:
    """
    Extrae el estado de un CDR (Constancia de Recepción) de SUNAT:
    cac:DocumentResponse/cac:Response/cbc:ResponseCode + cbc:Description.
    """
    root = _parse_xml_seguro(cdr_xml)

    response_code = _texto(root, './/cac:DocumentResponse/cac:Response/cbc:ResponseCode')
    descripcion = _texto(root, './/cac:DocumentResponse/cac:Response/cbc:Description')

    if not response_code:
        raise ValidacionXMLError(
            "El CDR no contiene cac:DocumentResponse/cac:Response/cbc:ResponseCode."
        )

    return EstadoCDR(
        response_code=response_code,
        estado=_clasificar_response_code(response_code),
        descripcion=descripcion,
    )
