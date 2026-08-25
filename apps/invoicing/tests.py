# apps/invoicing/tests.py
"""
Suite de apps.invoicing. 2 partes independientes, sin dependencias entre
sí (services_validacion.py es Python puro sin modelos; el resto de este
archivo sí toca la base de datos):

  1. Sub-fase 3.1a (sesión 59): validación de firma XAdES + extracción de
     datos UBL — servicio aislado, sin modelo Factura.

  2. Sub-fase 3.1b (esta sesión): modelo Factura + InvoicingService
     (saldo_disponible / validar_oc_disponible / validar_retencion_
     detraccion). Reutiliza OperationsTestBase (apps.operations.tests)
     para construir Tickets reales de principio a fin (vía
     AppointmentService/OperationsService, nunca escribiendo estado
     directo sobre el modelo) hasta FINALIZADO — solo así existe una
     EntradaMercaderiaLinea real, la fuente de dato que saldo_disponible
     necesita (ver su docstring: nunca PurchaseOrderLine.quantity_sap).

  3. Carga segura de archivos (services_archivos.py, Sub-fase 3.2) + los
     2 endpoints HTTP reales que la consumen (views.py).

  4. Sub-fase 3.3 (esta sesión): InvoicingService.procesar_validacion_
     documentos (dispara automáticamente validar_firma_xml/extraer_
     datos_factura/extraer_estado_cdr en cuanto los 3 archivos de una
     Factura están completos) + enviar_a_revision (candado que bloquea
     el avance a EN_REVISION_COMPRAS por firma inválida, CDR rechazado,
     o importe del XML que no coincide con la suma de FacturaLinea).
"""
import hashlib
import os
import threading
import time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connections, transaction
from django.db.models import Sum
from django.test import SimpleTestCase, TransactionTestCase

from apps.appointments.models import AppointmentSlot
from apps.appointments.services import AppointmentService
from apps.base.models import Sede, SupplierProfile
from apps.base.services_correo import ResultadoEnvioCorreo
from apps.invoicing import services_validacion as sv
from apps.operations.models import Ticket, TicketLineInspection
from apps.operations.services import OperationsService
from apps.operations.tests import OperationsTestBase
from apps.sap_sync.models import PurchaseOrder, PurchaseOrderLine

from . import services_archivos as sa
from .models import Factura, FacturaLinea, FacturaOrdenCompra
from .services import InvoicingService

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')


def _leer_fixture(nombre: str) -> bytes:
    with open(os.path.join(FIXTURES_DIR, nombre), 'rb') as f:
        return f.read()


# ═══════════════════════════════════════════════════════════════════════════
# Parte 1 (sesión 59) — validación de firma XAdES + extracción UBL
# ═══════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════
# Parte 2 (esta sesión) — modelo Factura + InvoicingService
# ═══════════════════════════════════════════════════════════════════════════

class InvoicingTestBase(OperationsTestBase):
    """
    Agrega, sobre la base ya existente de apps.operations, el paso final
    que esa base no necesitaba (registrar_calidad + registrar_salida) —
    aquí sí hace falta: sin un Ticket FINALIZADO no existe ninguna
    EntradaMercaderia/EntradaMercaderiaLinea real.
    """

    def _finalizar_ticket(self, u_mss_tdb: str, cantidad_real, requiere_calidad: bool = False) -> Ticket:
        """
        Construye un Ticket real hasta FINALIZADO, con la cantidad REAL
        inspeccionada (no la de SAP) fijada explícitamente en
        `cantidad_real` — esa es la cantidad que terminará en
        EntradaMercaderiaLinea.cantidad, la base de saldo_disponible.
        """
        ticket = self._crear_ticket_en_etapa(
            Ticket.ETAPA_ALMACEN, u_mss_tdb,
            requiere_coa=(u_mss_tdb == 'MP'), requiere_calidad=requiere_calidad,
        )

        def _resultados():
            return [
                {
                    'inspeccion_id': insp.id,
                    'estado': 'CONFORME',
                    'cantidad_modificada': str(cantidad_real),
                }
                for insp in TicketLineInspection.objects.filter(ticket=ticket, etapa='ALMACEN')
            ]

        actor = self.u_materia_prima if ticket.es_materia_prima else self.u_almacen
        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=actor, resultados=_resultados(),
        )
        ticket.refresh_from_db()

        if ticket.requiere_calidad:
            OperationsService.registrar_calidad(
                ticket_id=ticket.id, usuario_calidad=self.u_calidad, resultados=_resultados(),
            )
            ticket.refresh_from_db()

        OperationsService.registrar_salida(ticket_id=ticket.id, usuario_vigilancia=self.u_vigilancia)
        ticket.refresh_from_db()
        return ticket

    def _po_line_de(self, ticket: Ticket):
        """La (única) PurchaseOrderLine del Ticket construido por _finalizar_ticket."""
        po = ticket.appointment.purchase_orders.first()
        return po.lines.first()

    def _crear_factura(self, estado='BORRADOR', proveedor=None):
        return Factura.objects.create(
            proveedor=proveedor or self._supplier_profile(),
            sede=self._sede(),
            estado=estado,
        )

    def _supplier_profile(self):
        """
        SupplierProfile vinculado a self.proveedor (User creado por
        OperationsTestBase) — Factura.proveedor es FK a SupplierProfile,
        no a User directo, desde la migración de esta sesión.
        """
        from apps.base.models import SupplierProfile
        perfil, _ = SupplierProfile.objects.get_or_create(
            sap_card_code=self.proveedor.username,
            defaults={'ruc': self.proveedor.username, 'user': self.proveedor},
        )
        return perfil

    def _sede(self):
        from apps.base.models import Sede
        return Sede.objects.get(codigo='LURIN')

    def _crear_factura_linea(self, factura, po_line, cantidad, **extra):
        defaults = dict(
            cantidad_oc=po_line.quantity_sap,
            precio_oc=Decimal('10.0000'),
            cantidad=cantidad,
            precio=Decimal('10.0000'),
        )
        defaults.update(extra)
        return FacturaLinea.objects.create(factura=factura, po_line=po_line, **defaults)


class SaldoDisponibleTests(InvoicingTestBase):

    def test_sin_entrada_mercaderia_lanza_validation_error(self):
        """Ticket construido pero no finalizado -> sin EntradaMercaderiaLinea todavía."""
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'CDL', requiere_coa=False)
        po_line = self._po_line_de(ticket)

        with self.assertRaises(ValidationError):
            InvoicingService.saldo_disponible(po_line)

    def test_saldo_igual_a_lo_recibido_sin_ninguna_factura(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('8.0000'))
        po_line = self._po_line_de(ticket)

        self.assertEqual(InvoicingService.saldo_disponible(po_line), Decimal('8.0000'))

    def test_saldo_disminuye_con_factura_activa(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)

        factura = self._crear_factura(estado='EN_REVISION_COMPRAS')
        self._crear_factura_linea(factura, po_line, cantidad=Decimal('4.0000'))

        self.assertEqual(InvoicingService.saldo_disponible(po_line), Decimal('6.0000'))

    def test_saldo_ignora_lineas_de_factura_cancelada(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)

        factura_cancelada = self._crear_factura(estado='CANCELADO')
        self._crear_factura_linea(factura_cancelada, po_line, cantidad=Decimal('4.0000'))

        self.assertEqual(InvoicingService.saldo_disponible(po_line), Decimal('10.0000'))

    def test_excluir_factura_propia_al_editar(self):
        """
        Al recalcular el saldo para la MISMA Factura que ya tiene una línea
        sobre esa po_line, excluir_factura evita descontarla dos veces.
        """
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)

        factura = self._crear_factura(estado='BORRADOR')
        self._crear_factura_linea(factura, po_line, cantidad=Decimal('3.0000'))

        # Sin excluir: la propia línea ya cuenta contra el saldo.
        self.assertEqual(InvoicingService.saldo_disponible(po_line), Decimal('7.0000'))
        # Excluyéndola: como si la Factura no existiera todavía.
        self.assertEqual(
            InvoicingService.saldo_disponible(po_line, excluir_factura=factura),
            Decimal('10.0000'),
        )

    def test_saldo_suma_multiples_facturas_activas(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)

        f1 = self._crear_factura(estado='BORRADOR')
        self._crear_factura_linea(f1, po_line, cantidad=Decimal('3.0000'))
        f2 = self._crear_factura(estado='APROBADA_COMPRAS')
        self._crear_factura_linea(f2, po_line, cantidad=Decimal('2.0000'))

        self.assertEqual(InvoicingService.saldo_disponible(po_line), Decimal('5.0000'))


class ValidarOcDisponibleTests(InvoicingTestBase):
    """Candado de unicidad de OC (InvoicingService.validar_oc_disponible)."""

    def test_oc_libre_no_rechaza(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()

        InvoicingService.validar_oc_disponible(po)  # no debe lanzar

    def test_oc_ya_usada_por_factura_activa_rechaza(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()

        factura = self._crear_factura(estado='EN_REVISION_COMPRAS')
        FacturaOrdenCompra.objects.create(factura=factura, purchase_order=po)

        with self.assertRaises(ValidationError):
            InvoicingService.validar_oc_disponible(po)

    def test_oc_ya_usada_rechaza_sin_importar_el_estado_no_cancelado(self):
        """BORRADOR/EN_REVISION_COMPRAS/OBSERVADA/APROBADA_COMPRAS bloquean por igual."""
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()

        for estado in ('BORRADOR', 'EN_REVISION_COMPRAS', 'OBSERVADA', 'APROBADA_COMPRAS'):
            with self.subTest(estado=estado):
                factura = self._crear_factura(estado=estado)
                FacturaOrdenCompra.objects.create(factura=factura, purchase_order=po)
                with self.assertRaises(ValidationError):
                    InvoicingService.validar_oc_disponible(po)
                # Limpieza para el siguiente estado del mismo subTest.
                factura.delete()

    def test_cancelar_factura_libera_el_candado_de_oc(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()

        factura = self._crear_factura(estado='EN_REVISION_COMPRAS')
        FacturaOrdenCompra.objects.create(factura=factura, purchase_order=po)

        with self.assertRaises(ValidationError):
            InvoicingService.validar_oc_disponible(po)

        factura.estado = 'CANCELADO'
        factura.save(update_fields=['estado'])

        InvoicingService.validar_oc_disponible(po)  # ya no debe lanzar

    def test_excluir_factura_propia_al_editar(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()

        factura = self._crear_factura(estado='BORRADOR')
        FacturaOrdenCompra.objects.create(factura=factura, purchase_order=po)

        # Sin excluir: se bloquea a sí misma.
        with self.assertRaises(ValidationError):
            InvoicingService.validar_oc_disponible(po)
        # Excluyéndola: no se bloquea a sí misma.
        InvoicingService.validar_oc_disponible(po, excluir_factura=factura)


class ValidarRetencionDetraccionTests(InvoicingTestBase):

    def _linea(self, ticket, **extra):
        po_line = self._po_line_de(ticket)
        factura = self._crear_factura(estado='BORRADOR')
        return self._crear_factura_linea(factura, po_line, cantidad=Decimal('1.0000'), **extra)

    def test_retencion_sin_documento_rechaza(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        linea = self._linea(ticket, aplica_retencion=True)

        with self.assertRaises(ValidationError):
            InvoicingService.validar_retencion_detraccion(linea)

    def test_retencion_con_documento_no_rechaza(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        archivo = SimpleUploadedFile('retencion.pdf', b'contenido-de-prueba', content_type='application/pdf')
        linea = self._linea(ticket, aplica_retencion=True, documento_retencion=archivo)

        InvoicingService.validar_retencion_detraccion(linea)  # no debe lanzar

    def test_detraccion_sin_documento_rechaza(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        linea = self._linea(ticket, aplica_detraccion=True)

        with self.assertRaises(ValidationError):
            InvoicingService.validar_retencion_detraccion(linea)

    def test_ninguna_bandera_activa_no_rechaza(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        linea = self._linea(ticket)

        InvoicingService.validar_retencion_detraccion(linea)  # no debe lanzar


class FacturaLineaDifiereDeOCTests(InvoicingTestBase):
    """FacturaLinea.difiere_de_oc, calculado en save()."""

    def test_difiere_de_oc_false_cuando_coincide(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)
        factura = self._crear_factura()

        linea = self._crear_factura_linea(
            factura, po_line, cantidad=Decimal('10.0000'),
            cantidad_oc=Decimal('10.0000'), precio_oc=Decimal('5.0000'), precio=Decimal('5.0000'),
        )
        self.assertFalse(linea.difiere_de_oc)

    def test_difiere_de_oc_true_si_cantidad_distinta(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)
        factura = self._crear_factura()

        linea = self._crear_factura_linea(
            factura, po_line, cantidad=Decimal('9.0000'),
            cantidad_oc=Decimal('10.0000'), precio_oc=Decimal('5.0000'), precio=Decimal('5.0000'),
        )
        self.assertTrue(linea.difiere_de_oc)

    def test_difiere_de_oc_true_si_precio_distinto(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)
        factura = self._crear_factura()

        linea = self._crear_factura_linea(
            factura, po_line, cantidad=Decimal('10.0000'),
            cantidad_oc=Decimal('10.0000'), precio_oc=Decimal('5.0000'), precio=Decimal('5.5000'),
        )
        self.assertTrue(linea.difiere_de_oc)


class ValidarOcDisponibleConcurrenciaTests(TransactionTestCase):
    """
    Regresión del BUG REAL encontrado y corregido en la sesión de esta
    prueba: sin bloquear PurchaseOrder, 2 llamadas concurrentes intentando
    crear la PRIMERA Factura para la misma OC pasaban AMBAS la validación
    (select_for_update() sobre un queryset vacío no bloquea nada) —
    confirmado con hilos + conexiones reales contra Postgres antes de
    arreglar, y otra vez después para confirmar el fix.

    TransactionTestCase (no TestCase): TestCase envuelve el test entero en
    una única transacción compartida por todo el test — eso impediría que
    el bloqueo entre hilos ocurra de verdad (cada hilo necesita su propia
    conexión/transacción real). TransactionTestCase no envuelve nada y
    trunca las tablas al terminar cada test.
    """

    def setUp(self):
        Sede.objects.get_or_create(codigo='LURIN', defaults={'nombre': 'Planta Lurín'})
        proveedor_user = User.objects.create_user('concurrencia_test_proveedor', password='x')
        self.supplier_profile = SupplierProfile.objects.create(
            sap_card_code='concurrencia_test_proveedor', ruc='concurrencia_test_proveedor',
            user=proveedor_user,
        )
        self.po = PurchaseOrder.objects.create(
            doc_entry=888777666, doc_num=888777666, card_code='CONC', card_name='CONCURRENCIA SAC',
            e_mail='concurrencia@test.com', status='PENDIENTE', u_mss_tdb='CDL',
        )

    def test_dos_intentos_concurrentes_de_crear_factura_no_duplican_ni_pasan_ambos(self):
        resultados = {}

        def intentar(nombre, retraso_antes_de_comitear):
            try:
                with transaction.atomic():
                    po = PurchaseOrder.objects.get(pk=self.po.pk)
                    InvoicingService.validar_oc_disponible(po)
                    time.sleep(retraso_antes_de_comitear)
                    factura = Factura.objects.create(
                        proveedor=self.supplier_profile, sede=Sede.objects.get(codigo='LURIN'),
                        estado='BORRADOR',
                    )
                    FacturaOrdenCompra.objects.create(factura=factura, purchase_order=po)
                resultados[nombre] = 'OK'
            except ValidationError:
                resultados[nombre] = 'RECHAZADO'
            finally:
                connections.close_all()

        # t1 arranca primero (pequeña ventaja) y retiene la transacción
        # abierta 0.5s antes de comitear — la ventana exacta donde el bug
        # original dejaba pasar a un segundo llamador sin esperar.
        t1 = threading.Thread(target=intentar, args=('t1', 0.5))
        t2 = threading.Thread(target=intentar, args=('t2', 0.0))
        t1.start()
        time.sleep(0.05)
        t2.start()
        t1.join()
        t2.join()

        # Exactamente uno de los 2 debe pasar y el otro debe ser rechazado
        # — nunca ambos "OK" (duplicaría el candado) ni ambos "RECHAZADO"
        # (bloquearía a un llamador legítimo sin ningún motivo real).
        self.assertEqual(sorted(resultados.values()), ['OK', 'RECHAZADO'])
        self.assertEqual(
            Factura.objects.filter(ordenes_compra__purchase_order=self.po).count(), 1,
        )


class SaldoDisponibleConcurrenciaTests(TransactionTestCase):
    """
    Mismo patrón que ValidarOcDisponibleConcurrenciaTests (sesión 70),
    aplicado a saldo_disponible: 2 hilos intentan "facturar" 60 unidades
    cada uno contra una línea con 100 disponibles — si ambos pasaran sin
    esperarse, el resultado sería 120 facturado contra 100 real
    (sobre-facturación).

    Verificación pedida explícitamente (sesión 71): a diferencia de
    validar_oc_disponible (que SÍ tenía el bug — select_for_update()
    sobre un queryset vacío no bloquea nada), saldo_disponible bloquea
    primero EntradaMercaderiaLinea.objects.select_for_update().get(
    po_line=po_line) — una fila que, por precondición del propio método
    (lanza ValidationError si no existe), SIEMPRE existe antes de llegar
    ahí. Confirmado con la SQL real (FOR UPDATE sobre
    operations_entradamercaderialinea, filtrado por po_line_id — nunca
    sobre una tabla vacía) y con este mismo escenario de 2 hilos:
    verificado empíricamente, sin encontrar el mismo bug — no hizo falta
    ningún cambio de código, solo este test de regresión.

    No existe todavía ninguna función de servicio real que orqueste
    "leer saldo -> decidir -> crear FacturaLinea" (Sub-fase 3.2/3.3, sin
    construir) — este test arma esa orquestación mínima directamente
    dentro de cada hilo (el mismo patrón que cualquier endpoint futuro
    tendría que seguir: todo dentro de un único transaction.atomic()),
    para probar que el lock que YA usa saldo_disponible es suficiente
    por sí solo, sin necesitar ningún candado adicional en el futuro
    orquestador.

    TransactionTestCase, no TestCase: mismo motivo que la clase anterior
    — cada hilo necesita su propia conexión/transacción real.
    """

    def setUp(self):
        Sede.objects.get_or_create(codigo='LURIN', defaults={'nombre': 'Planta Lurín'})
        g_compras, _ = Group.objects.get_or_create(name='COMPRAS')

        self.proveedor = User.objects.create_user('saldo_conc_proveedor', password='x')
        self.supplier_profile = SupplierProfile.objects.create(
            sap_card_code='saldo_conc_proveedor', ruc='saldo_conc_proveedor', user=self.proveedor,
        )
        self.u_compras = User.objects.create_user('saldo_conc_compras', password='x')
        self.u_compras.groups.add(g_compras)
        u_vigilancia = User.objects.create_user('saldo_conc_vigilancia', password='x')
        u_almacen = User.objects.create_user('saldo_conc_almacen', password='x')

        doc_num = 777666555
        po = PurchaseOrder.objects.create(
            doc_entry=doc_num, doc_num=doc_num, card_code='SALDOCONC', card_name='SALDO CONC SAC',
            e_mail='saldoconc@test.com', status='PENDIENTE', u_mss_tdb='CDL',
        )
        self.po_line = PurchaseOrderLine.objects.create(
            purchase_order=po, line_num=1, item_code='ITEM-SALDO-CONC',
            description='Item saldo concurrencia', quantity_sap=100, und_medida='KG',
        )

        slot = AppointmentSlot.objects.create(
            sede=Sede.objects.get(codigo='LURIN'),
            date='2026-09-16', start_time='09:30', dock='TEST', max_capacity=5,
        )
        appointment = AppointmentService.solicitar_cita_borrador(
            user=self.proveedor, slot_id=slot.id, oc_ids=[po.id],
        )
        ticket = AppointmentService.confirmar_cita(
            appointment_id=appointment.id, usuario_almacen=self.u_compras,
        )
        ticket = OperationsService.iniciar_ingreso_planta(
            ticket_id=ticket.id, usuario_vigilancia=u_vigilancia,
        )
        OperationsService.autorizar_almacen(ticket_id=ticket.id, usuario=u_almacen)
        ticket.refresh_from_db()

        # Cierra el ciclo con la cantidad REAL = 100 (todo el saldo
        # disponible al arrancar el test) — mismo criterio de
        # InvoicingTestBase._finalizar_ticket (apps.operations.tests).
        resultados_insp = [
            {'inspeccion_id': insp.id, 'estado': 'CONFORME', 'cantidad_modificada': '100.0000'}
            for insp in TicketLineInspection.objects.filter(ticket=ticket, etapa='ALMACEN')
        ]
        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=u_almacen, resultados=resultados_insp,
        )
        ticket.refresh_from_db()
        OperationsService.registrar_salida(ticket_id=ticket.id, usuario_vigilancia=u_vigilancia)

    def test_dos_intentos_concurrentes_de_facturar_60_contra_saldo_100_no_sobrefacturan(self):
        resultados = {}

        def intentar(nombre, retraso_antes_de_comitear):
            try:
                with transaction.atomic():
                    po_line = PurchaseOrderLine.objects.get(pk=self.po_line.pk)
                    saldo = InvoicingService.saldo_disponible(po_line)
                    time.sleep(retraso_antes_de_comitear)
                    if Decimal('60.0000') > saldo:
                        raise ValidationError(
                            f"Saldo insuficiente: se pidió 60, disponible {saldo}."
                        )
                    factura = Factura.objects.create(
                        proveedor=self.supplier_profile, sede=Sede.objects.get(codigo='LURIN'),
                        estado='BORRADOR',
                    )
                    FacturaLinea.objects.create(
                        factura=factura, po_line=po_line,
                        cantidad_oc=po_line.quantity_sap, precio_oc=Decimal('1.0000'),
                        cantidad=Decimal('60.0000'), precio=Decimal('1.0000'),
                    )
                resultados[nombre] = 'OK'
            except ValidationError:
                resultados[nombre] = 'RECHAZADO'
            finally:
                connections.close_all()

        # t1 arranca primero y retiene la transacción abierta 0.5s antes
        # de comitear — misma ventana que el bug real de validar_oc_
        # disponible explotaba; aquí se espera que NO haya bug.
        t1 = threading.Thread(target=intentar, args=('t1', 0.5))
        t2 = threading.Thread(target=intentar, args=('t2', 0.0))
        t1.start()
        time.sleep(0.05)
        t2.start()
        t1.join()
        t2.join()

        # Exactamente uno "OK" y el otro "RECHAZADO" — nunca ambos "OK"
        # (eso sería la sobre-facturación: 120 contra 100 real).
        self.assertEqual(sorted(resultados.values()), ['OK', 'RECHAZADO'])

        total_facturado = FacturaLinea.objects.filter(po_line=self.po_line).aggregate(
            total=Sum('cantidad')
        )['total'] or Decimal('0')
        self.assertEqual(total_facturado, Decimal('60.0000'))


class NotificarFacturaObservadaTests(InvoicingTestBase):
    """
    InvoicingService.notificar_factura_observada — servicio listo para
    que la Sub-fase 3.5 lo consuma, sin ningún endpoint que lo dispare
    todavía (nadie la llama desde ningún flujo real en esta sesión).

    Mockeado (apps.base.services_correo.enviar_correo parcheado en su
    propio módulo — notificar_factura_observada hace un import local,
    mismo criterio que apps.appointments.tests) — la prueba real contra
    Graph ya se hizo manualmente para enviar_correo, no hace falta
    repetirla aquí.
    """

    @patch('apps.base.services_correo.enviar_correo')
    def test_usa_el_email_de_la_cuenta_de_portal_si_existe(self, mock_enviar):
        mock_enviar.return_value = ResultadoEnvioCorreo(enviado=True)
        self.proveedor.email = 'cuenta_portal@daryza-test.com'
        self.proveedor.save(update_fields=['email'])

        factura = self._crear_factura(estado='OBSERVADA')
        InvoicingService.notificar_factura_observada(factura, 'Falta el RUC en la línea 2.')

        mock_enviar.assert_called_once()
        _, kwargs = mock_enviar.call_args
        self.assertEqual(kwargs['destinatario'], 'cuenta_portal@daryza-test.com')
        self.assertIn('Falta el RUC en la línea 2.', kwargs['cuerpo_html'])

    @patch('apps.base.services_correo.enviar_correo')
    def test_usa_correo_electronico_del_perfil_si_no_hay_cuenta_vinculada(self, mock_enviar):
        mock_enviar.return_value = ResultadoEnvioCorreo(enviado=True)
        perfil = self._supplier_profile()
        perfil.user = None
        perfil.correo_electronico = 'sap_sync@proveedor-test.com'
        perfil.save(update_fields=['user', 'correo_electronico'])

        factura = self._crear_factura(estado='OBSERVADA', proveedor=perfil)
        InvoicingService.notificar_factura_observada(factura, 'Monto no coincide con la OC.')

        mock_enviar.assert_called_once()
        _, kwargs = mock_enviar.call_args
        self.assertEqual(kwargs['destinatario'], 'sap_sync@proveedor-test.com')

    @patch('apps.base.services_correo.enviar_correo')
    def test_fallo_de_envio_no_lanza(self, mock_enviar):
        mock_enviar.return_value = ResultadoEnvioCorreo(enviado=False, error='Graph caído.')

        factura = self._crear_factura(estado='OBSERVADA')
        resultado = InvoicingService.notificar_factura_observada(factura, 'Motivo.')

        self.assertFalse(resultado.enviado)
        self.assertEqual(resultado.error, 'Graph caído.')


# ═══════════════════════════════════════════════════════════════════════════
# Sub-fase 3.3 — InvoicingService.procesar_validacion_documentos +
# enviar_a_revision. OneDriveClient mockeado para AMBAS direcciones
# (subida vía cargar_archivo_factura, descarga vía descargar_archivo_
# factura que la orquestación usa para re-obtener el XML/CDR reales).
# ═══════════════════════════════════════════════════════════════════════════

class ProcesarValidacionDocumentosTests(InvoicingTestBase):

    def _mock_onedrive(self, mock_cls, xml_bytes: bytes, cdr_bytes: bytes):
        mock_cls.return_value.upload_documento_factura.return_value = 'https://onedrive.example.com/x'

        def _descargar(sede, ruc, identificador, nombre_archivo):
            if nombre_archivo.startswith('xml.'):
                return xml_bytes
            if nombre_archivo.startswith('cdr.'):
                return cdr_bytes
            raise AssertionError(f"Descarga inesperada de '{nombre_archivo}' en el test.")

        mock_cls.return_value.descargar_documento_factura.side_effect = _descargar

    def _cargar_los_3_archivos(self, factura, xml_bytes: bytes, cdr_bytes: bytes):
        """
        El PDF y el CDR se suben primero, y el XML AL FINAL — a propósito,
        para confirmar que el trigger de procesar_validacion_documentos
        no asume ningún orden fijo entre los 3 archivos (solo que los 3
        terminen presentes).
        """
        sa.cargar_archivo_factura(
            factura, 'pdf',
            SimpleUploadedFile('f.pdf', _leer_fixture('pdf_valido_minimo.pdf'), content_type='application/pdf'),
            self.proveedor,
        )
        sa.cargar_archivo_factura(
            factura, 'cdr',
            SimpleUploadedFile('c.xml', cdr_bytes, content_type='application/xml'),
            self.proveedor,
        )
        sa.cargar_archivo_factura(
            factura, 'xml',
            SimpleUploadedFile('f.xml', xml_bytes, content_type='application/xml'),
            self.proveedor,
        )

    @patch('apps.invoicing.services_archivos.OneDriveClient')
    def test_firma_valida_y_cdr_aceptado_puebla_los_datos_y_permite_enviar_a_revision(self, mock_cls):
        xml_bytes = _leer_fixture('factura_sintetica_firmada.xml')
        cdr_bytes = _leer_fixture('cdr_sintetico_aceptado.xml')
        self._mock_onedrive(mock_cls, xml_bytes, cdr_bytes)

        factura = self._crear_factura()
        self._cargar_los_3_archivos(factura, xml_bytes, cdr_bytes)

        factura.refresh_from_db()
        self.assertTrue(factura.firma_valida)
        self.assertEqual(factura.estado_cdr, 'ACEPTADO')
        self.assertEqual(factura.serie_comprobante, 'F001')
        self.assertEqual(factura.numero_comprobante, '123')
        self.assertEqual(factura.doc_cur, 'PEN')
        self.assertEqual(factura.importe_total_xml, Decimal('1180.50'))
        self.assertEqual(factura.moneda_xml, 'PEN')
        # Sin FacturaLinea todavía (Sub-fase 3.4, sin construir) -> nada
        # que comparar, no se marca el flag.
        self.assertFalse(factura.importe_no_coincide)
        self.assertIn('Importe: sin líneas cargadas todavía', factura.mensaje_validacion_documentos)

        InvoicingService.enviar_a_revision(factura)  # no debe lanzar
        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'EN_REVISION_COMPRAS')

    @patch('apps.invoicing.services_archivos.OneDriveClient')
    def test_trigger_no_depende_del_orden_en_que_se_completan_los_3_archivos(self, mock_cls):
        """
        _cargar_los_3_archivos ya sube pdf/cdr/xml en ese orden (el XML
        al final) — este test además confirma explícitamente que subir
        solo 1 o 2 de los 3 archivos NO dispara la población (factura.
        firma_valida sigue en None).
        """
        xml_bytes = _leer_fixture('factura_sintetica_firmada.xml')
        cdr_bytes = _leer_fixture('cdr_sintetico_aceptado.xml')
        self._mock_onedrive(mock_cls, xml_bytes, cdr_bytes)

        factura = self._crear_factura()
        sa.cargar_archivo_factura(
            factura, 'pdf',
            SimpleUploadedFile('f.pdf', _leer_fixture('pdf_valido_minimo.pdf'), content_type='application/pdf'),
            self.proveedor,
        )
        factura.refresh_from_db()
        self.assertIsNone(factura.firma_valida)  # todavía no se completaron los 3

        sa.cargar_archivo_factura(
            factura, 'cdr',
            SimpleUploadedFile('c.xml', cdr_bytes, content_type='application/xml'),
            self.proveedor,
        )
        factura.refresh_from_db()
        self.assertIsNone(factura.firma_valida)  # todavía falta el xml

        sa.cargar_archivo_factura(
            factura, 'xml',
            SimpleUploadedFile('f.xml', xml_bytes, content_type='application/xml'),
            self.proveedor,
        )
        factura.refresh_from_db()
        self.assertTrue(factura.firma_valida)  # ahora sí, los 3 están completos

    @patch('apps.invoicing.services_archivos.OneDriveClient')
    def test_descarga_con_contenido_distinto_al_hash_guardado_aborta_sin_procesar(self, mock_cls):
        """
        Verificación de integridad pedida explícitamente antes de
        producción: si lo que `descargar_documento_factura` trae de
        vuelta no coincide con el hash guardado al cargar el archivo (ej.
        la carpeta reconstruida en OneDrive ya no contiene el mismo
        contenido que se validó y aceptó en su momento), la orquestación
        debe abortar con un error claro — nunca procesar en silencio un
        archivo distinto al que el proveedor cargó.
        """
        xml_bytes = _leer_fixture('factura_sintetica_firmada.xml')
        cdr_bytes = _leer_fixture('cdr_sintetico_aceptado.xml')
        # El mock de descarga del XML devuelve un contenido DISTINTO al
        # que realmente se subió (el CDR sí coincide) — simula el
        # desajuste que la verificación de hash debe detectar.
        xml_bytes_distinto = _leer_fixture('factura_sintetica_alterada.xml')
        self._mock_onedrive(mock_cls, xml_bytes_distinto, cdr_bytes)

        factura = self._crear_factura()
        # Se suben los bytes REALES (xml_bytes) — el mock de descarga
        # devolverá xml_bytes_distinto, provocando el desajuste de hash.
        sa.cargar_archivo_factura(
            factura, 'pdf',
            SimpleUploadedFile('f.pdf', _leer_fixture('pdf_valido_minimo.pdf'), content_type='application/pdf'),
            self.proveedor,
        )
        sa.cargar_archivo_factura(
            factura, 'cdr',
            SimpleUploadedFile('c.xml', cdr_bytes, content_type='application/xml'),
            self.proveedor,
        )
        with self.assertRaises(ValidationError) as ctx:
            sa.cargar_archivo_factura(
                factura, 'xml',
                SimpleUploadedFile('f.xml', xml_bytes, content_type='application/xml'),
                self.proveedor,
            )
        self.assertIn('no coincide con el hash guardado', str(ctx.exception))

        # El archivo SÍ quedó guardado (cargar_archivo_factura ya había
        # terminado su propio guardado antes de disparar la orquestación
        # que falló) — pero ningún dato de negocio derivado del contenido
        # se pobló, ya que la orquestación abortó antes de tocarlos.
        factura.refresh_from_db()
        self.assertTrue(factura.xml_file)
        self.assertIsNone(factura.firma_valida)
        self.assertIsNone(factura.estado_cdr)

    @patch('apps.invoicing.services_archivos.OneDriveClient')
    def test_firma_invalida_real_bloquea_el_envio_a_revision(self, mock_cls):
        """
        Caso de firma inválida REAL (no simulado con "sin firma" o un
        booleano mockeado): factura_sintetica_alterada.xml es el mismo
        comprobante firmado con PayableAmount modificado DESPUÉS de
        firmar — xmlsec ejecuta la verificación criptográfica real
        contra el certificado embebido y falla de verdad, el mismo
        mecanismo que detectaría a un proveedor intentando inflar un
        monto ya firmado (mismo fixture ya usado en la Sub-fase 3.0 a
        nivel de services_validacion; aquí se ejercita a través de todo
        el pipeline: carga -> descarga -> validación -> bloqueo).
        """
        xml_bytes = _leer_fixture('factura_sintetica_alterada.xml')
        cdr_bytes = _leer_fixture('cdr_sintetico_aceptado.xml')
        self._mock_onedrive(mock_cls, xml_bytes, cdr_bytes)

        factura = self._crear_factura()
        self._cargar_los_3_archivos(factura, xml_bytes, cdr_bytes)

        factura.refresh_from_db()
        self.assertFalse(factura.firma_valida)
        self.assertIn('INVÁLIDA', factura.mensaje_validacion_documentos)

        with self.assertRaises(ValidationError) as cm:
            InvoicingService.enviar_a_revision(factura)
        self.assertIn('firma digital', str(cm.exception))

        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'BORRADOR')  # no avanzó

    @patch('apps.invoicing.services_archivos.OneDriveClient')
    def test_cdr_rechazado_bloquea_el_envio_a_revision(self, mock_cls):
        xml_bytes = _leer_fixture('factura_sintetica_firmada.xml')
        cdr_bytes = _leer_fixture('cdr_sintetico_rechazado.xml')
        self._mock_onedrive(mock_cls, xml_bytes, cdr_bytes)

        factura = self._crear_factura()
        self._cargar_los_3_archivos(factura, xml_bytes, cdr_bytes)

        factura.refresh_from_db()
        self.assertTrue(factura.firma_valida)  # la firma sí es válida — el bloqueo es solo por el CDR
        self.assertEqual(factura.estado_cdr, 'RECHAZADO')

        with self.assertRaises(ValidationError) as cm:
            InvoicingService.enviar_a_revision(factura)
        self.assertIn('RECHAZADO', str(cm.exception))

        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'BORRADOR')

    @patch('apps.invoicing.services_archivos.OneDriveClient')
    def test_importe_no_coincide_marca_el_flag_y_bloquea_el_envio_a_revision(self, mock_cls):
        """
        Decisión de negocio confirmada explícitamente por el usuario:
        importe_no_coincide SÍ bloquea el envío a revisión, no solo
        marca el flag para que Compras lo vea después.
        """
        xml_bytes = _leer_fixture('factura_sintetica_firmada.xml')  # PayableAmount = 1180.50 PEN
        cdr_bytes = _leer_fixture('cdr_sintetico_aceptado.xml')
        self._mock_onedrive(mock_cls, xml_bytes, cdr_bytes)

        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)
        factura = self._crear_factura()
        # 10 * 50 = 500.00 — muy lejos de los 1180.50 declarados en el XML.
        self._crear_factura_linea(factura, po_line, cantidad=Decimal('10.0000'), precio=Decimal('50.0000'))

        self._cargar_los_3_archivos(factura, xml_bytes, cdr_bytes)

        factura.refresh_from_db()
        self.assertTrue(factura.firma_valida)
        self.assertEqual(factura.estado_cdr, 'ACEPTADO')
        self.assertTrue(factura.importe_no_coincide)
        self.assertIn('NO COINCIDE', factura.mensaje_validacion_documentos)

        with self.assertRaises(ValidationError) as cm:
            InvoicingService.enviar_a_revision(factura)
        self.assertIn('importe', str(cm.exception).lower())

        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'BORRADOR')

    @patch('apps.invoicing.services_archivos.OneDriveClient')
    def test_importe_coincidente_dentro_de_tolerancia_no_marca_el_flag(self, mock_cls):
        xml_bytes = _leer_fixture('factura_sintetica_firmada.xml')  # PayableAmount = 1180.50 PEN
        cdr_bytes = _leer_fixture('cdr_sintetico_aceptado.xml')
        self._mock_onedrive(mock_cls, xml_bytes, cdr_bytes)

        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)
        factura = self._crear_factura()
        # 10 * 118.05 = 1180.50 exacto.
        self._crear_factura_linea(factura, po_line, cantidad=Decimal('10.0000'), precio=Decimal('118.05'))

        self._cargar_los_3_archivos(factura, xml_bytes, cdr_bytes)

        factura.refresh_from_db()
        self.assertFalse(factura.importe_no_coincide)

        InvoicingService.enviar_a_revision(factura)  # no debe lanzar
        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'EN_REVISION_COMPRAS')

    def test_enviar_a_revision_rechaza_si_estado_no_es_borrador_ni_observada(self):
        factura = self._crear_factura(estado='EN_REVISION_COMPRAS')
        with self.assertRaises(ValidationError):
            InvoicingService.enviar_a_revision(factura)

    def test_enviar_a_revision_sin_archivos_completados_bloquea(self):
        """
        firma_valida sigue en None (nunca se completaron los 3 archivos)
        -> `is not True` lo trata igual que False, bloquea con el mismo
        mensaje.
        """
        factura = self._crear_factura()
        with self.assertRaises(ValidationError) as cm:
            InvoicingService.enviar_a_revision(factura)
        self.assertIn('firma digital', str(cm.exception))


# ═══════════════════════════════════════════════════════════════════════════
# Carga segura de archivos (services_archivos.py) — punto 5: casos
# adversariales, no solo felices.
# ═══════════════════════════════════════════════════════════════════════════

class CargarArchivoFacturaTests(InvoicingTestBase):
    """
    cargar_archivo_factura — reutiliza services_validacion._parse_xml_
    seguro (sesión 59, protección XXE ya validada ahí) sin duplicarla.
    OneDriveClient mockeado en la mayoría de los tests (la subida real
    solo importa en el camino feliz; los casos adversariales rechazan
    ANTES de llegar a OneDrive, así que ni siquiera lo invocan) — una
    verificación manual real contra OneDrive se hizo aparte, no repetida
    en cada corrida de `manage.py test`.
    """

    def _factura_borrador(self, estado='BORRADOR', **extra):
        return self._crear_factura(estado=estado, **extra)

    @patch('apps.invoicing.services_archivos.OneDriveClient')
    def test_xml_valido_se_carga_y_calcula_hash_sha256(self, mock_cls):
        mock_cls.return_value.upload_documento_factura.return_value = (
            'https://onedrive.example.com/factura/xml'
        )
        factura = self._factura_borrador()
        xml_valido = _leer_fixture('factura_sintetica_firmada.xml')
        archivo = SimpleUploadedFile('factura.xml', xml_valido, content_type='application/xml')

        sa.cargar_archivo_factura(factura, 'xml', archivo, self.proveedor)

        factura.refresh_from_db()
        self.assertEqual(factura.xml_file, 'https://onedrive.example.com/factura/xml')
        self.assertEqual(factura.hash_xml, hashlib.sha256(xml_valido).hexdigest())

    @patch('apps.invoicing.services_archivos.OneDriveClient')
    def test_pdf_valido_se_carga_y_calcula_hash_sha256(self, mock_cls):
        mock_cls.return_value.upload_documento_factura.return_value = (
            'https://onedrive.example.com/factura/pdf'
        )
        factura = self._factura_borrador()
        # PDF ESTRUCTURALMENTE válido (no solo con la firma correcta) —
        # desde que se agregó la validación con pypdf (cierre de la
        # Sub-fase 3.2), un PDF fabricado a mano con solo la firma ya no
        # basta para pasar el camino feliz.
        pdf_valido = _leer_fixture('pdf_valido_minimo.pdf')
        archivo = SimpleUploadedFile('factura.pdf', pdf_valido, content_type='application/pdf')

        sa.cargar_archivo_factura(factura, 'pdf', archivo, self.proveedor)

        factura.refresh_from_db()
        self.assertEqual(factura.pdf_file, 'https://onedrive.example.com/factura/pdf')
        self.assertEqual(factura.hash_pdf, hashlib.sha256(pdf_valido).hexdigest())

    def test_xml_malformado_rechaza_sin_guardar_nada(self):
        """XML mal formado — debe rechazar sin guardar nada (punto 5)."""
        factura = self._factura_borrador()
        archivo = SimpleUploadedFile(
            'factura.xml', b'<Invoice><etiqueta_sin_cerrar>', content_type='application/xml',
        )

        with self.assertRaises(ValidationError):
            sa.cargar_archivo_factura(factura, 'xml', archivo, self.proveedor)

        factura.refresh_from_db()
        self.assertEqual(factura.xml_file, '')
        self.assertEqual(factura.hash_xml, '')

    def test_exe_renombrado_a_xml_rechaza_por_contenido_real(self):
        """.exe renombrado a .xml — debe rechazar por tipo real, no por extensión (punto 5)."""
        factura = self._factura_borrador()
        cabecera_pe = b'MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00' + b'\x00' * 100
        archivo = SimpleUploadedFile('factura.xml', cabecera_pe, content_type='application/xml')

        with self.assertRaises(ValidationError) as cm:
            sa.cargar_archivo_factura(factura, 'xml', archivo, self.proveedor)
        self.assertIn('XML válido', str(cm.exception))

        factura.refresh_from_db()
        self.assertEqual(factura.xml_file, '')

    def test_exe_renombrado_a_pdf_rechaza_por_firma_de_archivo(self):
        """Mismo caso adversarial, aplicado a PDF (firma b'%PDF-' ausente)."""
        factura = self._factura_borrador()
        cabecera_pe = b'MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00' + b'\x00' * 100
        archivo = SimpleUploadedFile('factura.pdf', cabecera_pe, content_type='application/pdf')

        with self.assertRaises(ValidationError) as cm:
            sa.cargar_archivo_factura(factura, 'pdf', archivo, self.proveedor)
        self.assertIn('PDF válido', str(cm.exception))

    def test_pdf_truncado_a_la_mitad_rechaza_pese_a_firma_valida(self):
        """
        Firma b'%PDF-' correcta al inicio, pero el archivo está cortado
        a la mitad (corrupto) — antes del cierre de la Sub-fase 3.2 esto
        pasaba la validación (solo se revisaba la firma); ahora pypdf
        detecta que la estructura no cierra correctamente y rechaza.
        """
        factura = self._factura_borrador()
        pdf_completo = _leer_fixture('pdf_valido_minimo.pdf')
        pdf_truncado = pdf_completo[: len(pdf_completo) // 2]
        self.assertTrue(pdf_truncado.startswith(b'%PDF-'))  # la firma SÍ sigue siendo válida

        archivo = SimpleUploadedFile('factura.pdf', pdf_truncado, content_type='application/pdf')

        with self.assertRaises(ValidationError) as cm:
            sa.cargar_archivo_factura(factura, 'pdf', archivo, self.proveedor)
        self.assertIn('corrupto', str(cm.exception))

        factura.refresh_from_db()
        self.assertEqual(factura.pdf_file, '')
        self.assertEqual(factura.hash_pdf, '')

    def test_archivo_de_50mb_rechaza_por_tamano(self):
        """Archivo de 50MB — debe rechazar por tamaño (límite 10MB, punto 5)."""
        factura = self._factura_borrador()
        contenido_grande = b'%PDF-' + b'0' * (50 * 1024 * 1024)
        archivo = SimpleUploadedFile('factura.pdf', contenido_grande, content_type='application/pdf')

        with self.assertRaises(ValidationError) as cm:
            sa.cargar_archivo_factura(factura, 'pdf', archivo, self.proveedor)
        self.assertIn('tamaño máximo', str(cm.exception))

    @patch('apps.invoicing.services_archivos.OneDriveClient')
    def test_intento_de_xxe_explicito_no_filtra_ningun_dato(self, mock_cls):
        """
        Intento de XXE explícito — mismo payload ya probado contra
        services_validacion (sesión 59). Confirmado ahí (test_xxe_no_
        resuelve_entidad_externa): con resolve_entities=False, lxml NO
        resuelve la entidad pero TAMPOCO lanza — el parseo es válido, la
        entidad simplemente queda sin expandir. Por eso esta carga NO se
        "rechaza" en el sentido de una excepción (reutiliza el servicio
        de validación tal cual, sin duplicarlo ni cambiar su
        comportamiento ya validado) — lo que sí se confirma aquí es que
        el contenido subido a OneDrive es el XML tal cual, con &xxe; sin
        resolver, nunca el contenido real de /etc/passwd.
        """
        mock_cls.return_value.upload_documento_factura.return_value = 'https://onedrive.example.com/x'
        factura = self._factura_borrador()
        malicioso = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE Invoice [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>'
            b'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2">'
            b'<cbc:ID xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">'
            b'&xxe;</cbc:ID></Invoice>'
        )
        archivo = SimpleUploadedFile('factura.xml', malicioso, content_type='application/xml')

        sa.cargar_archivo_factura(factura, 'xml', archivo, self.proveedor)

        contenido_subido = mock_cls.return_value.upload_documento_factura.call_args.kwargs['contenido']
        self.assertNotIn(b'root:', contenido_subido)  # /etc/passwd real empieza con esto
        self.assertIn(b'&xxe;', contenido_subido)  # la entidad quedó sin resolver, tal cual

    # ── Candado de servicio (punto 4): dueño + estado ───────────────────────

    @patch('apps.invoicing.services_archivos.OneDriveClient')
    def test_proveedor_no_dueno_rechaza(self, mock_cls):
        factura = self._factura_borrador()
        otro_proveedor = User.objects.create_user('otro_proveedor_test', password='x')
        archivo = SimpleUploadedFile('factura.pdf', b'%PDF-1.4', content_type='application/pdf')

        with self.assertRaises(ValidationError) as cm:
            sa.cargar_archivo_factura(factura, 'pdf', archivo, otro_proveedor)
        self.assertIn('dueño', str(cm.exception))
        mock_cls.return_value.upload_documento_factura.assert_not_called()

    def test_en_revision_compras_bloquea_la_carga(self):
        factura = self._factura_borrador(estado='EN_REVISION_COMPRAS')
        archivo = SimpleUploadedFile('factura.pdf', b'%PDF-1.4', content_type='application/pdf')

        with self.assertRaises(ValidationError) as cm:
            sa.cargar_archivo_factura(factura, 'pdf', archivo, self.proveedor)
        self.assertIn('En Revisión', str(cm.exception))

    def test_aprobada_compras_bloquea_la_carga(self):
        factura = self._factura_borrador(estado='APROBADA_COMPRAS')
        archivo = SimpleUploadedFile('factura.pdf', b'%PDF-1.4', content_type='application/pdf')

        with self.assertRaises(ValidationError):
            sa.cargar_archivo_factura(factura, 'pdf', archivo, self.proveedor)

    @patch('apps.invoicing.services_archivos.OneDriveClient')
    def test_observada_reabre_la_posibilidad_de_recargar(self, mock_cls):
        """Si Compras observa la Factura, se reabre la carga (punto 4)."""
        mock_cls.return_value.upload_documento_factura.return_value = 'https://onedrive.example.com/x'
        factura = self._factura_borrador(estado='OBSERVADA')
        archivo = SimpleUploadedFile(
            'factura.pdf', _leer_fixture('pdf_valido_minimo.pdf'), content_type='application/pdf',
        )

        sa.cargar_archivo_factura(factura, 'pdf', archivo, self.proveedor)  # no debe lanzar

        factura.refresh_from_db()
        self.assertEqual(factura.pdf_file, 'https://onedrive.example.com/x')

    def test_tipo_desconocido_rechaza(self):
        factura = self._factura_borrador()
        archivo = SimpleUploadedFile('factura.pdf', b'%PDF-1.4', content_type='application/pdf')

        with self.assertRaises(ValidationError):
            sa.cargar_archivo_factura(factura, 'formato_inventado', archivo, self.proveedor)


class CargarArchivoFacturaLineaTests(InvoicingTestBase):

    def _linea_con_retencion(self, ticket_u_mss_tdb='CDL'):
        ticket = self._finalizar_ticket(ticket_u_mss_tdb, cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)
        factura = self._crear_factura(estado='BORRADOR')
        return self._crear_factura_linea(
            factura, po_line, cantidad=Decimal('10.0000'), aplica_retencion=True,
        )

    @patch('apps.invoicing.services_archivos.OneDriveClient')
    def test_documento_retencion_valido_se_carga_y_calcula_hash(self, mock_cls):
        mock_cls.return_value.upload_documento_factura.return_value = (
            'https://onedrive.example.com/retencion'
        )
        linea = self._linea_con_retencion()
        pdf_valido = _leer_fixture('pdf_valido_minimo.pdf')
        archivo = SimpleUploadedFile('retencion.pdf', pdf_valido, content_type='application/pdf')

        sa.cargar_archivo_factura_linea(linea, 'retencion', archivo, self.proveedor)

        linea.refresh_from_db()
        self.assertEqual(linea.documento_retencion, 'https://onedrive.example.com/retencion')
        self.assertEqual(linea.hash_documento_retencion, hashlib.sha256(pdf_valido).hexdigest())

    def test_retencion_sin_aplica_retencion_rechaza(self):
        """No tiene sentido cargar un documento de retención si la línea no la aplica."""
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)
        factura = self._crear_factura(estado='BORRADOR')
        linea = self._crear_factura_linea(
            factura, po_line, cantidad=Decimal('10.0000'), aplica_retencion=False,
        )
        archivo = SimpleUploadedFile('retencion.pdf', b'%PDF-1.4', content_type='application/pdf')

        with self.assertRaises(ValidationError) as cm:
            sa.cargar_archivo_factura_linea(linea, 'retencion', archivo, self.proveedor)
        self.assertIn('aplica_retencion', str(cm.exception))

    def test_exe_renombrado_rechaza_tambien_a_nivel_de_linea(self):
        linea = self._linea_con_retencion()
        cabecera_pe = b'MZ\x90\x00\x03\x00\x00\x00' + b'\x00' * 50
        archivo = SimpleUploadedFile('retencion.pdf', cabecera_pe, content_type='application/pdf')

        with self.assertRaises(ValidationError):
            sa.cargar_archivo_factura_linea(linea, 'retencion', archivo, self.proveedor)


class SubirArchivoFacturaEndpointTests(InvoicingTestBase):
    """Vistas HTTP reales (apps/invoicing/views.py) — no solo el servicio."""

    @patch('apps.invoicing.services_archivos.OneDriveClient')
    def test_endpoint_real_carga_xml_correctamente(self, mock_cls):
        mock_cls.return_value.upload_documento_factura.return_value = 'https://onedrive.example.com/x'
        factura = self._crear_factura(estado='BORRADOR')
        xml_valido = _leer_fixture('factura_sintetica_firmada.xml')

        self.client.force_login(self.proveedor)
        resp = self.client.post(
            f'/invoicing/factura/{factura.id}/archivo/xml/',
            data={'archivo': SimpleUploadedFile('f.xml', xml_valido, content_type='application/xml')},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['status'], 'success')
        factura.refresh_from_db()
        self.assertEqual(factura.xml_file, 'https://onedrive.example.com/x')

    def test_endpoint_real_rechaza_xml_malformado_con_400(self):
        factura = self._crear_factura(estado='BORRADOR')

        self.client.force_login(self.proveedor)
        resp = self.client.post(
            f'/invoicing/factura/{factura.id}/archivo/xml/',
            data={'archivo': SimpleUploadedFile(
                'f.xml', b'<no_cierra>', content_type='application/xml',
            )},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['status'], 'error')

    def test_endpoint_real_404_para_proveedor_no_dueno(self):
        factura = self._crear_factura(estado='BORRADOR')
        otro_proveedor = User.objects.create_user('endpoint_otro_proveedor', password='x')
        otro_proveedor.groups.add(self.g_proveedores)  # necesita pasar @proveedor_required

        self.client.force_login(otro_proveedor)
        resp = self.client.post(
            f'/invoicing/factura/{factura.id}/archivo/xml/',
            data={'archivo': SimpleUploadedFile(
                'f.xml', b'<a/>', content_type='application/xml',
            )},
        )

        # 404, no 403/400: no revela si la Factura existe a alguien que
        # no es su dueño (mismo criterio ya establecido en
        # subir_coa_linea_ajax, apps/appointments/views.py).
        self.assertEqual(resp.status_code, 404)

    def test_endpoint_rechaza_sin_archivo_adjunto(self):
        factura = self._crear_factura(estado='BORRADOR')

        self.client.force_login(self.proveedor)
        resp = self.client.post(f'/invoicing/factura/{factura.id}/archivo/xml/', data={})

        self.assertEqual(resp.status_code, 400)
