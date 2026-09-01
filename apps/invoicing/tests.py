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

  4. Sub-fase 3.3: InvoicingService.procesar_validacion_documentos
     (dispara automáticamente validar_firma_xml/extraer_datos_factura/
     extraer_estado_cdr en cuanto los 3 archivos de una Factura están
     completos) + enviar_a_revision (candado que bloquea el avance a
     EN_REVISION_COMPRAS por firma inválida, CDR rechazado, o importe
     del XML que no coincide con la suma de FacturaLinea).

  5. Sub-fase 3.4: services_borrador.py — el patrón "Copiar de OC(s)"
     completo (listar_ocs_elegibles/lineas_para_copiar/crear_factura_
     desde_ocs/editar_cabecera_factura/editar_linea_factura), más un
     flujo HTTP de punta a punta y una regresión de concurrencia real
     (TransactionTestCase, mismo patrón de las sesiones 70/71) contra
     crear_factura_desde_ocs — no solo contra validar_oc_disponible
     aislado, que ya se probó entonces.

  6. Sub-fase 3.5: el lado de Compras (aprobar_factura/observar_factura),
     candado de rol en el servicio, historial de FacturaObservacion, y
     el ciclo HTTP completo observar -> corregir -> reenviar -> aprobar.

  7. Sub-fase 3.6: endpoints del daemon SAP para Factura (api/factura_
     api.py) — idempotencia de cada endpoint (mismo patrón ya probado
     para OC/EntradaMercaderia), el interruptor FACTURA_DRAFT_SAP_
     HABILITADO vaciando las listas de pendientes (y bloqueando también
     las acciones de confirmación), el candado explícito de
     estado_sap='Y' bloqueando edición incluso con un `estado` de
     negocio que en teoría lo permitiría, y el flujo completo simulado
     de punta a punta (aprobada -> preliminar -> reconciliación ->
     definitivo).

  8. Sub-fase 3.7 (esta sesión) — apps.base.oc_status: estado de
     FACTURACIÓN de la columna del Panel de Consulta de OC (cierre de la
     Fase 3 completa). Los 3 estados (SIN_FACTURAR/FACTURACION_EN_CURSO/
     FACTURADA) contra datos reales, y verificación de ausencia de N+1
     con CaptureQueriesContext (no había precedente de este patrón en el
     proyecto — es la herramienta estándar de Django para esto) — que
     además encontró y permitió corregir un N+1 real preexistente desde
     la sesión 52 en calcular_estado_despacho (ver su docstring).
"""
import hashlib
import json
import os
import threading
import time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, connections, transaction
from django.db.models import Sum
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.appointments.models import AppointmentSlot
from apps.appointments.services import AppointmentService
from apps.base import oc_status
from apps.base.models import Sede, SupplierProfile
from apps.base.services_correo import ResultadoEnvioCorreo
from apps.invoicing import services_validacion as sv
from apps.operations.models import EntradaMercaderia, EntradaMercaderiaLinea, Ticket, TicketLineInspection
from apps.operations.services import OperationsService
from apps.operations.tests import OperationsTestBase
from apps.operations.tests import _doc_num_counter
from apps.sap_sync.models import PurchaseOrder, PurchaseOrderLine

from . import services_archivos as sa
from . import services_borrador as sb
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

        # Sesión 99/99b: la EntradaMercaderia nace PENDIENTE; Almacén/MP la
        # envía ("Enviar a SAP B1"). Sesión 99c: para que la OC sea
        # facturable (listar_ocs_elegibles) hace falta además que el
        # daemon YA haya creado el GRPO en SAP (estado=CREADO_SAP). Acá se
        # simula ese paso del daemon fijando el estado directo — mismo
        # atajo que _factura_aprobada_con_grpo (Sub-fase 3.6).
        from apps.operations import services_entrada as _se
        entrada = EntradaMercaderia.objects.get(ticket=ticket)
        _actor = self.u_materia_prima if ticket.es_materia_prima else self.u_almacen
        _se.enviar_a_sap(entrada, _actor)
        entrada.refresh_from_db()
        entrada.estado = EntradaMercaderia.ESTADO_CREADO_SAP
        entrada.estado_sap = 'Y'
        entrada.doc_entry_definitivo = 900000 + entrada.id
        entrada.doc_num_sap = str(500000 + entrada.id)
        entrada.save(update_fields=['estado', 'estado_sap', 'doc_entry_definitivo', 'doc_num_sap'])
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

    def test_solo_las_facturas_en_vuelo_bloquean(self):
        """
        Sesión 99c (OC abierta con N despachos parciales): BORRADOR /
        EN_REVISION_COMPRAS / OBSERVADA bloquean (una Factura en vuelo a la
        vez por OC). APROBADA_COMPRAS NO bloquea — una OC abierta se
        factura en varias APROBADA_COMPRAS parciales a lo largo del tiempo.
        """
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()

        for estado in ('BORRADOR', 'EN_REVISION_COMPRAS', 'OBSERVADA'):
            with self.subTest(estado=estado):
                factura = self._crear_factura(estado=estado)
                FacturaOrdenCompra.objects.create(factura=factura, purchase_order=po)
                with self.assertRaises(ValidationError):
                    InvoicingService.validar_oc_disponible(po)
                factura.delete()

        factura_aprobada = self._crear_factura(estado='APROBADA_COMPRAS')
        FacturaOrdenCompra.objects.create(factura=factura_aprobada, purchase_order=po)
        InvoicingService.validar_oc_disponible(po)  # NO debe lanzar

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

    def test_precio_distinto_ya_no_activa_difiere_de_oc(self):
        """
        Sesión 92: precio dejó de participar en esta comparación —
        precio/precio_oc son siempre iguales por construcción para
        cualquier línea creada desde services_borrador.py en adelante,
        pero este test fuerza el caso igual (construyendo la línea
        directo, sin pasar por ese servicio) para confirmar que
        difiere_de_oc de verdad ignora precio, no solo que "nunca
        ocurre en la práctica".
        """
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)
        factura = self._crear_factura()

        linea = self._crear_factura_linea(
            factura, po_line, cantidad=Decimal('10.0000'),
            cantidad_oc=Decimal('10.0000'), precio_oc=Decimal('5.0000'), precio=Decimal('5.5000'),
        )
        self.assertFalse(linea.difiere_de_oc)


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
            base_url='http://testserver/',
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

        # Sesión 99c: saldo_disponible solo cuenta rondas CREADO_SAP. Este
        # setUp quedó sin actualizar cuando se hizo ese cambio (helper de
        # test — no producción); se simula el paso del daemon para que
        # saldo=100 al arrancar el test, mismo atajo que
        # InvoicingTestBase._finalizar_ticket.
        _e = EntradaMercaderia.objects.get(ticket=ticket)
        _e.estado = EntradaMercaderia.ESTADO_CREADO_SAP
        _e.estado_sap = 'Y'
        _e.save(update_fields=['estado', 'estado_sap'])

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
        # Sesión 93: doc_cur ya NO se sobreescribe con datos_factura.
        # moneda — se fija acá explícitamente, imitando lo que
        # _crear_factura_y_lineas ya hace siempre en la vida real
        # (heredarlo de la OC al crear), para poder verificar que se
        # compara contra el XML (que también declara 'PEN') Y que el
        # valor queda intacto después de procesar.
        factura.doc_cur = 'PEN'
        factura.save(update_fields=['doc_cur'])
        self._cargar_los_3_archivos(factura, xml_bytes, cdr_bytes)

        factura.refresh_from_db()
        self.assertTrue(factura.firma_valida)
        self.assertEqual(factura.estado_cdr, 'ACEPTADO')
        self.assertEqual(factura.serie_comprobante, 'F001')
        self.assertEqual(factura.numero_comprobante, '123')
        self.assertEqual(factura.doc_cur, 'PEN')  # intacto, no lo tocó el procesamiento
        self.assertEqual(factura.importe_total_xml, Decimal('1180.50'))
        self.assertEqual(factura.moneda_xml, 'PEN')
        self.assertFalse(factura.moneda_no_coincide)
        self.assertIn('Moneda: coincide', factura.mensaje_validacion_documentos)
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
    def test_moneda_no_coincide_bloquea_el_envio_a_revision(self, mock_cls):
        """
        Sesión 93, punto 3: la Factura con doc_cur heredado de una OC en
        una moneda distinta a la que el XML real declara (la fixture
        'factura_sintetica_firmada.xml' está en PEN) debe bloquear el
        envío a revisión con un mensaje claro — mismo tratamiento que
        firma inválida/CDR rechazado/importe no coincide. Sin ninguna
        conversión: solo detección y bloqueo (instrucción explícita).
        """
        xml_bytes = _leer_fixture('factura_sintetica_firmada.xml')  # PayableAmount en PEN
        cdr_bytes = _leer_fixture('cdr_sintetico_aceptado.xml')
        self._mock_onedrive(mock_cls, xml_bytes, cdr_bytes)

        factura = self._crear_factura()
        factura.doc_cur = 'USD'  # la OC real de esta Factura está en USD
        factura.save(update_fields=['doc_cur'])
        self._cargar_los_3_archivos(factura, xml_bytes, cdr_bytes)

        factura.refresh_from_db()
        self.assertTrue(factura.firma_valida)  # la firma sí es válida — el bloqueo es solo por la moneda
        self.assertEqual(factura.estado_cdr, 'ACEPTADO')
        self.assertEqual(factura.moneda_xml, 'PEN')
        self.assertEqual(factura.doc_cur, 'USD')  # intacto, no lo pisó el XML
        self.assertTrue(factura.moneda_no_coincide)
        self.assertIn('Moneda: NO COINCIDE', factura.mensaje_validacion_documentos)

        with self.assertRaises(ValidationError) as cm:
            InvoicingService.enviar_a_revision(factura)
        self.assertIn('moneda', str(cm.exception).lower())

        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'BORRADOR')  # no avanzó

    @patch('apps.invoicing.services_archivos.OneDriveClient')
    def test_moneda_sin_doc_cur_en_la_oc_no_bloquea_por_moneda(self, mock_cls):
        """
        Una Factura cuya OC todavía no tiene doc_cur poblado (blank/None
        — dato transitorio, self-healing en el próximo resync real,
        mismo criterio ya usado para otros campos de PurchaseOrderLine)
        no debe bloquear por moneda — no hay nada real contra qué
        comparar, así que no se marca ningún falso positivo.
        """
        xml_bytes = _leer_fixture('factura_sintetica_firmada.xml')
        cdr_bytes = _leer_fixture('cdr_sintetico_aceptado.xml')
        self._mock_onedrive(mock_cls, xml_bytes, cdr_bytes)

        factura = self._crear_factura()  # doc_cur queda en None (default del helper)
        self._cargar_los_3_archivos(factura, xml_bytes, cdr_bytes)

        factura.refresh_from_db()
        self.assertIsNone(factura.doc_cur)
        self.assertFalse(factura.moneda_no_coincide)
        self.assertIn('Moneda: sin datos suficientes', factura.mensaje_validacion_documentos)

        InvoicingService.enviar_a_revision(factura)  # no debe lanzar por moneda
        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'EN_REVISION_COMPRAS')

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
        # 10 * 118.05 = 1180.50 exacto, igual al PayableAmount del XML.
        # tax_code='IGV_EXE' a propósito (sesión 92): este test verifica
        # la TOLERANCIA de comparación, no el cálculo de IGV en sí (ese
        # tiene su propio test dedicado, ComparacionImporteConIGVTests)
        # — con la línea exonerada, el importe neto ya coincide 1:1 sin
        # sumar nada, preservando los números redondos originales.
        self._crear_factura_linea(
            factura, po_line, cantidad=Decimal('10.0000'), precio=Decimal('118.05'),
            tax_code=PurchaseOrderLine.TAX_CODE_IGV_EXE,
        )

        self._cargar_los_3_archivos(factura, xml_bytes, cdr_bytes)

        factura.refresh_from_db()
        self.assertFalse(factura.importe_no_coincide)

        InvoicingService.enviar_a_revision(factura)  # no debe lanzar
        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'EN_REVISION_COMPRAS')

    @patch('apps.invoicing.services_archivos.OneDriveClient')
    def test_linea_gravada_suma_igv_18_porciento_y_coincide(self, mock_cls):
        """
        Sesión 92, punto 7: caso GRAVADO real — el importe del XML
        (1180.50, con IGV incluido, como trae una factura real) debe
        coincidir contra (línea neta + IGV 18%), NO contra la línea
        neta sola. neto=1000.4237 (cantidad=1 * precio=1000.4237,
        tax_code='IGV' default) -> +18% = 1180.4999...66, dentro de la
        tolerancia de 0.02 contra 1180.50.
        """
        xml_bytes = _leer_fixture('factura_sintetica_firmada.xml')  # PayableAmount = 1180.50 PEN
        cdr_bytes = _leer_fixture('cdr_sintetico_aceptado.xml')
        self._mock_onedrive(mock_cls, xml_bytes, cdr_bytes)

        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)
        factura = self._crear_factura()
        # tax_code por defecto = 'IGV' (gravado, ver PurchaseOrderLine).
        self._crear_factura_linea(
            factura, po_line, cantidad=Decimal('1.0000'), precio=Decimal('1000.4237'),
        )

        self._cargar_los_3_archivos(factura, xml_bytes, cdr_bytes)

        factura.refresh_from_db()
        self.assertFalse(factura.importe_no_coincide)
        self.assertIn('IGV', factura.mensaje_validacion_documentos)

    @patch('apps.invoicing.services_archivos.OneDriveClient')
    def test_comparar_solo_contra_neto_sin_igv_hubiera_sido_incorrecto(self, mock_cls):
        """
        Confirma explícitamente el bug que existía antes de la sesión
        92 (comparaba importe_total_xml directo contra la suma neta,
        sin sumar IGV): una línea GRAVADA cuyo NETO ya es igual al
        PayableAmount del XML (1180.50) — bajo la comparación VIEJA
        esto habría marcado "coincide" incorrectamente; con el IGV
        sumado (total real ~1392.99), la comparación NUEVA correctamente
        lo marca como NO coincide.
        """
        xml_bytes = _leer_fixture('factura_sintetica_firmada.xml')  # PayableAmount = 1180.50 PEN
        cdr_bytes = _leer_fixture('cdr_sintetico_aceptado.xml')
        self._mock_onedrive(mock_cls, xml_bytes, cdr_bytes)

        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)
        factura = self._crear_factura()
        # 10 * 118.05 = 1180.50 NETO exacto, tax_code='IGV' (gravado) ->
        # con IGV sumado, el total real es 1180.50 * 1.18 = 1392.99.
        self._crear_factura_linea(
            factura, po_line, cantidad=Decimal('10.0000'), precio=Decimal('118.05'),
        )

        self._cargar_los_3_archivos(factura, xml_bytes, cdr_bytes)

        factura.refresh_from_db()
        self.assertTrue(factura.importe_no_coincide)
        self.assertIn('NO COINCIDE', factura.mensaje_validacion_documentos)
        self.assertIn('IGV', factura.mensaje_validacion_documentos)

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


# ═══════════════════════════════════════════════════════════════════════════
# Sub-fase 3.4 (esta sesión) — "Copiar de OC(s)": services_borrador.py
# ═══════════════════════════════════════════════════════════════════════════

class NuevaFacturaTestBase(InvoicingTestBase):
    """
    Extiende InvoicingTestBase con un SupplierProfile cuyo sap_card_code
    coincide con el card_code FIJO ('TESTCODE') que OperationsTestBase.
    _crear_cita_confirmada usa para toda OC de prueba (apps/operations/
    tests.py) — necesario para que services_borrador.listar_ocs_elegibles
    (filtra por card_code == proveedor.sap_card_code) encuentre las OC
    construidas por los helpers ya existentes (_finalizar_ticket, etc.).
    InvoicingTestBase._supplier_profile() usa en cambio sap_card_code=
    self.proveedor.username, que NO coincide con 'TESTCODE' — no se toca
    ese helper (las clases ya existentes lo usan sin necesitar este match).
    """

    def _perfil_oc(self):
        perfil, _ = SupplierProfile.objects.get_or_create(
            sap_card_code='TESTCODE', defaults={'ruc': 'TESTCODE', 'user': self.proveedor},
        )
        return perfil

    def _finalizar_segundo_ticket(self, cantidad_real):
        """
        Construye un SEGUNDO Ticket real hasta FINALIZADO, en un slot con
        fecha distinta a la que usa OperationsTestBase._crear_cita_
        confirmada ('2026-09-01 08:00', fija) — _finalizar_ticket no se
        puede llamar 2 veces en el mismo test (colisionaría con ese mismo
        slot, unique_together=('sede','date','start_time')). Mismos
        pasos que _finalizar_ticket (apps.operations.tests), sin
        reutilizarla — 'CDL' fijo (comercial, sin COA) para no complicar
        el helper con el camino Materia Prima, no necesario aquí.
        """
        po = PurchaseOrder.objects.create(
            doc_entry=900001, doc_num=900001, card_code='TESTCODE', card_name='TEST SAC 2',
            e_mail='test2@test.com', status='PENDIENTE', u_mss_tdb='CDL',
        )
        PurchaseOrderLine.objects.create(
            purchase_order=po, line_num=1, item_code='ITEM-TEST-2', description='Item de prueba 2',
            quantity_sap=10, und_medida='KG', requiere_coa=False,
        )
        slot = self._slot_futuro()
        appointment = AppointmentService.solicitar_cita_borrador(
            user=self.proveedor, slot_id=slot.id, oc_ids=[po.id],
        )
        ticket = AppointmentService.confirmar_cita(
            appointment_id=appointment.id, usuario_almacen=self.u_compras,
            base_url='http://testserver/',
        )
        ticket = OperationsService.iniciar_ingreso_planta(
            ticket_id=ticket.id, usuario_vigilancia=self.u_vigilancia,
        )
        OperationsService.autorizar_almacen(ticket_id=ticket.id, usuario=self.u_almacen)
        ticket.refresh_from_db()

        resultados = [
            {'inspeccion_id': insp.id, 'estado': 'CONFORME', 'cantidad_modificada': str(cantidad_real)}
            for insp in TicketLineInspection.objects.filter(ticket=ticket, etapa='ALMACEN')
        ]
        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=self.u_almacen, resultados=resultados,
        )
        ticket.refresh_from_db()
        OperationsService.registrar_salida(ticket_id=ticket.id, usuario_vigilancia=self.u_vigilancia)
        ticket.refresh_from_db()

        # Sesión 99c: para que la OC sea facturable / tenga saldo, la
        # EntradaMercaderia debe llegar a CREADO_SAP (mismo atajo que
        # _finalizar_ticket — este helper quedó sin actualizar cuando se
        # hizo ese cambio).
        _e = EntradaMercaderia.objects.get(ticket=ticket)
        _e.estado = EntradaMercaderia.ESTADO_CREADO_SAP
        _e.estado_sap = 'Y'
        _e.doc_entry_definitivo = 900000 + _e.id
        _e.doc_num_sap = str(500000 + _e.id)
        _e.save(update_fields=['estado', 'estado_sap', 'doc_entry_definitivo', 'doc_num_sap'])
        return ticket

    @staticmethod
    def _cabecera_minima():
        # Sesión 93: doc_cur ya no va acá — _crear_factura_y_lineas lo
        # deriva de la(s) OC seleccionadas (PurchaseOrder.doc_cur), no
        # del payload; incluirlo acá colisionaría con el kwarg explícito
        # doc_cur=moneda que ese método ya pasa a Factura.objects.create.
        return {
            'tax_date': None, 'doc_due_date': None,
            'num_at_card': 'F001-1', 'serie_comprobante': 'F001',
            'numero_comprobante': '1', 'tipo_operacion': '02',
            'clasificacion_bienes_servicios': 1,
        }


class ListarOcsElegiblesTests(NuevaFacturaTestBase):

    def test_oc_con_ticket_finalizado_y_saldo_aparece(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()
        perfil = self._perfil_oc()

        elegibles = sb.listar_ocs_elegibles(perfil)

        self.assertEqual([e['purchase_order'].id for e in elegibles], [po.id])
        self.assertEqual(elegibles[0]['sede'].codigo, 'LURIN')

    def test_oc_sin_ticket_finalizado_no_aparece(self):
        self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'CDL', requiere_coa=False)
        perfil = self._perfil_oc()

        self.assertEqual(sb.listar_ocs_elegibles(perfil), [])

    def test_oc_con_factura_activa_no_aparece(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()
        perfil = self._perfil_oc()

        factura = self._crear_factura(estado='EN_REVISION_COMPRAS', proveedor=perfil)
        FacturaOrdenCompra.objects.create(factura=factura, purchase_order=po)

        self.assertEqual(sb.listar_ocs_elegibles(perfil), [])

    def test_oc_con_factura_cancelada_vuelve_a_aparecer(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()
        perfil = self._perfil_oc()

        factura = self._crear_factura(estado='CANCELADO', proveedor=perfil)
        FacturaOrdenCompra.objects.create(factura=factura, purchase_order=po)

        elegibles = sb.listar_ocs_elegibles(perfil)
        self.assertEqual([e['purchase_order'].id for e in elegibles], [po.id])

    def test_oc_de_otro_proveedor_no_aparece(self):
        self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        otro_perfil = SupplierProfile.objects.create(sap_card_code='OTRO-PROVEEDOR', ruc='OTRO-PROVEEDOR')

        self.assertEqual(sb.listar_ocs_elegibles(otro_perfil), [])


class CrearFacturaDesdeOCsTests(NuevaFacturaTestBase):
    """Punto 6 del pedido: creación con 1 OC, con varias OC, exceso de saldo, retención sin documento."""

    def test_creacion_exitosa_con_una_oc(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()
        po_line = po.lines.first()
        # Sesión 92: precio ya no viene del payload — se hereda de
        # PurchaseOrderLine.precio_unitario (dato real de SAP).
        po_line.precio_unitario = Decimal('2.5000')
        po_line.save(update_fields=['precio_unitario'])
        perfil = self._perfil_oc()

        factura = sb.crear_factura_desde_ocs(
            proveedor=perfil, purchase_order_ids=[po.id],
            cabecera=self._cabecera_minima(),
            lineas_payload=[{
                'po_line': po_line, 'cantidad': Decimal('6.0000'),
                'aplica_retencion': False, 'aplica_detraccion': False,
            }],
            usuario=self.proveedor,
        )

        self.assertEqual(factura.estado, 'BORRADOR')
        self.assertEqual(factura.proveedor_id, perfil.id)
        self.assertEqual(factura.sede.codigo, 'LURIN')
        self.assertEqual(factura.serie_comprobante, 'F001')
        self.assertEqual(factura.ordenes_compra.count(), 1)
        self.assertEqual(factura.ordenes_compra.first().purchase_order_id, po.id)

        linea = factura.lineas.get()
        self.assertEqual(linea.po_line_id, po_line.id)
        self.assertEqual(linea.cantidad, Decimal('6.0000'))
        self.assertEqual(linea.precio, Decimal('2.5000'))
        self.assertEqual(linea.cantidad_oc, po_line.quantity_sap)
        self.assertEqual(linea.precio_oc, Decimal('2.5000'))
        self.assertEqual(linea.tax_code, po_line.tax_code)

    def test_creacion_exitosa_con_varias_oc(self):
        ticket1 = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        ticket2 = self._finalizar_segundo_ticket(cantidad_real=Decimal('4.0000'))
        po1 = ticket1.appointment.purchase_orders.first()
        po2 = ticket2.appointment.purchase_orders.first()
        po_line1 = po1.lines.first()
        po_line2 = po2.lines.first()
        perfil = self._perfil_oc()

        factura = sb.crear_factura_desde_ocs(
            proveedor=perfil, purchase_order_ids=[po1.id, po2.id],
            cabecera=self._cabecera_minima(),
            lineas_payload=[
                {'po_line': po_line1, 'cantidad': Decimal('10.0000'), 'precio': Decimal('1.0000'),
                 'aplica_retencion': False, 'aplica_detraccion': False},
                {'po_line': po_line2, 'cantidad': Decimal('4.0000'), 'precio': Decimal('3.0000'),
                 'aplica_retencion': False, 'aplica_detraccion': False},
            ],
            usuario=self.proveedor,
        )

        self.assertEqual(factura.ordenes_compra.count(), 2)
        self.assertEqual(factura.lineas.count(), 2)
        self.assertEqual(
            set(factura.ordenes_compra.values_list('purchase_order_id', flat=True)),
            {po1.id, po2.id},
        )

    def test_exceder_saldo_rechaza_sin_crear_nada(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()
        po_line = po.lines.first()
        perfil = self._perfil_oc()

        with self.assertRaises(ValidationError):
            sb.crear_factura_desde_ocs(
                proveedor=perfil, purchase_order_ids=[po.id],
                cabecera=self._cabecera_minima(),
                lineas_payload=[{
                    'po_line': po_line, 'cantidad': Decimal('11.0000'), 'precio': Decimal('1.0000'),
                    'aplica_retencion': False, 'aplica_detraccion': False,
                }],
                usuario=self.proveedor,
            )

        self.assertEqual(Factura.objects.count(), 0)
        self.assertEqual(FacturaOrdenCompra.objects.count(), 0)

    def test_retencion_marcada_sin_documento_rechaza_y_no_deja_factura_parcial(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()
        po_line = po.lines.first()
        perfil = self._perfil_oc()

        with self.assertRaises(ValidationError):
            sb.crear_factura_desde_ocs(
                proveedor=perfil, purchase_order_ids=[po.id],
                cabecera=self._cabecera_minima(),
                lineas_payload=[{
                    'po_line': po_line, 'cantidad': Decimal('5.0000'), 'precio': Decimal('1.0000'),
                    'aplica_retencion': True, 'aplica_detraccion': False,
                    'archivo_retencion': None,
                }],
                usuario=self.proveedor,
            )

        # "no crees una Factura parcial" (pedido explícito): la Factura
        # se crea en la Fase 1 y se BORRA al fallar la Fase 2 (falta el
        # documento) — no debe quedar ningún rastro.
        self.assertEqual(Factura.objects.count(), 0)
        self.assertEqual(FacturaOrdenCompra.objects.count(), 0)
        self.assertEqual(FacturaLinea.objects.count(), 0)

    @patch('apps.invoicing.services_archivos.OneDriveClient')
    def test_retencion_con_documento_real_se_adjunta_correctamente(self, mock_cls):
        mock_cls.return_value.upload_documento_factura.return_value = 'https://onedrive.example.com/retencion.pdf'
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()
        po_line = po.lines.first()
        perfil = self._perfil_oc()

        factura = sb.crear_factura_desde_ocs(
            proveedor=perfil, purchase_order_ids=[po.id],
            cabecera=self._cabecera_minima(),
            lineas_payload=[{
                'po_line': po_line, 'cantidad': Decimal('5.0000'), 'precio': Decimal('1.0000'),
                'aplica_retencion': True, 'aplica_detraccion': False,
                'archivo_retencion': SimpleUploadedFile(
                    'r.pdf', _leer_fixture('pdf_valido_minimo.pdf'), content_type='application/pdf',
                ),
            }],
            usuario=self.proveedor,
        )

        linea = factura.lineas.get()
        self.assertEqual(linea.documento_retencion, 'https://onedrive.example.com/retencion.pdf')
        self.assertTrue(linea.hash_documento_retencion)

    @patch('apps.invoicing.services_archivos.OneDriveClient')
    def test_documento_retencion_ya_cargado_muestra_link_ver_en_el_detalle(self, mock_cls):
        """
        Bug real reportado: el documento de retención SÍ se guardaba
        correctamente (DB + OneDrive, Fase 2 de la sesión 79) — el
        problema era que factura_detalle.html mostraba solo un badge
        "Cargado" sin ningún link para verlo, a diferencia del patrón ya
        usado para COA (badge + link "Ver" clicable, botón "Reemplazar"
        en vez de "Subir" cuando ya existe un documento). Verifica el
        HTML real renderizado, no solo el dato en BD.
        """
        mock_cls.return_value.upload_documento_factura.return_value = 'https://onedrive.example.com/retencion.pdf'
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()
        po_line = po.lines.first()
        perfil = self._perfil_oc()

        factura = sb.crear_factura_desde_ocs(
            proveedor=perfil, purchase_order_ids=[po.id],
            cabecera=self._cabecera_minima(),
            lineas_payload=[{
                'po_line': po_line, 'cantidad': Decimal('5.0000'),
                'aplica_retencion': True, 'aplica_detraccion': False,
                'archivo_retencion': SimpleUploadedFile(
                    'r.pdf', _leer_fixture('pdf_valido_minimo.pdf'), content_type='application/pdf',
                ),
            }],
            usuario=self.proveedor,
        )

        self.client.force_login(self.proveedor)
        resp = self.client.get(f'/invoicing/factura/{factura.id}/')

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'https://onedrive.example.com/retencion.pdf')
        self.assertContains(resp, 'bi-eye')  # ícono del link "Ver", mismo patrón que COA
        self.assertContains(resp, 'Ver')
        self.assertContains(resp, 'Reemplazar')
        self.assertNotContains(resp, 'Subir')  # el único botón de subida visible ya dice "Reemplazar"

    def test_oc_de_otro_proveedor_rechaza(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()
        po_line = po.lines.first()
        otro_perfil = SupplierProfile.objects.create(sap_card_code='OTRO-2', ruc='OTRO-2')

        with self.assertRaises(ValidationError):
            sb.crear_factura_desde_ocs(
                proveedor=otro_perfil, purchase_order_ids=[po.id],
                cabecera=self._cabecera_minima(),
                lineas_payload=[{
                    'po_line': po_line, 'cantidad': Decimal('1.0000'), 'precio': Decimal('1.0000'),
                    'aplica_retencion': False, 'aplica_detraccion': False,
                }],
                usuario=self.proveedor,
            )
        self.assertEqual(Factura.objects.count(), 0)

    def test_oc_ya_facturada_rechaza(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()
        po_line = po.lines.first()
        perfil = self._perfil_oc()

        factura_existente = self._crear_factura(estado='EN_REVISION_COMPRAS', proveedor=perfil)
        FacturaOrdenCompra.objects.create(factura=factura_existente, purchase_order=po)

        with self.assertRaises(ValidationError):
            sb.crear_factura_desde_ocs(
                proveedor=perfil, purchase_order_ids=[po.id],
                cabecera=self._cabecera_minima(),
                lineas_payload=[{
                    'po_line': po_line, 'cantidad': Decimal('1.0000'), 'precio': Decimal('1.0000'),
                    'aplica_retencion': False, 'aplica_detraccion': False,
                }],
                usuario=self.proveedor,
            )
        self.assertEqual(Factura.objects.count(), 1)  # solo la ya existente

    def test_precio_en_el_payload_se_ignora_siempre_usa_po_line(self):
        """
        Sesión 92, punto 9 del pedido: "precio bloqueado... rechazo
        también vía POST directo, no solo UI". A nivel de servicio, un
        'precio' fabricado a mano en `fila` (simulando un caller que
        bypasea la UI ya corregida) no tiene ningún efecto — el precio
        real SIEMPRE es po_line.precio_unitario.
        """
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()
        po_line = po.lines.first()
        po_line.precio_unitario = Decimal('7.0000')
        po_line.save(update_fields=['precio_unitario'])
        perfil = self._perfil_oc()

        factura = sb.crear_factura_desde_ocs(
            proveedor=perfil, purchase_order_ids=[po.id],
            cabecera=self._cabecera_minima(),
            lineas_payload=[{
                # 'precio' deliberadamente MUY distinto del real —
                # simula un intento de manipulación.
                'po_line': po_line, 'cantidad': Decimal('1.0000'), 'precio': Decimal('99999.0000'),
                'aplica_retencion': False, 'aplica_detraccion': False,
            }],
            usuario=self.proveedor,
        )

        linea = factura.lineas.get()
        self.assertEqual(linea.precio, Decimal('7.0000'))
        self.assertEqual(linea.precio_oc, Decimal('7.0000'))
        self.assertNotEqual(linea.precio, Decimal('99999.0000'))

    def test_doc_cur_se_hereda_de_la_oc(self):
        """
        Sesión 93, punto 2 del pedido: doc_cur ya no se pide al
        proveedor — se hereda de PurchaseOrder.doc_cur (dato real de
        SAP). _cabecera_minima() ya no incluye 'doc_cur' en absoluto.
        """
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()
        po.doc_cur = 'USD'
        po.save(update_fields=['doc_cur'])
        po_line = po.lines.first()
        perfil = self._perfil_oc()

        factura = sb.crear_factura_desde_ocs(
            proveedor=perfil, purchase_order_ids=[po.id],
            cabecera=self._cabecera_minima(),
            lineas_payload=[{
                'po_line': po_line, 'cantidad': Decimal('1.0000'),
                'aplica_retencion': False, 'aplica_detraccion': False,
            }],
            usuario=self.proveedor,
        )

        self.assertEqual(factura.doc_cur, 'USD')

    def test_ocs_con_monedas_distintas_rechaza_sin_crear_nada(self):
        """Sesión 93, punto 2: mismo criterio que el candado de sede."""
        ticket1 = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        ticket2 = self._finalizar_segundo_ticket(cantidad_real=Decimal('4.0000'))
        po1 = ticket1.appointment.purchase_orders.first()
        po2 = ticket2.appointment.purchase_orders.first()
        po1.doc_cur = 'PEN'
        po1.save(update_fields=['doc_cur'])
        po2.doc_cur = 'USD'
        po2.save(update_fields=['doc_cur'])
        po_line1 = po1.lines.first()
        po_line2 = po2.lines.first()
        perfil = self._perfil_oc()

        with self.assertRaises(ValidationError):
            sb.crear_factura_desde_ocs(
                proveedor=perfil, purchase_order_ids=[po1.id, po2.id],
                cabecera=self._cabecera_minima(),
                lineas_payload=[
                    {'po_line': po_line1, 'cantidad': Decimal('10.0000'),
                     'aplica_retencion': False, 'aplica_detraccion': False},
                    {'po_line': po_line2, 'cantidad': Decimal('4.0000'),
                     'aplica_retencion': False, 'aplica_detraccion': False},
                ],
                usuario=self.proveedor,
            )

        self.assertEqual(Factura.objects.count(), 0)
        self.assertEqual(FacturaOrdenCompra.objects.count(), 0)

    def test_doc_cur_en_el_payload_se_ignora_al_crear(self):
        """
        Sesión 93: mismo criterio que test_precio_en_el_payload_se_
        ignora_siempre_usa_po_line — un 'doc_cur' fabricado a mano en
        `cabecera` (bypaseando la vista, que ya lo excluye vía
        _parsear_cabecera) no tiene ningún efecto; el valor real siempre
        es PurchaseOrder.doc_cur.
        """
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()
        po.doc_cur = 'PEN'
        po.save(update_fields=['doc_cur'])
        po_line = po.lines.first()
        perfil = self._perfil_oc()

        cabecera_manipulada = dict(self._cabecera_minima(), doc_cur='USD')
        factura = sb.crear_factura_desde_ocs(
            proveedor=perfil, purchase_order_ids=[po.id],
            cabecera=cabecera_manipulada,
            lineas_payload=[{
                'po_line': po_line, 'cantidad': Decimal('1.0000'),
                'aplica_retencion': False, 'aplica_detraccion': False,
            }],
            usuario=self.proveedor,
        )

        self.assertEqual(factura.doc_cur, 'PEN')
        self.assertNotEqual(factura.doc_cur, 'USD')


class EditarFacturaTests(NuevaFacturaTestBase):
    """Punto 6 del pedido: edición bloqueada tras EN_REVISION_COMPRAS."""

    def test_edicion_bloqueada_tras_en_revision_compras(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)
        perfil = self._perfil_oc()
        factura = self._crear_factura(estado='EN_REVISION_COMPRAS', proveedor=perfil)
        linea = self._crear_factura_linea(factura, po_line, cantidad=Decimal('1.0000'))

        # Sesión 93: doc_cur ya no es un campo editable en absoluto (se
        # usaba acá como campo de prueba genérico, sin relación con lo
        # que este test verifica realmente — el candado de ESTADO). Se
        # reemplaza por num_at_card, un campo que sigue siendo editable
        # en BORRADOR/OBSERVADA, para seguir probando exactamente el
        # mismo candado sin depender de un campo ya bloqueado.
        with self.assertRaises(ValidationError):
            sb.editar_cabecera_factura(factura, self.proveedor, {'num_at_card': 'F001-999'})

        with self.assertRaises(ValidationError):
            sb.editar_linea_factura(linea, self.proveedor, cantidad=Decimal('2.0000'))

        # Nada cambió.
        factura.refresh_from_db()
        linea.refresh_from_db()
        self.assertIsNone(factura.num_at_card)
        self.assertEqual(linea.cantidad, Decimal('1.0000'))

    def test_edicion_permitida_en_borrador_recalcula_difiere_de_oc(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)
        perfil = self._perfil_oc()
        factura = self._crear_factura(estado='BORRADOR', proveedor=perfil)
        linea = self._crear_factura_linea(factura, po_line, cantidad=po_line.quantity_sap)
        self.assertFalse(linea.difiere_de_oc)

        sb.editar_linea_factura(linea, self.proveedor, cantidad=Decimal('1.0000'))
        linea.refresh_from_db()
        self.assertEqual(linea.cantidad, Decimal('1.0000'))
        self.assertTrue(linea.difiere_de_oc)

        # Sesión 93: mismo reemplazo que el test anterior (doc_cur ya no
        # es editable) — num_at_card sigue siéndolo.
        sb.editar_cabecera_factura(factura, self.proveedor, {'num_at_card': 'F001-999'})
        factura.refresh_from_db()
        self.assertEqual(factura.num_at_card, 'F001-999')

    def test_edicion_excede_saldo_rechaza(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)
        perfil = self._perfil_oc()
        factura = self._crear_factura(estado='BORRADOR', proveedor=perfil)
        linea = self._crear_factura_linea(factura, po_line, cantidad=Decimal('5.0000'))

        with self.assertRaises(ValidationError):
            sb.editar_linea_factura(linea, self.proveedor, cantidad=Decimal('11.0000'))

    def test_precio_no_es_parametro_editable_del_servicio(self):
        """
        Sesión 92: editar_linea_factura ya no acepta 'precio' — pasarlo
        como kwarg debe fallar con TypeError (no es un parámetro válido
        de la función), no ser aceptado y luego ignorado en silencio.
        """
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)
        perfil = self._perfil_oc()
        factura = self._crear_factura(estado='BORRADOR', proveedor=perfil)
        linea = self._crear_factura_linea(factura, po_line, cantidad=Decimal('5.0000'))

        with self.assertRaises(TypeError):
            sb.editar_linea_factura(linea, self.proveedor, precio=Decimal('999.0000'))

    def test_editar_linea_ajax_rechaza_precio_en_el_payload_via_post_directo(self):
        """
        Sesión 92, punto 9 del pedido: "rechazo también vía POST
        directo, no solo UI". Simula un cliente que bypasea la UI (ya
        corregida, nunca envía 'precio') y manda el campo a mano —
        rechazo explícito ANTES de tocar el servicio, precio real
        queda intacto.
        """
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)
        po_line.precio_unitario = Decimal('12.0000')
        po_line.save(update_fields=['precio_unitario'])
        perfil = self._perfil_oc()
        factura = self._crear_factura(estado='BORRADOR', proveedor=perfil)
        linea = self._crear_factura_linea(
            factura, po_line, cantidad=Decimal('5.0000'), precio=Decimal('12.0000'),
        )

        self.client.force_login(self.proveedor)
        resp = self.client.post(
            f'/invoicing/factura-linea/{linea.id}/editar/',
            data=json.dumps({'precio': '1.0000'}),
            content_type='application/json',
        )

        self.assertEqual(resp.status_code, 400)  # _json_err real, no 500
        self.assertEqual(resp.json()['status'], 'error')

        linea.refresh_from_db()
        self.assertEqual(linea.precio, Decimal('12.0000'))  # sin cambios

    def test_editar_cabecera_ajax_rechaza_doc_cur_en_el_payload_via_post_directo(self):
        """
        Sesión 93, punto 2: mismo criterio exacto que el rechazo de
        'precio' de arriba — un 'doc_cur' fabricado a mano en el POST de
        edición de cabecera se rechaza explícito, ANTES de tocar el
        servicio; el valor real (heredado de la OC al crear) queda
        intacto, y el resto de campos del mismo payload tampoco se
        aplica (rechazo de la request completa, no un ignorado parcial).
        """
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()
        po.doc_cur = 'PEN'
        po.save(update_fields=['doc_cur'])
        po_line = po.lines.first()
        perfil = self._perfil_oc()
        factura = sb.crear_factura_desde_ocs(
            proveedor=perfil, purchase_order_ids=[po.id],
            cabecera=self._cabecera_minima(),
            lineas_payload=[{
                'po_line': po_line, 'cantidad': Decimal('1.0000'),
                'aplica_retencion': False, 'aplica_detraccion': False,
            }],
            usuario=self.proveedor,
        )
        self.assertEqual(factura.doc_cur, 'PEN')

        self.client.force_login(self.proveedor)
        resp = self.client.post(
            f'/invoicing/factura/{factura.id}/editar/',
            data=json.dumps({'doc_cur': 'USD', 'num_at_card': 'F001-999'}),
            content_type='application/json',
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['status'], 'error')

        factura.refresh_from_db()
        self.assertEqual(factura.doc_cur, 'PEN')  # sin cambios
        # num_at_card='F001-1' viene de _cabecera_minima(), usada al
        # crear — tampoco se aplicó el 'F001-999' del payload rechazado.
        self.assertEqual(factura.num_at_card, 'F001-1')


class NuevaFacturaHTTPFlowTests(NuevaFacturaTestBase):
    """
    Flujo HTTP real de punta a punta: listado -> copia -> creación (AJAX)
    -> detalle -> carga de los 3 archivos -> enviar a revisión. Confirma
    que las 3 plantillas nuevas renderizan sin error Y que el flujo real
    (no solo las funciones de servicio aisladas) funciona.
    """

    def test_flujo_completo_desde_listado_hasta_enviar_a_revision(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()
        self._perfil_oc()  # crea/vincula el SupplierProfile con card_code='TESTCODE'

        self.client.force_login(self.proveedor)

        # Paso 1: listado.
        resp = self.client.get('/invoicing/nueva/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f'OC {po.doc_num}')

        # Paso 2: copiar — sin ningún guardado en BD todavía.
        resp = self.client.get('/invoicing/nueva/copiar/', {'oc_ids': [po.id]})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f'OC {po.doc_num}')
        self.assertEqual(Factura.objects.count(), 0)

        # Paso 3: crear (AJAX, multipart). cantidad*precio = 1180.50, para
        # que coincida EXACTAMENTE con el PayableAmount de la fixture
        # 'factura_sintetica_firmada.xml' (1180.50 PEN, ya confirmado en
        # ProcesarValidacionDocumentosTests) — de lo contrario
        # InvoicingService.enviar_a_revision (paso 6) rechazaría por
        # importe_no_coincide, que es exactamente lo esperado pero no lo
        # que este test de flujo feliz quiere ejercitar. Sesión 92:
        # precio ya no se envía por POST — se hereda de PurchaseOrderLine.
        # precio_unitario. IGV_EXE a propósito, mismo criterio ya
        # aplicado en el resto de la suite: mantiene el total neto ==
        # PayableAmount sin recalcular los números redondos originales.
        po_line = po.lines.first()
        po_line.precio_unitario = Decimal('118.0500')
        po_line.tax_code = PurchaseOrderLine.TAX_CODE_IGV_EXE
        po_line.save(update_fields=['precio_unitario', 'tax_code'])
        resp = self.client.post('/invoicing/nueva/crear/', data={
            'oc_ids': [po.id],
            'serie_comprobante': 'F001', 'numero_comprobante': '999',
            'tipo_operacion': '02',
            f'cantidad_{po_line.id}': '10.0000',
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['status'], 'success')
        factura_id = data['factura_id']

        factura = Factura.objects.get(id=factura_id)
        self.assertEqual(factura.estado, 'BORRADOR')

        # La OC ya no vuelve a aparecer en el listado (candado de unicidad).
        resp = self.client.get('/invoicing/nueva/')
        self.assertNotContains(resp, f'OC {po.doc_num}')

        # Paso 4: detalle.
        resp = self.client.get(f'/invoicing/factura/{factura_id}/')
        self.assertEqual(resp.status_code, 200)

        # Paso 5: los 3 archivos (mismo mock ya usado por ProcesarValidacionDocumentosTests).
        xml_bytes = _leer_fixture('factura_sintetica_firmada.xml')
        cdr_bytes = _leer_fixture('cdr_sintetico_aceptado.xml')
        pdf_bytes = _leer_fixture('pdf_valido_minimo.pdf')

        with patch('apps.invoicing.services_archivos.OneDriveClient') as mock_cls:
            mock_cls.return_value.upload_documento_factura.return_value = 'https://onedrive.example.com/x'

            def _descargar(sede, ruc, identificador, nombre_archivo):
                if nombre_archivo.startswith('xml.'):
                    return xml_bytes
                if nombre_archivo.startswith('cdr.'):
                    return cdr_bytes
                raise AssertionError(f"Descarga inesperada de '{nombre_archivo}'.")
            mock_cls.return_value.descargar_documento_factura.side_effect = _descargar

            for tipo, contenido, nombre, content_type in (
                ('pdf', pdf_bytes, 'f.pdf', 'application/pdf'),
                ('cdr', cdr_bytes, 'c.xml', 'application/xml'),
                ('xml', xml_bytes, 'f.xml', 'application/xml'),
            ):
                resp = self.client.post(
                    f'/invoicing/factura/{factura_id}/archivo/{tipo}/',
                    data={'archivo': SimpleUploadedFile(nombre, contenido, content_type=content_type)},
                )
                self.assertEqual(resp.json()['status'], 'success', resp.json())

        factura.refresh_from_db()
        self.assertTrue(factura.firma_valida)

        # Paso 6: enviar a revisión.
        resp = self.client.post(
            f'/invoicing/factura/{factura_id}/enviar-a-revision/',
            data='{}', content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['status'], 'success')
        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'EN_REVISION_COMPRAS')

        # Edición ahora bloqueada — mismo candado, expuesto vía HTTP.
        resp = self.client.post(
            f'/invoicing/factura/{factura_id}/editar/',
            data=json.dumps({'doc_cur': 'USD'}), content_type='application/json',
        )
        self.assertEqual(resp.json()['status'], 'error')

    def test_pantalla_de_copia_no_tiene_ningun_input_editable_de_precio(self):
        """
        Sesión 92, punto 6: la Pantalla de Copia ya no debe ofrecer NADA
        editable para precio — ni siquiera un input oculto. Verifica el
        HTML real renderizado, no solo el comportamiento del backend.
        """
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()
        po_line = po.lines.first()
        po_line.precio_unitario = Decimal('42.0000')
        po_line.save(update_fields=['precio_unitario'])
        self._perfil_oc()

        self.client.force_login(self.proveedor)
        resp = self.client.get('/invoicing/nueva/copiar/', {'oc_ids': [po.id]})

        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, f'name="precio_{po_line.id}"')
        # El precio real de SAP sí se muestra, en solo lectura.
        self.assertContains(resp, '42.0000')

    def test_precio_enviado_por_post_directo_en_la_creacion_se_ignora(self):
        """
        Sesión 92, punto 9: complemento HTTP del test de servicio ya
        existente (CrearFacturaDesdeOCsTests) — un POST real, fabricado
        a mano con un precio_<id> que la UI corregida nunca enviaría,
        no tiene ningún efecto en el precio guardado.
        """
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()
        po_line = po.lines.first()
        po_line.precio_unitario = Decimal('9.0000')
        po_line.save(update_fields=['precio_unitario'])
        self._perfil_oc()

        self.client.force_login(self.proveedor)
        resp = self.client.post('/invoicing/nueva/crear/', data={
            'oc_ids': [po.id],
            'serie_comprobante': 'F001', 'numero_comprobante': '999',
            'tipo_operacion': '02',
            f'cantidad_{po_line.id}': '1.0000',
            f'precio_{po_line.id}': '99999.0000',  # intento de manipulación
        })

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['status'], 'success', data)

        factura = Factura.objects.get(id=data['factura_id'])
        linea = factura.lineas.get()
        self.assertEqual(linea.precio, Decimal('9.0000'))
        self.assertNotEqual(linea.precio, Decimal('99999.0000'))

    def test_cantidad_a_facturar_precargada_con_cantidad_real_inspeccionada(self):
        """
        Sesión 92, punto 8: "Cantidad a Facturar" debe pre-cargarse con
        EntradaMercaderiaLinea.cantidad (cantidad real inspeccionada),
        acotada al saldo disponible como tope. Verificado empíricamente
        (no solo por lectura de código): en el caso más común — primera
        Factura sobre la línea, sin facturación previa — saldo_
        disponible() (InvoicingService, Sub-fase 3.1) YA es exactamente
        igual a EntradaMercaderiaLinea.cantidad (saldo = cantidad_real -
        ya_facturado, y ya_facturado=0 acá), y ES el valor que la
        Pantalla de Copia ya usa como value/max del input — confirma que
        el punto 8 ya estaba satisfecho por el mecanismo existente, sin
        necesitar ningún cambio de código.
        """
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('37.5000'))
        po = ticket.appointment.purchase_orders.first()
        po_line = po.lines.first()
        self._perfil_oc()

        entrada_linea = EntradaMercaderiaLinea.objects.get(po_line=po_line)
        self.assertEqual(entrada_linea.cantidad, Decimal('37.5000'))

        self.client.force_login(self.proveedor)
        resp = self.client.get('/invoicing/nueva/copiar/', {'oc_ids': [po.id]})

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f'name="cantidad_{po_line.id}"')
        self.assertContains(resp, 'value="37.5000"')
        self.assertContains(resp, f'max="37.5000"')


class CrearFacturaDesdeOCsConcurrenciaTests(TransactionTestCase):
    """
    Punto 6 del pedido — "mismo patrón TransactionTestCase de las
    sesiones 70/71, reutilízalo" — ahora contra el FLUJO COMPLETO real
    (services_borrador.crear_factura_desde_ocs), no solo validar_oc_
    disponible aislado (ya verificado y corregido en la sesión 70). Si la
    orquestación nueva reabriera la misma ventana de carrera (por
    ejemplo, si el lock de PurchaseOrder se liberara antes de comitear la
    Factura), este test lo detectaría.

    El retraso se inyecta parcheando InvoicingService.validar_oc_
    disponible UNA SOLA VEZ, envolviendo el arranque de AMBOS hilos (no
    un patch por hilo — parchear un atributo de clase compartido desde 2
    hilos sería en sí mismo una condición de carrera del TEST, no de la
    producción) con un side_effect que primero llama a la implementación
    REAL (adquiere el lock real de PurchaseOrder) y RECIÉN DESPUÉS duerme
    el retraso que le toca a ese hilo (identificado por threading.
    current_thread().name) — el sleep ocurre con el lock ya retenido y la
    transacción de _crear_factura_y_lineas todavía abierta, reproduciendo
    la misma ventana que el bug original de la sesión 70 explotaba.

    TransactionTestCase (no TestCase): cada hilo necesita su propia
    conexión/transacción real — TestCase envuelve el test entero en una
    única transacción compartida, lo que impediría el bloqueo real entre
    hilos (mismo motivo ya documentado en las sesiones 70/71).
    """

    def setUp(self):
        Sede.objects.get_or_create(codigo='LURIN', defaults={'nombre': 'Planta Lurín'})
        g_compras, _ = Group.objects.get_or_create(name='COMPRAS')

        self.proveedor_user = User.objects.create_user('carrera_factura_proveedor', password='x')
        self.supplier_profile = SupplierProfile.objects.create(
            sap_card_code='CARRERA-FACTURA', ruc='CARRERA-FACTURA', user=self.proveedor_user,
        )
        u_compras = User.objects.create_user('carrera_factura_compras', password='x')
        u_compras.groups.add(g_compras)
        u_vigilancia = User.objects.create_user('carrera_factura_vigilancia', password='x')
        u_almacen = User.objects.create_user('carrera_factura_almacen', password='x')

        doc_num = 666555444
        po = PurchaseOrder.objects.create(
            doc_entry=doc_num, doc_num=doc_num, card_code='CARRERA-FACTURA',
            card_name='CARRERA FACTURA SAC', e_mail='carrera@test.com',
            status='PENDIENTE', u_mss_tdb='CDL',
        )
        self.po_line = PurchaseOrderLine.objects.create(
            purchase_order=po, line_num=1, item_code='ITEM-CARRERA-FACTURA',
            description='Item carrera factura', quantity_sap=20, und_medida='KG',
        )
        self.po = po

        slot = AppointmentSlot.objects.create(
            sede=Sede.objects.get(codigo='LURIN'),
            date='2026-09-17', start_time='09:30', dock='TEST', max_capacity=5,
        )
        appointment = AppointmentService.solicitar_cita_borrador(
            user=self.proveedor_user, slot_id=slot.id, oc_ids=[po.id],
        )
        ticket = AppointmentService.confirmar_cita(
            appointment_id=appointment.id, usuario_almacen=u_compras,
            base_url='http://testserver/',
        )
        ticket = OperationsService.iniciar_ingreso_planta(
            ticket_id=ticket.id, usuario_vigilancia=u_vigilancia,
        )
        OperationsService.autorizar_almacen(ticket_id=ticket.id, usuario=u_almacen)
        ticket.refresh_from_db()

        resultados_insp = [
            {'inspeccion_id': insp.id, 'estado': 'CONFORME', 'cantidad_modificada': '20.0000'}
            for insp in TicketLineInspection.objects.filter(ticket=ticket, etapa='ALMACEN')
        ]
        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=u_almacen, resultados=resultados_insp,
        )
        ticket.refresh_from_db()
        OperationsService.registrar_salida(ticket_id=ticket.id, usuario_vigilancia=u_vigilancia)

        # Sesión 99c: saldo_disponible solo cuenta rondas CREADO_SAP —
        # ver nota en SaldoDisponibleConcurrenciaTests.setUp.
        _e = EntradaMercaderia.objects.get(ticket=ticket)
        _e.estado = EntradaMercaderia.ESTADO_CREADO_SAP
        _e.estado_sap = 'Y'
        _e.save(update_fields=['estado', 'estado_sap'])

    @staticmethod
    def _cabecera_minima():
        # Sesión 93: doc_cur ya no va acá — _crear_factura_y_lineas lo
        # deriva de la(s) OC seleccionadas (PurchaseOrder.doc_cur), no
        # del payload; incluirlo acá colisionaría con el kwarg explícito
        # doc_cur=moneda que ese método ya pasa a Factura.objects.create.
        return {
            'tax_date': None, 'doc_due_date': None,
            'num_at_card': 'F001-1', 'serie_comprobante': 'F001',
            'numero_comprobante': '1', 'tipo_operacion': '02',
            'clasificacion_bienes_servicios': 1,
        }

    def test_dos_intentos_concurrentes_de_crear_factura_desde_la_misma_oc_no_duplican_ni_pasan_ambos(self):
        resultados = {}
        retrasos = {'t1': 0.5, 't2': 0.0}
        real_validar = InvoicingService.validar_oc_disponible

        def _validar_con_retraso(purchase_order, excluir_factura=None):
            real_validar(purchase_order, excluir_factura=excluir_factura)
            time.sleep(retrasos.get(threading.current_thread().name, 0))

        def intentar(nombre):
            try:
                po = PurchaseOrder.objects.get(pk=self.po.pk)
                po_line = PurchaseOrderLine.objects.get(pk=self.po_line.pk)
                sb.crear_factura_desde_ocs(
                    proveedor=self.supplier_profile,
                    purchase_order_ids=[po.id],
                    cabecera=self._cabecera_minima(),
                    lineas_payload=[{
                        'po_line': po_line, 'cantidad': Decimal('5.0000'),
                        'precio': Decimal('1.0000'), 'aplica_retencion': False,
                        'aplica_detraccion': False,
                    }],
                    usuario=self.proveedor_user,
                )
                resultados[nombre] = 'OK'
            except ValidationError:
                resultados[nombre] = 'RECHAZADO'
            finally:
                connections.close_all()

        # t1 arranca primero y retiene la transacción abierta 0.5s antes
        # de comitear — misma ventana exacta que el bug real de
        # validar_oc_disponible explotaba (sesión 70); aquí se espera que
        # crear_factura_desde_ocs, que la reutiliza, esté libre del bug.
        with patch.object(InvoicingService, 'validar_oc_disponible', side_effect=_validar_con_retraso):
            t1 = threading.Thread(target=intentar, args=('t1',), name='t1')
            t2 = threading.Thread(target=intentar, args=('t2',), name='t2')
            t1.start()
            time.sleep(0.05)
            t2.start()
            t1.join()
            t2.join()

        # Exactamente uno de los 2 debe pasar y el otro debe ser
        # rechazado — nunca ambos "OK" (duplicaría la Factura sobre la
        # misma OC) ni ambos "RECHAZADO" (bloquearía a un llamador
        # legítimo sin ningún motivo real).
        self.assertEqual(sorted(resultados.values()), ['OK', 'RECHAZADO'])
        self.assertEqual(
            Factura.objects.filter(ordenes_compra__purchase_order=self.po).count(), 1,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Sub-fase 3.5 (esta sesión) — el lado de Compras: aprobar_factura /
# observar_factura. 2 niveles: pruebas directas contra InvoicingService
# (permiso, defensa en profundidad, historial de rondas) y un flujo HTTP
# real de punta a punta (observar -> proveedor corrige -> reenviar ->
# aprobar), reutilizando el mismo patrón de mock de OneDrive que
# NuevaFacturaHTTPFlowTests.
# ═══════════════════════════════════════════════════════════════════════════

class AprobarObservarFacturaTests(InvoicingTestBase):

    def _factura_en_revision(self, **extra):
        defaults = dict(
            proveedor=self._supplier_profile(), sede=self._sede(),
            estado='EN_REVISION_COMPRAS', firma_valida=True, estado_cdr='ACEPTADO',
            importe_no_coincide=False,
        )
        defaults.update(extra)
        return Factura.objects.create(**defaults)

    # ── aprobar_factura ──────────────────────────────────────────────────

    @patch('apps.base.services_correo.enviar_correo')
    def test_aprobar_con_firma_invalida_rechaza_pese_a_estar_en_revision(self, mock_enviar):
        """
        Punto 5 del pedido, caso explícito: aunque la Factura ya esté
        EN_REVISION_COMPRAS (lo que en teoría implica que ya pasó por
        enviar_a_revision), aprobar_factura vuelve a evaluar firma_valida
        por su cuenta (defensa en profundidad) y rechaza igual.
        """
        factura = self._factura_en_revision(firma_valida=False)

        with self.assertRaises(ValidationError):
            InvoicingService.aprobar_factura(factura, self.u_compras)

        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'EN_REVISION_COMPRAS')
        self.assertEqual(factura.estado_sap, '')
        mock_enviar.assert_not_called()

    def test_aprobar_con_cdr_rechazado_rechaza(self):
        factura = self._factura_en_revision(estado_cdr='RECHAZADO')
        with self.assertRaises(ValidationError):
            InvoicingService.aprobar_factura(factura, self.u_compras)
        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'EN_REVISION_COMPRAS')

    def test_aprobar_con_importe_no_coincide_rechaza(self):
        factura = self._factura_en_revision(importe_no_coincide=True)
        with self.assertRaises(ValidationError):
            InvoicingService.aprobar_factura(factura, self.u_compras)
        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'EN_REVISION_COMPRAS')

    def test_aprobar_exitoso_marca_aprobada_y_estado_sap_L(self):
        factura = self._factura_en_revision()
        InvoicingService.aprobar_factura(factura, self.u_compras)
        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'APROBADA_COMPRAS')
        self.assertEqual(factura.estado_sap, 'L')

    def test_aprobar_fuera_de_en_revision_rechaza(self):
        factura = self._factura_en_revision(estado='BORRADOR', firma_valida=None, estado_cdr=None)
        with self.assertRaises(ValidationError):
            InvoicingService.aprobar_factura(factura, self.u_compras)

    def test_aprobar_cuenta_sin_grupo_compras_rechaza(self):
        """Punto 5 del pedido: cuenta sin grupo COMPRAS intentando aprobar."""
        factura = self._factura_en_revision()
        with self.assertRaises(ValidationError):
            InvoicingService.aprobar_factura(factura, self.u_almacen)
        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'EN_REVISION_COMPRAS')

    def test_aprobar_superusuario_sin_grupo_compras_funciona(self):
        superusuario = User.objects.create_superuser('super_aprobar_test', password='x')
        factura = self._factura_en_revision()
        InvoicingService.aprobar_factura(factura, superusuario)
        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'APROBADA_COMPRAS')

    # ── observar_factura ─────────────────────────────────────────────────

    @patch('apps.base.services_correo.enviar_correo')
    def test_observar_sin_texto_rechaza(self, mock_enviar):
        """Punto 5 del pedido: observar sin texto (rechaza)."""
        factura = self._factura_en_revision()
        with self.assertRaises(ValidationError):
            InvoicingService.observar_factura(factura, self.u_compras, '   ')
        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'EN_REVISION_COMPRAS')
        self.assertEqual(factura.observaciones.count(), 0)
        mock_enviar.assert_not_called()

    def test_observar_cuenta_sin_grupo_compras_rechaza(self):
        factura = self._factura_en_revision()
        with self.assertRaises(ValidationError):
            InvoicingService.observar_factura(factura, self.u_almacen, 'Falta el RUC.')
        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'EN_REVISION_COMPRAS')
        self.assertEqual(factura.observaciones.count(), 0)

    def test_observar_fuera_de_en_revision_rechaza(self):
        factura = self._factura_en_revision(estado='OBSERVADA')
        with self.assertRaises(ValidationError):
            InvoicingService.observar_factura(factura, self.u_compras, 'Otra observación.')

    @patch('apps.base.services_correo.enviar_correo')
    def test_observar_no_bloquea_si_falla_el_correo(self, mock_enviar):
        mock_enviar.return_value = ResultadoEnvioCorreo(enviado=False, error='Graph caído.')
        factura = self._factura_en_revision()

        InvoicingService.observar_factura(factura, self.u_compras, 'Corregir RUC.')

        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'OBSERVADA')
        self.assertEqual(factura._email_observacion_error, 'Graph caído.')

    @patch('apps.base.services_correo.enviar_correo')
    def test_historial_de_2_rondas_de_observacion_se_conserva_integro(self, mock_enviar):
        """Punto 5 del pedido: historial de 2+ rondas se conserva íntegro."""
        mock_enviar.return_value = ResultadoEnvioCorreo(enviado=True)
        factura = self._factura_en_revision()

        InvoicingService.observar_factura(factura, self.u_compras, 'Ronda 1: falta el RUC.')
        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'OBSERVADA')

        # El proveedor corrige y reenvía SIN que Compras haya tocado nada
        # de la ronda anterior — enviar_a_revision no crea ninguna
        # FacturaObservacion, solo cambia el estado.
        InvoicingService.enviar_a_revision(factura)
        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'EN_REVISION_COMPRAS')

        InvoicingService.observar_factura(factura, self.u_compras, 'Ronda 2: falta el monto en letras.')
        factura.refresh_from_db()

        observaciones = list(factura.observaciones.all())
        self.assertEqual(len(observaciones), 2)
        self.assertEqual(observaciones[0].ronda, 1)
        self.assertEqual(observaciones[0].texto, 'Ronda 1: falta el RUC.')
        self.assertEqual(observaciones[1].ronda, 2)
        self.assertEqual(observaciones[1].texto, 'Ronda 2: falta el monto en letras.')
        self.assertEqual(mock_enviar.call_count, 2)


class AprobarObservarFacturaHTTPTests(NuevaFacturaTestBase):
    """
    Flujo HTTP real de punta a punta, continuando exactamente donde
    termina NuevaFacturaHTTPFlowTests (que se detiene en "enviar a
    revisión"): Compras observa -> el proveedor ve la observación en
    "Mis Facturas" y puede reeditar/reenviar (verifica en carne propia el
    candado ya construido en la Sub-fase 3.2/3.4, punto 4 del pedido,
    "no lo des por hecho") -> Compras aprueba.
    """

    def _subir_los_3_archivos(self, factura_id, xml_bytes, cdr_bytes, pdf_bytes):
        with patch('apps.invoicing.services_archivos.OneDriveClient') as mock_cls:
            mock_cls.return_value.upload_documento_factura.return_value = 'https://onedrive.example.com/x'

            def _descargar(sede, ruc, identificador, nombre_archivo):
                if nombre_archivo.startswith('xml.'):
                    return xml_bytes
                if nombre_archivo.startswith('cdr.'):
                    return cdr_bytes
                raise AssertionError(f"Descarga inesperada de '{nombre_archivo}'.")
            mock_cls.return_value.descargar_documento_factura.side_effect = _descargar

            for tipo, contenido, nombre, content_type in (
                ('pdf', pdf_bytes, 'f.pdf', 'application/pdf'),
                ('cdr', cdr_bytes, 'c.xml', 'application/xml'),
                ('xml', xml_bytes, 'f.xml', 'application/xml'),
            ):
                resp = self.client.post(
                    f'/invoicing/factura/{factura_id}/archivo/{tipo}/',
                    data={'archivo': SimpleUploadedFile(nombre, contenido, content_type=content_type)},
                )
                self.assertEqual(resp.json()['status'], 'success', resp.json())

    @patch('apps.base.services_correo.enviar_correo')
    def test_ciclo_completo_observar_corregir_reenviar_aprobar(self, mock_enviar):
        """Punto 5 del pedido: ciclo completo observar -> proveedor corrige -> reenviar -> aprobar."""
        mock_enviar.return_value = ResultadoEnvioCorreo(enviado=True)

        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = ticket.appointment.purchase_orders.first()
        po_line = po.lines.first()
        self._perfil_oc()

        # Sesión 92: precio ya no se envía por POST — se hereda de
        # PurchaseOrderLine.precio_unitario. IGV_EXE a propósito (mismo
        # criterio ya aplicado en ProcesarValidacionDocumentosTests):
        # mantiene el total neto == PayableAmount de la fixture sin
        # tener que recalcular los números redondos originales.
        po_line.precio_unitario = Decimal('118.0500')
        po_line.tax_code = PurchaseOrderLine.TAX_CODE_IGV_EXE
        po_line.save(update_fields=['precio_unitario', 'tax_code'])

        # El proveedor arma y envía la Factura (mismos datos ya probados
        # en NuevaFacturaHTTPFlowTests: cantidad*precio = 1180.50,
        # coincide con el PayableAmount de la fixture firmada).
        self.client.force_login(self.proveedor)
        resp = self.client.post('/invoicing/nueva/crear/', data={
            'oc_ids': [po.id],
            'serie_comprobante': 'F001', 'numero_comprobante': '999',
            'tipo_operacion': '02',
            f'cantidad_{po_line.id}': '10.0000',
        })
        factura_id = resp.json()['factura_id']

        xml_bytes = _leer_fixture('factura_sintetica_firmada.xml')
        cdr_bytes = _leer_fixture('cdr_sintetico_aceptado.xml')
        pdf_bytes = _leer_fixture('pdf_valido_minimo.pdf')
        self._subir_los_3_archivos(factura_id, xml_bytes, cdr_bytes, pdf_bytes)

        resp = self.client.post(
            f'/invoicing/factura/{factura_id}/enviar-a-revision/',
            data='{}', content_type='application/json',
        )
        self.assertEqual(resp.json()['status'], 'success')

        # ── Compras observa ──────────────────────────────────────────
        self.client.force_login(self.u_compras)

        resp = self.client.get(f'/invoicing/compras/factura/{factura_id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Aprobar')
        self.assertContains(resp, 'Observar')
        # Solo lectura para Compras — no se renderiza el botón de guardar
        # cabecera ni el botón de subir archivos (mismo `puede_editar`
        # que ya gatea esos elementos para el proveedor).
        self.assertNotContains(resp, 'guardarCabecera(')
        self.assertNotContains(resp, 'subirArchivoFactura(')

        resp = self.client.post(
            f'/invoicing/compras/factura/{factura_id}/observar/',
            data=json.dumps({'texto': 'Falta el número de RUC del proveedor en el PDF.'}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['status'], 'success')

        factura = Factura.objects.get(id=factura_id)
        self.assertEqual(factura.estado, 'OBSERVADA')
        self.assertEqual(factura.observaciones.count(), 1)

        # Aprobar/Observar ya no deben ofrecerse mientras está OBSERVADA
        # (puede_actuar_compras exige EN_REVISION_COMPRAS).
        resp = self.client.get(f'/invoicing/compras/factura/{factura_id}/')
        self.assertNotContains(resp, 'aprobarFactura(')

        # ── El proveedor ve la observación y puede corregir/reenviar ──
        self.client.force_login(self.proveedor)

        resp = self.client.get('/invoicing/mis-facturas/')
        self.assertContains(resp, 'Falta el número de RUC')

        # Verifica en carne propia (no solo por lectura de código, punto
        # 4 del pedido) que el candado de la Sub-fase 3.2/3.4 sí permite
        # reeditar mientras la Factura está OBSERVADA.
        # Sesión 93: doc_cur ya no se envía acá — se rechazaría el POST
        # completo (ya no es editable, ver test dedicado más abajo).
        resp = self.client.post(
            f'/invoicing/factura/{factura_id}/editar/',
            data=json.dumps({'num_at_card': 'F001-999-CORREGIDO'}),
            content_type='application/json',
        )
        self.assertEqual(resp.json()['status'], 'success')
        factura.refresh_from_db()
        self.assertEqual(factura.num_at_card, 'F001-999-CORREGIDO')

        resp = self.client.post(
            f'/invoicing/factura/{factura_id}/enviar-a-revision/',
            data='{}', content_type='application/json',
        )
        self.assertEqual(resp.json()['status'], 'success')
        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'EN_REVISION_COMPRAS')

        # ── Compras aprueba ──────────────────────────────────────────
        self.client.force_login(self.u_compras)
        resp = self.client.post(f'/invoicing/compras/factura/{factura_id}/aprobar/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['status'], 'success')

        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'APROBADA_COMPRAS')
        self.assertEqual(factura.estado_sap, 'L')

        # La ronda de observaciones sigue íntegra (1 sola, no se perdió
        # ni se duplicó a lo largo de todo el ciclo).
        self.assertEqual(factura.observaciones.count(), 1)
        self.assertEqual(factura.observaciones.first().ronda, 1)

    def test_cuenta_sin_grupo_compras_bloqueada_por_la_vista(self):
        """
        Complemento HTTP de test_aprobar_cuenta_sin_grupo_compras_rechaza:
        @compras_required redirige (mismo patrón ya usado por panel_
        compras/ajax_confirmar_cita_compras) antes de llegar al servicio.
        """
        factura = self._crear_factura(estado='EN_REVISION_COMPRAS', proveedor=self._perfil_oc())
        self.client.force_login(self.u_almacen)

        resp = self.client.post(f'/invoicing/compras/factura/{factura.id}/aprobar/')
        self.assertEqual(resp.status_code, 302)

        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'EN_REVISION_COMPRAS')


# ═══════════════════════════════════════════════════════════════════════════
# Sub-fase 3.6 (esta sesión) — endpoints del daemon SAP para Factura
# (api/factura_api.py). 3 flujos (preliminar/reconciliación/cancelación),
# el interruptor FACTURA_DRAFT_SAP_HABILITADO, y el candado explícito de
# estado_sap='Y' bloqueando edición.
# ═══════════════════════════════════════════════════════════════════════════

class FacturaSAPAPITestBase(InvoicingTestBase):
    """
    Base compartida por los tests del daemon SAP de Factura — mismo
    criterio de autenticación por Token ya usado en EntradaMercaderiaAPI
    Tests (apps/operations/tests.py, sesión 57).
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.daemon_user = User.objects.create_user(username='daemon_test_factura', password='x')
        cls.token = Token.objects.create(user=cls.daemon_user)

    def setUp(self):
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

    def _finalizar_ticket_en_fecha(self, slot_date, cantidad_real):
        """
        Ticket real 'CDL' (comercial, sin COA) hasta FINALIZADO en un
        slot con `slot_date` explícito — mismos pasos que InvoicingTest
        Base._finalizar_ticket, sin reutilizarla: esa usa siempre la
        fecha de slot FIJA '2026-09-01' (heredada de OperationsTestBase.
        _crear_cita_confirmada), así que llamarla más de una vez en el
        mismo test colisiona con AppointmentSlot.unique_together=
        ('sede','date','start_time') — mismo motivo ya documentado en
        NuevaFacturaTestBase._finalizar_segundo_ticket (sesión 79),
        generalizado aquí a una fecha arbitraria por llamada (varios
        tests de este bloque necesitan más de una Factura/Ticket real
        dentro del mismo método, para probar qué queda excluido de cada
        filtro, no solo qué queda incluido).
        """
        doc_num = next(_doc_num_counter)
        po = PurchaseOrder.objects.create(
            doc_entry=doc_num, doc_num=doc_num, card_code='TESTCODE', card_name='TEST SAC',
            e_mail='test@test.com', status='PENDIENTE', u_mss_tdb='CDL',
        )
        PurchaseOrderLine.objects.create(
            purchase_order=po, line_num=1, item_code=f'ITEM-{doc_num}', description='Item de prueba',
            quantity_sap=10, und_medida='KG', requiere_coa=False,
        )
        slot = AppointmentSlot.objects.create(
            sede=Sede.objects.get(codigo='LURIN'),
            date=slot_date, start_time='08:00', dock='TEST', max_capacity=5,
        )
        appointment = AppointmentService.solicitar_cita_borrador(
            user=self.proveedor, slot_id=slot.id, oc_ids=[po.id],
        )
        ticket = AppointmentService.confirmar_cita(
            appointment_id=appointment.id, usuario_almacen=self.u_compras,
            base_url='http://testserver/',
        )
        ticket = OperationsService.iniciar_ingreso_planta(
            ticket_id=ticket.id, usuario_vigilancia=self.u_vigilancia,
        )
        OperationsService.autorizar_almacen(ticket_id=ticket.id, usuario=self.u_almacen)
        ticket.refresh_from_db()

        resultados = [
            {'inspeccion_id': insp.id, 'estado': 'CONFORME', 'cantidad_modificada': str(cantidad_real)}
            for insp in TicketLineInspection.objects.filter(ticket=ticket, etapa='ALMACEN')
        ]
        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=self.u_almacen, resultados=resultados,
        )
        ticket.refresh_from_db()
        OperationsService.registrar_salida(ticket_id=ticket.id, usuario_vigilancia=self.u_vigilancia)
        ticket.refresh_from_db()
        return ticket

    def _factura_aprobada_con_grpo(self, *, slot_date='2026-10-01', grpo_doc_entry=5001,
                                    cantidad=Decimal('10.0000'), precio=Decimal('118.0500'),
                                    serie='F001', numero='500'):
        """
        Punto de partida real de casi todos los tests de este bloque: un
        Ticket FINALIZADO con su EntradaMercaderia ya confirmada en SAP
        (estado_sap='Y', doc_entry_definitivo=grpo_doc_entry — como si el
        daemon ya hubiera sincronizado ese GRPO en una sesión anterior de
        la Sub-fase 3.6 de EntradaMercaderia, sesiones 57-58), y la
        Factura correspondiente ya APROBADA_COMPRAS (estado_sap='L') —
        exactamente el estado que InvoicingService.aprobar_factura deja
        (sesión 81), el punto de entrada real de facturas-pendientes-
        preliminar/.
        """
        ticket = self._finalizar_ticket_en_fecha(slot_date, cantidad)
        po_line = ticket.appointment.purchase_orders.first().lines.first()

        entrada = EntradaMercaderia.objects.get(ticket=ticket)
        entrada.doc_entry_definitivo = grpo_doc_entry
        entrada.estado_sap = 'Y'
        entrada.estado = EntradaMercaderia.ESTADO_CREADO_SAP  # sesión 99: estado de negocio consistente con estado_sap='Y'
        entrada.fecha_definitivo_confirmado = timezone.now()
        entrada.save(update_fields=['doc_entry_definitivo', 'estado', 'estado_sap', 'fecha_definitivo_confirmado'])

        perfil = self._supplier_profile()
        factura = Factura.objects.create(
            proveedor=perfil, sede=self._sede(), estado='EN_REVISION_COMPRAS',
            firma_valida=True, estado_cdr='ACEPTADO', importe_no_coincide=False,
            serie_comprobante=serie, numero_comprobante=numero, doc_cur='PEN',
            num_at_card=f'{serie}-{numero}', tipo_operacion='02', clasificacion_bienes_servicios=1,
        )
        self._crear_factura_linea(factura, po_line, cantidad=cantidad, precio=precio, precio_oc=precio)
        InvoicingService.aprobar_factura(factura, self.u_compras)
        factura.refresh_from_db()
        return factura, po_line, entrada


class FacturaPreliminarAPITests(FacturaSAPAPITestBase):
    """GET facturas-pendientes-preliminar/ + confirmar-preliminar/ + reportar-error/."""

    @override_settings(FACTURA_DRAFT_SAP_HABILITADO=True)
    def test_get_lista_solo_aprobada_compras_con_estado_sap_L(self):
        factura, *_ = self._factura_aprobada_con_grpo(slot_date='2026-10-01')
        resp = self.api.get('/api/v1/facturas-pendientes-preliminar/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(factura.id, [r['id'] for r in resp.data])

        factura.estado_sap = 'B'
        factura.save(update_fields=['estado_sap'])
        resp2 = self.api.get('/api/v1/facturas-pendientes-preliminar/')
        self.assertNotIn(
            factura.id, [r['id'] for r in resp2.data],
            'Una Factura ya en B no debe listarse como pendiente de Preliminar.',
        )

    @override_settings(FACTURA_DRAFT_SAP_HABILITADO=True)
    def test_payload_incluye_udf_sap_y_referencia_al_grpo(self):
        """Punto 1 del pedido: todos los datos que el demonio necesita, con los nombres reales de SAP."""
        factura, po_line, entrada = self._factura_aprobada_con_grpo(
            slot_date='2026-10-01', grpo_doc_entry=7777, serie='F002', numero='321',
        )
        resp = self.api.get('/api/v1/facturas-pendientes-preliminar/')
        row = next(r for r in resp.data if r['id'] == factura.id)

        self.assertEqual(row['card_code'], factura.proveedor.sap_card_code)
        self.assertEqual(row['sede_codigo'], factura.sede.codigo)
        self.assertEqual(row['doc_cur'], 'PEN')
        self.assertEqual(row['num_at_card'], 'F002-321')
        self.assertEqual(row['U_MSSL_FPC'], 'F002')
        self.assertEqual(row['U_MSSL_FNC'], '321')
        self.assertEqual(row['U_MSSL_TOP'], '02')
        self.assertEqual(row['U_MSSL_CBS'], 1)
        self.assertEqual(len(row['lineas']), 1)

        linea = row['lineas'][0]
        self.assertEqual(linea['item_code'], po_line.item_code)
        self.assertEqual(linea['base_line'], po_line.line_num)
        self.assertEqual(Decimal(linea['cantidad']), Decimal('10.0000'))
        self.assertEqual(linea['grpo_doc_entry'], 7777)
        self.assertEqual(linea['grpo_estado_sap'], 'Y')

    @override_settings(FACTURA_DRAFT_SAP_HABILITADO=True)
    def test_confirmar_preliminar_marca_B_y_guarda_doc_entry(self):
        factura, *_ = self._factura_aprobada_con_grpo(slot_date='2026-10-01')
        resp = self.api.post(
            f'/api/v1/facturas-pendientes-preliminar/{factura.id}/confirmar-preliminar/',
            {'doc_entry_preliminar': 90101}, format='json',
        )
        self.assertEqual(resp.status_code, 200)
        factura.refresh_from_db()
        self.assertEqual(factura.estado_sap, 'B')
        self.assertEqual(factura.doc_entry_preliminar, '90101')
        self.assertIsNotNone(factura.fecha_preliminar_confirmado)

    @override_settings(FACTURA_DRAFT_SAP_HABILITADO=True)
    def test_confirmar_preliminar_es_idempotente_en_reintento(self):
        """Reintentar con el MISMO doc_entry no falla, solo actualiza — punto 7 del pedido."""
        factura, *_ = self._factura_aprobada_con_grpo(slot_date='2026-10-01')
        r1 = self.api.post(
            f'/api/v1/facturas-pendientes-preliminar/{factura.id}/confirmar-preliminar/',
            {'doc_entry_preliminar': 90102}, format='json',
        )
        r2 = self.api.post(
            f'/api/v1/facturas-pendientes-preliminar/{factura.id}/confirmar-preliminar/',
            {'doc_entry_preliminar': 90102}, format='json',
        )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        factura.refresh_from_db()
        self.assertEqual(factura.doc_entry_preliminar, '90102')
        self.assertEqual(factura.estado_sap, 'B')

    @override_settings(FACTURA_DRAFT_SAP_HABILITADO=True)
    def test_confirmar_preliminar_sin_doc_entry_devuelve_400(self):
        factura, *_ = self._factura_aprobada_con_grpo(slot_date='2026-10-01')
        resp = self.api.post(
            f'/api/v1/facturas-pendientes-preliminar/{factura.id}/confirmar-preliminar/', {}, format='json',
        )
        self.assertEqual(resp.status_code, 400)
        factura.refresh_from_db()
        self.assertEqual(factura.estado_sap, 'L')

    @override_settings(FACTURA_DRAFT_SAP_HABILITADO=True)
    def test_reportar_error_guarda_mensaje_sin_tocar_estado_sap(self):
        """Mismo patrón que EntradaMercaderia.reportar_error (sesión 58) — punto 2 del pedido."""
        factura, *_ = self._factura_aprobada_con_grpo(slot_date='2026-10-01')
        resp = self.api.post(
            f'/api/v1/facturas-pendientes-preliminar/{factura.id}/reportar-error/',
            {'error_mensaje': 'SAP rechazó: CardCode inexistente.'}, format='json',
        )
        self.assertEqual(resp.status_code, 200)
        factura.refresh_from_db()
        self.assertEqual(factura.estado_sap, 'L')
        self.assertEqual(factura.error_mensaje_sap, 'SAP rechazó: CardCode inexistente.')

        # Sigue lista para que el daemon la reintente en el siguiente ciclo.
        resp2 = self.api.get('/api/v1/facturas-pendientes-preliminar/')
        self.assertIn(factura.id, [r['id'] for r in resp2.data])

    @override_settings(FACTURA_DRAFT_SAP_HABILITADO=True)
    def test_reportar_error_sin_mensaje_devuelve_400(self):
        factura, *_ = self._factura_aprobada_con_grpo(slot_date='2026-10-01')
        resp = self.api.post(
            f'/api/v1/facturas-pendientes-preliminar/{factura.id}/reportar-error/', {}, format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_sin_token_devuelve_401(self):
        api_sin_token = APIClient()
        resp = api_sin_token.get('/api/v1/facturas-pendientes-preliminar/')
        self.assertEqual(resp.status_code, 401)


class FacturaReconciliacionAPITests(FacturaSAPAPITestBase):
    """GET facturas-preliminares/ (reconciliación) + confirmar-definitivo/ — puntos 3-4 del pedido."""

    @override_settings(FACTURA_DRAFT_SAP_HABILITADO=True)
    def test_get_lista_solo_estado_sap_B(self):
        factura_b, *_ = self._factura_aprobada_con_grpo(slot_date='2026-10-01', grpo_doc_entry=7001)
        factura_b.doc_entry_preliminar = '90200'
        factura_b.estado_sap = 'B'
        factura_b.save(update_fields=['doc_entry_preliminar', 'estado_sap'])

        factura_l, *_ = self._factura_aprobada_con_grpo(slot_date='2026-10-02', grpo_doc_entry=7002)

        resp = self.api.get('/api/v1/facturas-preliminares/')
        self.assertEqual(resp.status_code, 200)
        ids = [r['id'] for r in resp.data]
        self.assertIn(factura_b.id, ids)
        self.assertNotIn(factura_l.id, ids, 'Una Factura todavía en L (sin Preliminar) no es de reconciliación.')

    @override_settings(FACTURA_DRAFT_SAP_HABILITADO=True)
    def test_confirmar_definitivo_marca_Y_y_guarda_doc_entry(self):
        factura, *_ = self._factura_aprobada_con_grpo(slot_date='2026-10-01')
        factura.doc_entry_preliminar = '90201'
        factura.estado_sap = 'B'
        factura.save(update_fields=['doc_entry_preliminar', 'estado_sap'])

        resp = self.api.post(
            f'/api/v1/facturas-preliminares/{factura.id}/confirmar-definitivo/',
            {'doc_entry_definitivo': 90202}, format='json',
        )
        self.assertEqual(resp.status_code, 200)
        factura.refresh_from_db()
        self.assertEqual(factura.estado_sap, 'Y')
        self.assertEqual(factura.doc_entry_definitivo, '90202')
        self.assertIsNotNone(factura.fecha_definitivo_confirmado)

    @override_settings(FACTURA_DRAFT_SAP_HABILITADO=True)
    def test_confirmar_definitivo_es_idempotente_en_reintento(self):
        factura, *_ = self._factura_aprobada_con_grpo(slot_date='2026-10-01')
        factura.doc_entry_preliminar = '90203'
        factura.estado_sap = 'B'
        factura.save(update_fields=['doc_entry_preliminar', 'estado_sap'])

        r1 = self.api.post(
            f'/api/v1/facturas-preliminares/{factura.id}/confirmar-definitivo/',
            {'doc_entry_definitivo': 90204}, format='json',
        )
        r2 = self.api.post(
            f'/api/v1/facturas-preliminares/{factura.id}/confirmar-definitivo/',
            {'doc_entry_definitivo': 90204}, format='json',
        )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        factura.refresh_from_db()
        self.assertEqual(factura.estado_sap, 'Y')

    @override_settings(FACTURA_DRAFT_SAP_HABILITADO=True)
    def test_confirmar_definitivo_sin_doc_entry_devuelve_400(self):
        factura, *_ = self._factura_aprobada_con_grpo(slot_date='2026-10-01')
        factura.estado_sap = 'B'
        factura.save(update_fields=['estado_sap'])
        resp = self.api.post(
            f'/api/v1/facturas-preliminares/{factura.id}/confirmar-definitivo/', {}, format='json',
        )
        self.assertEqual(resp.status_code, 400)


class FacturaCancelacionAPITests(FacturaSAPAPITestBase):
    """GET facturas-pendientes-cancelacion/ + confirmar-cancelacion/ — punto 5 del pedido."""

    @override_settings(FACTURA_DRAFT_SAP_HABILITADO=True)
    def test_get_lista_solo_cancelado_con_preliminar_en_sap(self):
        factura_cancelada_con_b, *_ = self._factura_aprobada_con_grpo(slot_date='2026-10-01', grpo_doc_entry=7101)
        factura_cancelada_con_b.doc_entry_preliminar = '90300'
        factura_cancelada_con_b.estado_sap = 'B'
        factura_cancelada_con_b.estado = 'CANCELADO'
        factura_cancelada_con_b.save(update_fields=['doc_entry_preliminar', 'estado_sap', 'estado'])

        # Cancelada pero SIN Preliminar en SAP todavía (estado_sap='L') —
        # no hay nada que anular del lado de SAP, no debe listarse aquí.
        factura_cancelada_sin_b, *_ = self._factura_aprobada_con_grpo(slot_date='2026-10-02', grpo_doc_entry=7102)
        factura_cancelada_sin_b.estado = 'CANCELADO'
        factura_cancelada_sin_b.save(update_fields=['estado'])

        resp = self.api.get('/api/v1/facturas-pendientes-cancelacion/')
        self.assertEqual(resp.status_code, 200)
        ids = [r['id'] for r in resp.data]
        self.assertIn(factura_cancelada_con_b.id, ids)
        self.assertNotIn(factura_cancelada_sin_b.id, ids)

    @override_settings(FACTURA_DRAFT_SAP_HABILITADO=True)
    def test_confirmar_cancelacion_marca_C(self):
        factura, *_ = self._factura_aprobada_con_grpo(slot_date='2026-10-01')
        factura.doc_entry_preliminar = '90301'
        factura.estado_sap = 'B'
        factura.estado = 'CANCELADO'
        factura.save(update_fields=['doc_entry_preliminar', 'estado_sap', 'estado'])

        resp = self.api.post(
            f'/api/v1/facturas-pendientes-cancelacion/{factura.id}/confirmar-cancelacion/', {}, format='json',
        )
        self.assertEqual(resp.status_code, 200)
        factura.refresh_from_db()
        self.assertEqual(factura.estado_sap, 'C')
        self.assertIsNotNone(factura.fecha_cancelado_sap)

    @override_settings(FACTURA_DRAFT_SAP_HABILITADO=True)
    def test_confirmar_cancelacion_es_idempotente_en_reintento(self):
        factura, *_ = self._factura_aprobada_con_grpo(slot_date='2026-10-01')
        factura.doc_entry_preliminar = '90302'
        factura.estado_sap = 'B'
        factura.estado = 'CANCELADO'
        factura.save(update_fields=['doc_entry_preliminar', 'estado_sap', 'estado'])

        r1 = self.api.post(
            f'/api/v1/facturas-pendientes-cancelacion/{factura.id}/confirmar-cancelacion/', {}, format='json',
        )
        r2 = self.api.post(
            f'/api/v1/facturas-pendientes-cancelacion/{factura.id}/confirmar-cancelacion/', {}, format='json',
        )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        factura.refresh_from_db()
        self.assertEqual(factura.estado_sap, 'C')


class InterruptorFacturaDraftSapTests(FacturaSAPAPITestBase):
    """
    Punto 6 del pedido: FACTURA_DRAFT_SAP_HABILITADO — verificado SIN
    @override_settings en ninguno de estos tests (usan el valor real por
    defecto de settings.py, el mismo que producción tiene hoy — sesión
    66/67: la variable no está configurada en Railway) y con datos
    reales que SÍ calificarían para cada lista si el flag estuviera en
    True — no alcanza con una BD vacía, que probaría poco.
    """

    def test_valor_por_defecto_es_false(self):
        from django.conf import settings
        self.assertFalse(
            settings.FACTURA_DRAFT_SAP_HABILITADO,
            'El default de settings.py debe ser False — mismo valor que producción hoy.',
        )

    def test_flag_apagado_vacia_lista_de_preliminar_pese_a_datos_calificados(self):
        self._factura_aprobada_con_grpo(slot_date='2026-10-01')
        resp = self.api.get('/api/v1/facturas-pendientes-preliminar/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(list(resp.data), [])

    def test_flag_apagado_bloquea_confirmar_preliminar_pese_a_id_real(self):
        factura, *_ = self._factura_aprobada_con_grpo(slot_date='2026-10-01')
        resp = self.api.post(
            f'/api/v1/facturas-pendientes-preliminar/{factura.id}/confirmar-preliminar/',
            {'doc_entry_preliminar': 1}, format='json',
        )
        self.assertEqual(resp.status_code, 404)
        factura.refresh_from_db()
        self.assertEqual(factura.estado_sap, 'L', 'Sin cambios — el flag apagado bloqueó la confirmación.')

    def test_flag_apagado_vacia_lista_de_reconciliacion_pese_a_datos_calificados(self):
        factura, *_ = self._factura_aprobada_con_grpo(slot_date='2026-10-01')
        factura.doc_entry_preliminar = '1'
        factura.estado_sap = 'B'
        factura.save(update_fields=['doc_entry_preliminar', 'estado_sap'])

        resp = self.api.get('/api/v1/facturas-preliminares/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(list(resp.data), [])

    def test_flag_apagado_bloquea_confirmar_definitivo_pese_a_id_real(self):
        factura, *_ = self._factura_aprobada_con_grpo(slot_date='2026-10-01')
        factura.estado_sap = 'B'
        factura.save(update_fields=['estado_sap'])

        resp = self.api.post(
            f'/api/v1/facturas-preliminares/{factura.id}/confirmar-definitivo/',
            {'doc_entry_definitivo': 1}, format='json',
        )
        self.assertEqual(resp.status_code, 404)
        factura.refresh_from_db()
        self.assertEqual(factura.estado_sap, 'B')

    def test_flag_apagado_vacia_lista_de_cancelacion_pese_a_datos_calificados(self):
        factura, *_ = self._factura_aprobada_con_grpo(slot_date='2026-10-01')
        factura.doc_entry_preliminar = '1'
        factura.estado_sap = 'B'
        factura.estado = 'CANCELADO'
        factura.save(update_fields=['doc_entry_preliminar', 'estado_sap', 'estado'])

        resp = self.api.get('/api/v1/facturas-pendientes-cancelacion/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(list(resp.data), [])

    def test_flag_apagado_bloquea_confirmar_cancelacion_pese_a_id_real(self):
        factura, *_ = self._factura_aprobada_con_grpo(slot_date='2026-10-01')
        factura.doc_entry_preliminar = '1'
        factura.estado_sap = 'B'
        factura.estado = 'CANCELADO'
        factura.save(update_fields=['doc_entry_preliminar', 'estado_sap', 'estado'])

        resp = self.api.post(
            f'/api/v1/facturas-pendientes-cancelacion/{factura.id}/confirmar-cancelacion/', {}, format='json',
        )
        self.assertEqual(resp.status_code, 404)
        factura.refresh_from_db()
        self.assertEqual(factura.estado_sap, 'B', 'Sin cambios — el flag apagado bloqueó la confirmación.')

    @override_settings(FACTURA_DRAFT_SAP_HABILITADO=True)
    def test_encender_el_flag_revela_los_mismos_datos_ya_probados_apagado(self):
        """
        Contraprueba explícita: la MISMA Factura que con el flag apagado
        devolvía lista vacía y 404 en las confirmaciones, con el flag
        encendido (@override_settings) sí aparece y sí acepta la
        confirmación — descarta que el flag esté simplemente rompiendo
        el endpoint por otro motivo (un `get_queryset` mal filtrado
        podría devolver vacío también con el flag en True; esta prueba
        confirma que el comportamiento cambia EXACTAMENTE al togglear el
        flag, no por casualidad).
        """
        factura, *_ = self._factura_aprobada_con_grpo(slot_date='2026-10-01')
        resp = self.api.get('/api/v1/facturas-pendientes-preliminar/')
        self.assertIn(factura.id, [r['id'] for r in resp.data])

        resp2 = self.api.post(
            f'/api/v1/facturas-pendientes-preliminar/{factura.id}/confirmar-preliminar/',
            {'doc_entry_preliminar': 90999}, format='json',
        )
        self.assertEqual(resp2.status_code, 200)
        factura.refresh_from_db()
        self.assertEqual(factura.estado_sap, 'B')


class CandadoEstadoSapYBloqueaEdicionTests(InvoicingTestBase):
    """
    Punto 4 del pedido: verifica EXPLÍCITAMENTE que estado_sap='Y'
    bloquea la edición incluso si `estado` fuera (hipotéticamente) uno
    de los editables — construyendo a propósito esa combinación
    inconsistente (nunca alcanzable por el flujo real: estado_sap solo
    avanza más allá de '' cuando `estado` ya es APROBADA_COMPRAS, fuera
    de ESTADOS_CARGA_PERMITIDA) para probar que el candado de
    estado_sap actúa de forma INDEPENDIENTE al de `estado`, no solo
    transitivamente a través de él — "no confíes en que el estado de
    negocio ya lo garantizaba".
    """

    def test_estado_sap_Y_bloquea_pese_a_estado_BORRADOR_editable(self):
        factura = self._crear_factura(estado='BORRADOR')
        factura.estado_sap = 'Y'
        factura.save(update_fields=['estado_sap'])

        with self.assertRaises(ValidationError) as ctx:
            sa.validar_permiso_edicion(factura, self.proveedor)
        self.assertIn('SAP', str(ctx.exception))

    def test_estado_sap_Y_bloquea_pese_a_estado_OBSERVADA_editable(self):
        factura = self._crear_factura(estado='OBSERVADA')
        factura.estado_sap = 'Y'
        factura.save(update_fields=['estado_sap'])

        with self.assertRaises(ValidationError):
            sa.validar_permiso_edicion(factura, self.proveedor)

    def test_estado_sap_Y_bloquea_carga_de_archivo(self):
        factura = self._crear_factura(estado='OBSERVADA')
        factura.estado_sap = 'Y'
        factura.save(update_fields=['estado_sap'])

        archivo = SimpleUploadedFile('f.xml', b'<a/>', content_type='application/xml')
        with self.assertRaises(ValidationError):
            sa.cargar_archivo_factura(factura, 'xml', archivo, self.proveedor)

    def test_estado_sap_B_no_bloquea_por_si_solo(self):
        """
        Solo 'Y' (documento definitivo) bloquea explícitamente — 'B'
        (Preliminar, todavía reversible del lado de SAP) NO dispara el
        candado nuevo: con `estado` en un valor editable (OBSERVADA) y
        estado_sap='B', validar_permiso_edicion no debe lanzar nada.
        Confirma que el candado nuevo está acotado exactamente a 'Y', no
        a "cualquier estado_sap no vacío" — una combinación (estado
        editable + estado_sap='B') que nunca ocurre en el flujo real
        (estado_sap solo avanza más allá de '' cuando `estado` ya es
        APROBADA_COMPRAS) pero que aquí se fuerza para aislar el
        comportamiento exacto del candado nuevo, igual que el resto de
        este bloque.
        """
        factura = self._crear_factura(estado='OBSERVADA')
        factura.estado_sap = 'B'
        factura.save(update_fields=['estado_sap'])

        sa.validar_permiso_edicion(factura, self.proveedor)  # no debe lanzar

    def test_estado_APROBADA_COMPRAS_con_estado_sap_B_bloquea_por_el_candado_de_estado(self):
        """
        Camino REAL (no forzado): tras aprobar_factura, `estado` ya es
        APROBADA_COMPRAS (fuera de ESTADOS_CARGA_PERMITIDA) incluso antes
        de que estado_sap llegue a 'Y' — el candado de `estado` ya
        bloquea desde 'L'/'B' en adelante, con su mensaje genérico (no el
        específico de SAP, que solo dispara con 'Y').
        """
        factura = self._crear_factura(estado='APROBADA_COMPRAS')
        factura.estado_sap = 'B'
        factura.save(update_fields=['estado_sap'])

        with self.assertRaises(ValidationError) as ctx:
            sa.validar_permiso_edicion(factura, self.proveedor)
        self.assertNotIn('SAP', str(ctx.exception))


class FlujoCompletoDaemonFacturaTests(FacturaSAPAPITestBase):
    """
    Punto 7 del pedido: flujo completo simulado de punta a punta —
    Factura aprobada -> el demonio la toma -> confirma Preliminar ->
    reconciliación -> confirma definitivo (y, en un segundo test,
    cancelación desde un Preliminar ya confirmado).
    """

    @override_settings(FACTURA_DRAFT_SAP_HABILITADO=True)
    def test_flujo_completo_aprobada_preliminar_reconciliacion_definitivo(self):
        factura, po_line, entrada = self._factura_aprobada_con_grpo(
            slot_date='2026-10-01', grpo_doc_entry=6001,
        )
        self.assertEqual(factura.estado, 'APROBADA_COMPRAS')
        self.assertEqual(factura.estado_sap, 'L')

        # 1. El demonio la toma.
        resp = self.api.get('/api/v1/facturas-pendientes-preliminar/')
        self.assertIn(factura.id, [r['id'] for r in resp.data])

        # 2. Confirma el Preliminar.
        resp = self.api.post(
            f'/api/v1/facturas-pendientes-preliminar/{factura.id}/confirmar-preliminar/',
            {'doc_entry_preliminar': 91001}, format='json',
        )
        self.assertEqual(resp.status_code, 200)
        factura.refresh_from_db()
        self.assertEqual(factura.estado_sap, 'B')

        # Ya no está pendiente de Preliminar.
        resp = self.api.get('/api/v1/facturas-pendientes-preliminar/')
        self.assertNotIn(factura.id, [r['id'] for r in resp.data])

        # 3. Reconciliación: aparece pendiente del definitivo.
        resp = self.api.get('/api/v1/facturas-preliminares/')
        self.assertIn(factura.id, [r['id'] for r in resp.data])

        # 4. Confirma el definitivo.
        resp = self.api.post(
            f'/api/v1/facturas-preliminares/{factura.id}/confirmar-definitivo/',
            {'doc_entry_definitivo': 91002}, format='json',
        )
        self.assertEqual(resp.status_code, 200)
        factura.refresh_from_db()
        self.assertEqual(factura.estado_sap, 'Y')
        self.assertEqual(factura.doc_entry_preliminar, '91001')
        self.assertEqual(factura.doc_entry_definitivo, '91002')

        # Ya no aparece en reconciliación.
        resp = self.api.get('/api/v1/facturas-preliminares/')
        self.assertNotIn(factura.id, [r['id'] for r in resp.data])

        # Fin del ciclo: ninguna edición se acepta ya (mismo candado del
        # bloque anterior, esta vez alcanzado por el camino REAL, no
        # forzado a mano).
        with self.assertRaises(ValidationError):
            sa.validar_permiso_edicion(factura, self.proveedor)

    @override_settings(FACTURA_DRAFT_SAP_HABILITADO=True)
    def test_flujo_completo_cancelacion_desde_preliminar_ya_confirmado(self):
        factura, *_ = self._factura_aprobada_con_grpo(slot_date='2026-10-01', grpo_doc_entry=6002)
        self.api.post(
            f'/api/v1/facturas-pendientes-preliminar/{factura.id}/confirmar-preliminar/',
            {'doc_entry_preliminar': 91003}, format='json',
        )
        factura.refresh_from_db()
        self.assertEqual(factura.estado_sap, 'B')

        # La Factura se cancela — construir el flujo real de negocio para
        # llegar a CANCELADO es explícitamente Sub-fase 3.7+ (no pedido
        # en esta sesión); se fuerza el estado directamente, mismo
        # criterio ya usado en el resto de esta suite para probar
        # candados de forma aislada (p. ej. SaldoDisponibleTests.
        # test_saldo_ignora_lineas_de_factura_cancelada).
        factura.estado = 'CANCELADO'
        factura.save(update_fields=['estado'])

        resp = self.api.get('/api/v1/facturas-pendientes-cancelacion/')
        self.assertIn(factura.id, [r['id'] for r in resp.data])

        resp = self.api.post(
            f'/api/v1/facturas-pendientes-cancelacion/{factura.id}/confirmar-cancelacion/', {}, format='json',
        )
        self.assertEqual(resp.status_code, 200)
        factura.refresh_from_db()
        self.assertEqual(factura.estado_sap, 'C')
        self.assertIsNotNone(factura.fecha_cancelado_sap)

        resp = self.api.get('/api/v1/facturas-pendientes-cancelacion/')
        self.assertNotIn(factura.id, [r['id'] for r in resp.data])


# ═══════════════════════════════════════════════════════════════════════════
# Sub-fase 3.7 (esta sesión) — apps.base.oc_status: estado de FACTURACIÓN
# de la columna del Panel de Consulta de OC (cierre de la Fase 3
# completa). Los 3 estados contra datos reales + verificación explícita
# de ausencia de N+1.
# ═══════════════════════════════════════════════════════════════════════════

class EstadoFacturacionOCTests(InvoicingTestBase):
    """
    calcular_estado_facturacion — los 3 estados (punto 1 del pedido),
    cada uno contra un Ticket/OC real construido de punta a punta (nunca
    escribiendo estado directo sobre PurchaseOrder, mismo criterio ya
    establecido en toda la suite).
    """

    def test_sin_ninguna_factura_es_sin_facturar(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po = self._po_line_de(ticket).purchase_order

        estado, factura_id, _ = oc_status.calcular_estado_facturacion(po)

        self.assertEqual(estado, oc_status.ESTADO_FACT_SIN_FACTURAR)
        self.assertIsNone(factura_id)

    def _oc_con_factura(self, estado_factura):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)
        po = po_line.purchase_order
        factura = Factura.objects.create(
            proveedor=self._supplier_profile(), sede=self._sede(), estado=estado_factura,
        )
        FacturaOrdenCompra.objects.create(factura=factura, purchase_order=po)
        self._crear_factura_linea(factura, po_line, cantidad=Decimal('10.0000'))
        return po, factura

    def test_factura_en_borrador_es_facturacion_en_curso(self):
        po, factura = self._oc_con_factura('BORRADOR')
        estado, factura_id, _ = oc_status.calcular_estado_facturacion(po)
        self.assertEqual(estado, oc_status.ESTADO_FACT_EN_CURSO)
        self.assertEqual(factura_id, factura.id)

    def test_factura_en_revision_compras_es_facturacion_en_curso(self):
        po, factura = self._oc_con_factura('EN_REVISION_COMPRAS')
        estado, factura_id, _ = oc_status.calcular_estado_facturacion(po)
        self.assertEqual(estado, oc_status.ESTADO_FACT_EN_CURSO)
        self.assertEqual(factura_id, factura.id)

    def test_factura_observada_es_facturacion_en_curso(self):
        po, factura = self._oc_con_factura('OBSERVADA')
        estado, factura_id, _ = oc_status.calcular_estado_facturacion(po)
        self.assertEqual(estado, oc_status.ESTADO_FACT_EN_CURSO)
        self.assertEqual(factura_id, factura.id)

    def test_factura_aprobada_compras_es_facturada(self):
        """
        Camino REAL (no forzado): pasa por InvoicingService.aprobar_
        factura, con las 3 validaciones de firma/CDR/importe satisfechas
        — el mismo candado que ya se prueba en AprobarObservarFacturaTests,
        aquí solo para confirmar que oc_status lee el resultado correcto.
        """
        po, factura = self._oc_con_factura('EN_REVISION_COMPRAS')
        factura.firma_valida = True
        factura.estado_cdr = 'ACEPTADO'
        factura.importe_no_coincide = False
        factura.save(update_fields=['firma_valida', 'estado_cdr', 'importe_no_coincide'])
        InvoicingService.aprobar_factura(factura, self.u_compras)
        factura.refresh_from_db()
        self.assertEqual(factura.estado, 'APROBADA_COMPRAS')  # precondición del test

        estado, factura_id, _ = oc_status.calcular_estado_facturacion(po)

        self.assertEqual(estado, oc_status.ESTADO_FACT_FACTURADA)
        self.assertEqual(factura_id, factura.id)

    def test_factura_cancelada_vuelve_a_sin_facturar(self):
        """
        Punto 1 del pedido, caso implícito: una Factura CANCELADO no
        cuenta como "activa" — mismo criterio ya establecido en
        InvoicingService.validar_oc_disponible/saldo_disponible. La OC
        vuelve a SIN_FACTURAR, no queda "en curso" ni "facturada".
        """
        po, factura = self._oc_con_factura('BORRADOR')
        factura.estado = 'CANCELADO'
        factura.save(update_fields=['estado'])

        estado, factura_id, _ = oc_status.calcular_estado_facturacion(po)

        self.assertEqual(estado, oc_status.ESTADO_FACT_SIN_FACTURAR)
        self.assertIsNone(factura_id)


class ConstruirFilasEstadoOcFacturacionTests(NuevaFacturaTestBase):
    """
    construir_filas_estado_oc — combina despacho + facturación en una
    sola pasada, sobre 3 OC reales con combinaciones distintas a
    propósito (ninguna cita, cita finalizada sin Factura, cita
    finalizada CON Factura aprobada) para confirmar que ambas columnas
    se calculan correctamente juntas, no solo cada una por separado.
    """

    def _oc_pendiente_sin_ticket(self, suffix):
        doc_num = next(_doc_num_counter)
        po = PurchaseOrder.objects.create(
            doc_entry=doc_num, doc_num=doc_num, card_code='TESTCODE', card_name=f'TEST SAC {suffix}',
            e_mail='t@t.com', status='PENDIENTE', u_mss_tdb='CDL',
        )
        PurchaseOrderLine.objects.create(
            purchase_order=po, line_num=1, item_code=f'ITEM-{doc_num}', description='Item de prueba',
            quantity_sap=10, und_medida='KG', requiere_coa=False,
        )
        return po

    def test_construir_filas_calcula_facturacion_junto_con_despacho(self):
        po_pendiente = self._oc_pendiente_sin_ticket('A')

        ticket_sin_factura = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_sin_factura = self._po_line_de(ticket_sin_factura).purchase_order

        ticket_facturada = self._finalizar_segundo_ticket(cantidad_real=Decimal('10.0000'))
        po_line_facturada = ticket_facturada.appointment.purchase_orders.first().lines.first()
        po_facturada = po_line_facturada.purchase_order
        factura = Factura.objects.create(
            proveedor=self._perfil_oc(), sede=self._sede(), estado='EN_REVISION_COMPRAS',
            firma_valida=True, estado_cdr='ACEPTADO', importe_no_coincide=False,
        )
        FacturaOrdenCompra.objects.create(factura=factura, purchase_order=po_facturada)
        self._crear_factura_linea(factura, po_line_facturada, cantidad=Decimal('10.0000'))
        InvoicingService.aprobar_factura(factura, self.u_compras)
        factura.refresh_from_db()

        filas = oc_status.construir_filas_estado_oc(
            PurchaseOrder.objects.filter(
                id__in=[po_pendiente.id, po_sin_factura.id, po_facturada.id],
            )
        )
        por_oc = {f.purchase_order.id: f for f in filas}
        self.assertEqual(len(por_oc), 3)

        self.assertEqual(por_oc[po_pendiente.id].estado, oc_status.ESTADO_PENDIENTE)
        self.assertEqual(por_oc[po_pendiente.id].estado_facturacion, oc_status.ESTADO_FACT_SIN_FACTURAR)
        self.assertIsNone(por_oc[po_pendiente.id].factura_id)

        self.assertEqual(por_oc[po_sin_factura.id].estado, oc_status.ESTADO_DESPACHADA)
        self.assertEqual(por_oc[po_sin_factura.id].estado_facturacion, oc_status.ESTADO_FACT_SIN_FACTURAR)
        self.assertIsNone(por_oc[po_sin_factura.id].factura_id)

        self.assertEqual(por_oc[po_facturada.id].estado, oc_status.ESTADO_DESPACHADA)
        self.assertEqual(por_oc[po_facturada.id].estado_facturacion, oc_status.ESTADO_FACT_FACTURADA)
        self.assertEqual(por_oc[po_facturada.id].factura_id, factura.id)


class ConstruirFilasEstadoOcSinN1Tests(InvoicingTestBase):
    """
    Punto 2 del pedido: verifica que la columna de facturación NO
    reintroduce el problema de N+1 ya cuidado en la sesión 52 — con
    CaptureQueriesContext (Django estándar; sin precedente de
    assertNumQueries/CaptureQueriesContext en el proyecto hasta ahora,
    así que se introduce aquí, tal como el pedido lo permitía
    explícitamente: "de la forma que ya sea estándar aquí" — no había
    ninguna).

    BUG REAL encontrado al escribir esta verificación, no solo evitado
    en el código nuevo: calcular_estado_despacho (sesión 52) tenía
    EXACTAMENTE este problema para el caso "OC sin ningún Appointment
    activo" (PENDIENTE) — nunca se había cerrado del todo. Corregido en
    apps/base/oc_status.py (sentinel `_SIN_RESOLVER`, no `None`, como
    valor por defecto) — ver el docstring de calcular_estado_despacho
    para el detalle completo, incluida la medición real (14 queries para
    5 OC pendientes, antes del fix).
    """

    def _oc_pendiente(self, suffix):
        doc_num = next(_doc_num_counter)
        po = PurchaseOrder.objects.create(
            doc_entry=doc_num, doc_num=doc_num, card_code='TESTCODE', card_name=f'TEST SAC {suffix}',
            e_mail='t@t.com', status='PENDIENTE', u_mss_tdb='CDL',
        )
        PurchaseOrderLine.objects.create(
            purchase_order=po, line_num=1, item_code=f'ITEM-{doc_num}', description='Item de prueba',
            quantity_sap=10, und_medida='KG', requiere_coa=False,
        )
        return po

    def test_query_count_no_crece_con_el_volumen_de_oc_pendientes(self):
        """
        La prueba directa de "sin N+1": el número de queries debe ser el
        MISMO sin importar cuántas OC se procesen — si creciera de forma
        lineal con el volumen de datos, sería un N+1 real. Se usan OC
        PENDIENTE (el caso donde el bug real vivía — ver docstring de la
        clase) a propósito, no datos "ricos".
        """
        pos_5 = [self._oc_pendiente(i) for i in range(5)]
        with CaptureQueriesContext(connection) as ctx_5:
            filas_5 = oc_status.construir_filas_estado_oc(
                PurchaseOrder.objects.filter(id__in=[p.id for p in pos_5])
            )

        pos_15 = pos_5 + [self._oc_pendiente(i) for i in range(5, 15)]
        with CaptureQueriesContext(connection) as ctx_15:
            filas_15 = oc_status.construir_filas_estado_oc(
                PurchaseOrder.objects.filter(id__in=[p.id for p in pos_15])
            )

        self.assertEqual(len(filas_5), 5)
        self.assertEqual(len(filas_15), 15)
        self.assertEqual(
            len(ctx_5.captured_queries), len(ctx_15.captured_queries),
            f"El conteo de queries creció con el volumen de datos "
            f"({len(ctx_5.captured_queries)} -> {len(ctx_15.captured_queries)}) — indicio de N+1.",
        )
        self.assertLessEqual(
            len(ctx_5.captured_queries), 6,
            "Más de las 6 queries esperadas (1 principal + 3 prefetch "
            "appointment_set/lines/facturas_oc + 2 de agregación "
            "recibido/facturado por línea, sesión 99c).",
        )

    def test_query_count_no_crece_con_una_factura_real_presente(self):
        """
        Mismo chequeo, con datos "ricos" (Ticket FINALIZADO real +
        Factura real APROBADA_COMPRAS) mezclados con OC PENDIENTE — para
        confirmar que el prefetch de facturas_oc tampoco genera una
        query extra por fila cuando SÍ hay algo que traer, no solo
        cuando está vacío.
        """
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)
        po_facturada = po_line.purchase_order
        factura = Factura.objects.create(
            proveedor=self._supplier_profile(), sede=self._sede(), estado='EN_REVISION_COMPRAS',
            firma_valida=True, estado_cdr='ACEPTADO', importe_no_coincide=False,
        )
        FacturaOrdenCompra.objects.create(factura=factura, purchase_order=po_facturada)
        self._crear_factura_linea(factura, po_line, cantidad=Decimal('10.0000'))
        InvoicingService.aprobar_factura(factura, self.u_compras)

        pos_extra = [self._oc_pendiente(i) for i in range(5)]

        with CaptureQueriesContext(connection) as ctx:
            filas = oc_status.construir_filas_estado_oc(
                PurchaseOrder.objects.filter(id__in=[po_facturada.id] + [p.id for p in pos_extra])
            )

        self.assertEqual(len(filas), 6)
        por_oc = {f.purchase_order.id: f for f in filas}
        self.assertEqual(por_oc[po_facturada.id].estado_facturacion, oc_status.ESTADO_FACT_FACTURADA)
        self.assertEqual(por_oc[po_facturada.id].factura_id, factura.id)
        self.assertLessEqual(
            len(ctx.captured_queries), 6,
            "El prefetch de facturas_oc (o una agregación) generó una query extra por fila con datos reales presentes.",
        )


class OcParcialTests(InvoicingTestBase):
    """
    Sesión 99c — OC abierta con N despachos parciales:
      - saldo_disponible suma TODAS las rondas CREADO_SAP (antes un .get()
        singular que reventaba con MultipleObjectsReturned).
      - solicitar_cita_borrador permite una 2da cita mientras quede saldo,
        bloquea si hay un ciclo en curso o si la OC ya está completa.
      - validar_oc_disponible / _pos_con_factura_en_vuelo dejan facturar
        varias veces la misma OC (una APROBADA_COMPRAS no bloquea).
      - construir_filas_estado_oc devuelve DESPACHO_PARCIAL /
        FACTURACION_PARCIAL + cantidades.
    """

    def _po_de(self, ticket):
        return ticket.appointment.purchase_orders.first()

    def _segunda_ronda(self, po, cantidad_real, creado_sap=True):
        """
        Nueva cita para la MISMA `po` -> Ticket FINALIZADO -> 2da
        EntradaMercaderia (via el flujo real de servicios). `po` es
        comercial ('CDL', actor ALMACEN, sin COA/calidad).
        """
        slot = self._slot_futuro()
        appt = AppointmentService.solicitar_cita_borrador(
            user=self.proveedor, slot_id=slot.id, oc_ids=[po.id],
        )
        ticket = AppointmentService.confirmar_cita(
            appointment_id=appt.id, usuario_almacen=self.u_compras, base_url='http://testserver/',
        )
        ticket = OperationsService.iniciar_ingreso_planta(
            ticket_id=ticket.id, usuario_vigilancia=self.u_vigilancia,
        )
        OperationsService.autorizar_almacen(ticket_id=ticket.id, usuario=self.u_almacen)
        ticket.refresh_from_db()
        resultados = [
            {'inspeccion_id': i.id, 'estado': 'CONFORME', 'cantidad_modificada': str(cantidad_real)}
            for i in TicketLineInspection.objects.filter(ticket=ticket, etapa='ALMACEN')
        ]
        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=self.u_almacen, resultados=resultados,
        )
        ticket.refresh_from_db()
        OperationsService.registrar_salida(ticket_id=ticket.id, usuario_vigilancia=self.u_vigilancia)
        ticket.refresh_from_db()

        from apps.operations import services_entrada as _se
        entrada = EntradaMercaderia.objects.get(ticket=ticket)
        _se.enviar_a_sap(entrada, self.u_almacen)
        if creado_sap:
            entrada.refresh_from_db()
            entrada.estado = EntradaMercaderia.ESTADO_CREADO_SAP
            entrada.estado_sap = 'Y'
            entrada.save(update_fields=['estado', 'estado_sap'])
        return ticket

    # ── saldo_disponible ──────────────────────────────────────────────

    def test_saldo_disponible_suma_dos_rondas_creado_sap(self):
        """OC de 10: ronda 1 recibe 6, ronda 2 recibe 4 -> saldo 10."""
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('6.0000'))
        po_line = self._po_line_de(ticket)
        self.assertEqual(InvoicingService.saldo_disponible(po_line), Decimal('6.0000'))

        self._segunda_ronda(po_line.purchase_order, Decimal('4.0000'))
        self.assertEqual(InvoicingService.saldo_disponible(po_line), Decimal('10.0000'))

    def test_saldo_disponible_ignora_ronda_no_creado_sap(self):
        """Una 2da ronda ENVIADO (no CREADO_SAP) NO suma al saldo facturable."""
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('6.0000'))
        po_line = self._po_line_de(ticket)
        self._segunda_ronda(po_line.purchase_order, Decimal('4.0000'), creado_sap=False)
        self.assertEqual(InvoicingService.saldo_disponible(po_line), Decimal('6.0000'))

    # ── solicitar_cita_borrador ───────────────────────────────────────

    def test_segunda_cita_permitida_con_saldo_pendiente(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('6.0000'))
        po = self._po_de(ticket)
        slot = self._slot_futuro()
        appt = AppointmentService.solicitar_cita_borrador(
            user=self.proveedor, slot_id=slot.id, oc_ids=[po.id],
        )  # NO debe lanzar
        self.assertIsNotNone(appt.id)

    def test_segunda_cita_bloqueada_si_oc_totalmente_recibida(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('6.0000'))
        po = self._po_de(ticket)
        self._segunda_ronda(po, Decimal('4.0000'))  # total 10 == quantity_sap
        slot = self._slot_futuro()
        with self.assertRaises(ValidationError) as ctx:
            AppointmentService.solicitar_cita_borrador(
                user=self.proveedor, slot_id=slot.id, oc_ids=[po.id],
            )
        self.assertIn('recibidas completamente', '; '.join(ctx.exception.messages))

    def test_segunda_cita_bloqueada_si_ciclo_en_curso(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('6.0000'))
        po = self._po_de(ticket)
        slot1 = self._slot_futuro()
        AppointmentService.solicitar_cita_borrador(
            user=self.proveedor, slot_id=slot1.id, oc_ids=[po.id],
        )
        slot2 = self._slot_futuro()
        with self.assertRaises(ValidationError) as ctx:
            AppointmentService.solicitar_cita_borrador(
                user=self.proveedor, slot_id=slot2.id, oc_ids=[po.id],
            )
        self.assertIn('cita en curso', '; '.join(ctx.exception.messages))

    # ── validar_oc_disponible ─────────────────────────────────────────

    def test_factura_aprobada_no_bloquea_una_nueva_para_la_misma_oc(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)
        po = po_line.purchase_order

        f_aprobada = self._crear_factura(estado='APROBADA_COMPRAS')
        FacturaOrdenCompra.objects.create(factura=f_aprobada, purchase_order=po)
        self._crear_factura_linea(f_aprobada, po_line, cantidad=Decimal('4.0000'))

        InvoicingService.validar_oc_disponible(po)  # NO debe lanzar
        self.assertNotIn(po.id, list(sb._pos_con_factura_en_vuelo()))

        f_borrador = self._crear_factura(estado='BORRADOR')
        FacturaOrdenCompra.objects.create(factura=f_borrador, purchase_order=po)
        with self.assertRaises(ValidationError):
            InvoicingService.validar_oc_disponible(po)

    # ── oc_status: estados PARCIAL + cantidades ───────────────────────

    def test_despacho_parcial_con_progreso_cuantitativo(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('6.0000'))
        po = self._po_de(ticket)  # 6 de 10, sin cita en curso

        filas = oc_status.construir_filas_estado_oc(PurchaseOrder.objects.filter(id=po.id))
        self.assertEqual(len(filas), 1)
        fila = filas[0]
        self.assertEqual(fila.estado, oc_status.ESTADO_DESPACHO_PARCIAL)
        self.assertEqual(fila.cantidad_ordenada, Decimal('10.0000'))
        self.assertEqual(fila.cantidad_recibida, Decimal('6.0000'))
        self.assertEqual(fila.pct_recibido, 60)
        self.assertEqual(fila.progreso_despacho, '6 / 10')

    def test_despachada_cuando_todas_las_rondas_completan(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('6.0000'))
        po = self._po_de(ticket)
        self._segunda_ronda(po, Decimal('4.0000'))

        fila = oc_status.construir_filas_estado_oc(PurchaseOrder.objects.filter(id=po.id))[0]
        self.assertEqual(fila.estado, oc_status.ESTADO_DESPACHADA)
        self.assertEqual(fila.cantidad_recibida, Decimal('10.0000'))
        self.assertEqual(fila.pct_recibido, 100)

    def test_facturacion_parcial_con_aprobada_que_no_cubre_todo(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('10.0000'))
        po_line = self._po_line_de(ticket)
        po = po_line.purchase_order

        f = self._crear_factura(estado='APROBADA_COMPRAS')
        FacturaOrdenCompra.objects.create(factura=f, purchase_order=po)
        self._crear_factura_linea(f, po_line, cantidad=Decimal('4.0000'))

        fila = oc_status.construir_filas_estado_oc(PurchaseOrder.objects.filter(id=po.id))[0]
        self.assertEqual(fila.estado_facturacion, oc_status.ESTADO_FACT_PARCIAL)
        self.assertEqual(fila.cantidad_facturada, Decimal('4.0000'))
        self.assertEqual(fila.pct_facturado, 40)


# ═══════════════════════════════════════════════════════════════════════════
# Sesión 100 — Obs 2: cantidades consolidadas por LÍNEA (oc_status)
# ═══════════════════════════════════════════════════════════════════════════

class CantidadesConsolidadasPorLineaTests(NuevaFacturaTestBase):
    """
    oc_status.cantidades_consolidadas_por_linea: {po_line_id: {ordenada,
    atendida, disponible}} — las 2 lecturas (solo_confirmado True/False),
    el clamp de 'disponible' a 0 en sobre-recepción, y que NO es N+1.
    """

    def _segunda_ronda(self, po, cantidad_real, creado_sap=True):
        """Nueva cita para la MISMA `po` ('CDL', actor ALMACEN) → 2da
        EntradaMercaderia vía el flujo real de servicios (calco de
        OcParcialTests._segunda_ronda, sin heredar esa clase para no
        re-correr sus ~20 tests)."""
        slot = self._slot_futuro()
        appt = AppointmentService.solicitar_cita_borrador(
            user=self.proveedor, slot_id=slot.id, oc_ids=[po.id],
        )
        ticket = AppointmentService.confirmar_cita(
            appointment_id=appt.id, usuario_almacen=self.u_compras, base_url='http://testserver/',
        )
        ticket = OperationsService.iniciar_ingreso_planta(
            ticket_id=ticket.id, usuario_vigilancia=self.u_vigilancia,
        )
        OperationsService.autorizar_almacen(ticket_id=ticket.id, usuario=self.u_almacen)
        ticket.refresh_from_db()
        resultados = [
            {'inspeccion_id': i.id, 'estado': 'CONFORME', 'cantidad_modificada': str(cantidad_real)}
            for i in TicketLineInspection.objects.filter(ticket=ticket, etapa='ALMACEN')
        ]
        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=self.u_almacen, resultados=resultados,
        )
        ticket.refresh_from_db()
        OperationsService.registrar_salida(ticket_id=ticket.id, usuario_vigilancia=self.u_vigilancia)

        from apps.operations import services_entrada as _se
        entrada = EntradaMercaderia.objects.get(ticket=ticket)
        _se.enviar_a_sap(entrada, self.u_almacen)
        if creado_sap:
            entrada.refresh_from_db()
            entrada.estado = EntradaMercaderia.ESTADO_CREADO_SAP
            entrada.estado_sap = 'Y'
            entrada.save(update_fields=['estado', 'estado_sap'])
        return ticket

    def test_lectura_facturacion_vs_recepcion(self):
        """
        OC de 10: ronda 1 = 6 (CREADO_SAP), ronda 2 = 4 (solo ENVIADO).
          solo_confirmado=True  → atendida 6, disponible 4 (solo la confirmada).
          solo_confirmado=False → atendida 10, disponible 0 (todo lo físico).
        """
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('6.0000'))
        po_line = self._po_line_de(ticket)
        po = po_line.purchase_order
        self._segunda_ronda(po, Decimal('4.0000'), creado_sap=False)

        confirmada = oc_status.cantidades_consolidadas_por_linea([po], solo_confirmado=True)
        fisica = oc_status.cantidades_consolidadas_por_linea([po], solo_confirmado=False)

        self.assertEqual(confirmada[po_line.id]['ordenada'], Decimal('10.0000'))
        self.assertEqual(confirmada[po_line.id]['atendida'], Decimal('6.0000'))
        self.assertEqual(confirmada[po_line.id]['disponible'], Decimal('4.0000'))

        self.assertEqual(fisica[po_line.id]['atendida'], Decimal('10.0000'))
        self.assertEqual(fisica[po_line.id]['disponible'], Decimal('0.0000'))

    def test_disponible_no_baja_de_cero_en_sobre_recepcion(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('13.0000'))  # OC de 10
        po_line = self._po_line_de(ticket)
        po = po_line.purchase_order

        c = oc_status.cantidades_consolidadas_por_linea([po], solo_confirmado=True)
        self.assertEqual(c[po_line.id]['atendida'], Decimal('13.0000'))
        self.assertEqual(c[po_line.id]['disponible'], Decimal('0'))

    def test_pos_vacio_devuelve_dict_vacio(self):
        self.assertEqual(oc_status.cantidades_consolidadas_por_linea([], solo_confirmado=True), {})

    def test_no_es_n1_conteo_constante_de_queries(self):
        """2 queries constantes (recibido_por_linea + PurchaseOrderLine),
        sin importar cuántas OC — no escala con el número de OC."""
        t1 = self._finalizar_ticket('CDL', cantidad_real=Decimal('6.0000'))
        po1 = self._po_line_de(t1).purchase_order
        t2 = self._finalizar_segundo_ticket(Decimal('5.0000'))
        po2 = t2.appointment.purchase_orders.first()

        with CaptureQueriesContext(connection) as ctx_1:
            oc_status.cantidades_consolidadas_por_linea([po1], solo_confirmado=False)
        with CaptureQueriesContext(connection) as ctx_2:
            oc_status.cantidades_consolidadas_por_linea([po1, po2], solo_confirmado=False)

        self.assertEqual(len(ctx_1), 2)
        self.assertEqual(len(ctx_2), 2)

    # ── Propagación a get_estado_actual_por_oc ────────────────────────────

    def test_get_estado_actual_por_oc_incluye_cantidad_atendida_y_disponible(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('6.0000'))
        po_line = self._po_line_de(ticket)

        lineas = next(iter(OperationsService.get_estado_actual_por_oc(ticket.id).values()))['lineas']
        # Ticket ya FINALIZADO → 1 ronda CREADO_SAP de 6 sobre una OC de 10.
        for l in lineas:
            if l['id'] and l['etapa'] == 'ALMACEN':
                self.assertEqual(l['cantidad_atendida'], Decimal('6.0000'))
                self.assertEqual(l['cantidad_disponible'], Decimal('4.0000'))

    # ── Propagación a las plantillas ─────────────────────────────────────

    def test_copiar_oc_muestra_columna_atendida(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('6.0000'))
        po = ticket.appointment.purchase_orders.first()
        self._perfil_oc()

        self.client.force_login(self.proveedor)
        resp = self.client.get('/invoicing/nueva/copiar/', {'oc_ids': [po.id]})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Atendida')
        self.assertContains(resp, '6.0000')   # atendida de la línea

    def test_factura_detalle_muestra_cantidad_atendida(self):
        ticket = self._finalizar_ticket('CDL', cantidad_real=Decimal('6.0000'))
        po_line = self._po_line_de(ticket)
        po = po_line.purchase_order
        self._perfil_oc()

        factura = self._crear_factura(estado='BORRADOR', proveedor=self._perfil_oc())
        FacturaOrdenCompra.objects.create(factura=factura, purchase_order=po)
        self._crear_factura_linea(factura, po_line, cantidad=Decimal('4.0000'))

        self.client.force_login(self.proveedor)
        resp = self.client.get(f'/invoicing/factura/{factura.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Cant. Atendida (OC)')
        self.assertContains(resp, '6.0000')
