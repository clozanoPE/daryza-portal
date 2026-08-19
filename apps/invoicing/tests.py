# apps/invoicing/tests.py
"""
Suite de apps.invoicing — Sub-fase 3.1 (sesión 59): validación de firma
XAdES + extracción de datos UBL, servicio aislado sin modelo Factura
todavía.

Fixtures (apps/invoicing/fixtures/*.xml) — IMPORTANTE, no son los 2
archivos reales que el usuario mencionó tener disponibles: esta sesión no
recibió ningún adjunto real (no llegó ningún archivo a la conversación),
así que se generaron 4 XML SINTÉTICOS que replican la estructura UBL 2.1
descrita explícitamente por el usuario (certificado embebido en ds:
KeyInfo/X509Data/X509Certificate, cac:DocumentResponse/cbc:ResponseCode +
cbc:Description en el CDR), firmados de verdad con xmlsec contra un
certificado autofirmado de prueba generado solo para esto — así que la
verificación de firma que prueban es real (matemáticamente válida/
inválida), aunque el contenido de negocio (RUC, montos, etc.) es
inventado. Nombrados con el prefijo "sintetic_"/"sintetica_" a propósito,
para que nunca se confundan con datos reales de SUNAT si se revisan más
adelante. Si el usuario proporciona los 2 archivos reales, pueden
sumarse como fixtures adicionales sin reemplazar estas (que igual siguen
siendo útiles: no dependen de que el certificado real de un proveedor no
haya expirado).
"""
import os

from django.test import SimpleTestCase

from apps.invoicing import services_validacion as sv

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')


def _leer_fixture(nombre: str) -> bytes:
    with open(os.path.join(FIXTURES_DIR, nombre), 'rb') as f:
        return f.read()


class ValidarFirmaXMLTests(SimpleTestCase):
    """
    SimpleTestCase (no TestCase): este servicio no toca la base de datos
    en absoluto, así que no hace falta la infraestructura de transacción/
    rollback por test — coherente con que services_validacion.py es
    Python puro, sin ningún import de apps.invoicing.models.
    """

    def test_firma_valida_sobre_documento_no_alterado(self):
        resultado = sv.validar_firma_xml(_leer_fixture('factura_sintetica_firmada.xml'))
        self.assertTrue(resultado.valido)
        self.assertIsNone(resultado.error)
        self.assertIsNotNone(resultado.certificado_pem)
        self.assertIn('BEGIN CERTIFICATE', resultado.certificado_pem)

    def test_firma_invalida_si_el_documento_fue_alterado_despues_de_firmar(self):
        """
        factura_sintetica_alterada.xml es la misma factura firmada, con
        PayableAmount modificado DESPUÉS de firmar (mismo mecanismo que
        detectaría a un proveedor que intente inflar un monto ya firmado
        por SUNAT/el emisor) — debe fallar la verificación, no solo "no
        coincidir con algo", sino específicamente por firma inválida.
        """
        resultado = sv.validar_firma_xml(_leer_fixture('factura_sintetica_alterada.xml'))
        self.assertFalse(resultado.valido)
        self.assertIsNotNone(resultado.error)
        self.assertIn('inv', resultado.error.lower())  # "inválida" (sin asumir el encoding exacto del msg)

    def test_sin_firma_devuelve_invalido_sin_lanzar_excepcion(self):
        sin_firma = b'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"/>'
        resultado = sv.validar_firma_xml(sin_firma)
        self.assertFalse(resultado.valido)
        self.assertIn('no contiene ninguna firma', resultado.error)

    def test_xml_mal_formado_lanza_validacion_xml_error(self):
        with self.assertRaises(sv.ValidacionXMLError):
            sv.validar_firma_xml(b'<Invoice><etiqueta_sin_cerrar>')

    def test_acepta_ruta_de_archivo_ademas_de_bytes(self):
        """La firma pública admite bytes, ruta o file-like — se prueba la ruta aquí."""
        ruta = os.path.join(FIXTURES_DIR, 'factura_sintetica_firmada.xml')
        resultado = sv.validar_firma_xml(ruta)
        self.assertTrue(resultado.valido)

    def test_xxe_no_resuelve_entidad_externa(self):
        """
        Regresión de seguridad: un XML con una entidad externa declarada
        (DOCTYPE + ENTITY SYSTEM) no debe filtrar contenido de archivos
        locales del servidor. Se prueba contra extraer_datos_factura (más
        fácil de inspeccionar el resultado) pero el parser seguro es
        compartido por las 3 funciones públicas de este módulo.
        """
        malicioso = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE Invoice [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>'
            b'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2">'
            b'<cbc:ID xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">'
            b'&xxe;</cbc:ID></Invoice>'
        )
        datos = sv.extraer_datos_factura(malicioso)
        # La entidad no se resuelve: el contenido queda vacío, nunca el
        # contenido de /etc/passwd ni un error que delate si el archivo existe.
        self.assertEqual(datos.numero, '')


class ExtraerDatosFacturaTests(SimpleTestCase):

    def setUp(self):
        self.datos = sv.extraer_datos_factura(_leer_fixture('factura_sintetica_firmada.xml'))

    def test_numero(self):
        self.assertEqual(self.datos.numero, 'F001-123')

    def test_fecha_emision(self):
        self.assertEqual(self.datos.fecha_emision, '2026-08-19')

    def test_importe_total_y_moneda(self):
        from decimal import Decimal
        self.assertEqual(self.datos.importe_total, Decimal('1180.50'))
        self.assertEqual(self.datos.moneda, 'PEN')

    def test_ruc_y_razon_social_emisor(self):
        self.assertEqual(self.datos.ruc_emisor, '20100055237')
        self.assertEqual(self.datos.razon_social_emisor, 'ALICORP S.A.A.')

    def test_xml_sin_los_campos_no_lanza_devuelve_vacios(self):
        vacio = b'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"/>'
        datos = sv.extraer_datos_factura(vacio)
        self.assertEqual(datos.numero, '')
        self.assertEqual(datos.fecha_emision, '')
        self.assertIsNone(datos.importe_total)
        self.assertEqual(datos.moneda, '')


class ExtraerEstadoCDRTests(SimpleTestCase):

    def test_response_code_0_es_aceptado(self):
        estado = sv.extraer_estado_cdr(_leer_fixture('cdr_sintetico_aceptado.xml'))
        self.assertEqual(estado.response_code, '0')
        self.assertEqual(estado.estado, sv.ESTADO_ACEPTADO)
        self.assertEqual(estado.descripcion, 'La Factura numero F001-123, ha sido aceptada')

    def test_response_code_2335_es_rechazado(self):
        """
        2335 = "documento alterado" — dentro del rango 2000-3999 que el
        Manual del Programador de SUNAT documenta como errores que
        generan RECHAZO (no existe un "catálogo 21" para esto — ver la
        nota en _clasificar_response_code).
        """
        estado = sv.extraer_estado_cdr(_leer_fixture('cdr_sintetico_rechazado.xml'))
        self.assertEqual(estado.response_code, '2335')
        self.assertEqual(estado.estado, sv.ESTADO_RECHAZADO)
        self.assertTrue(estado.descripcion)

    def test_response_code_4000_mas_es_observado(self):
        cdr = (
            b'<ApplicationResponse xmlns="urn:oasis:names:specification:ubl:schema:xsd:ApplicationResponse-2" '
            b'xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" '
            b'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">'
            b'<cac:DocumentResponse><cac:Response>'
            b'<cbc:ResponseCode>4003</cbc:ResponseCode>'
            b'<cbc:Description>Observado: dato con formato incorrecto</cbc:Description>'
            b'</cac:Response></cac:DocumentResponse></ApplicationResponse>'
        )
        estado = sv.extraer_estado_cdr(cdr)
        self.assertEqual(estado.estado, sv.ESTADO_OBSERVADO)

    def test_response_code_no_numerico_es_desconocido(self):
        cdr = (
            b'<ApplicationResponse xmlns="urn:oasis:names:specification:ubl:schema:xsd:ApplicationResponse-2" '
            b'xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" '
            b'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">'
            b'<cac:DocumentResponse><cac:Response>'
            b'<cbc:ResponseCode>ABC</cbc:ResponseCode>'
            b'</cac:Response></cac:DocumentResponse></ApplicationResponse>'
        )
        estado = sv.extraer_estado_cdr(cdr)
        self.assertEqual(estado.estado, sv.ESTADO_DESCONOCIDO)

    def test_cdr_sin_response_code_lanza_validacion_xml_error(self):
        cdr_vacio = b'<ApplicationResponse xmlns="urn:oasis:names:specification:ubl:schema:xsd:ApplicationResponse-2"/>'
        with self.assertRaises(sv.ValidacionXMLError):
            sv.extraer_estado_cdr(cdr_vacio)
