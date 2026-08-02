# apps/operations/tests.py
"""
Pruebas de la Fase 3: el permiso para escribir en TicketLineInspection vía
/operations/api/registrar-inspeccion/ debe corresponder al grupo específico
que le toca actuar según Ticket.etapa_actual + tipo_flujo (no basta con
pertenecer a "algún" grupo interno, que es lo que validaba el antiguo
@staff_interno_required por sí solo).
"""
from django.contrib.auth.models import User, Group
from django.test import TestCase

from apps.appointments.services import AppointmentService
from apps.appointments.models import AppointmentSlot
from apps.operations.models import Ticket, TicketLineInspection
from apps.operations.services import OperationsService
from apps.sap_sync.models import PurchaseOrder, PurchaseOrderLine


class RegistrarInspeccionPermisoPorEtapaTests(TestCase):
    """
    Verifica que ajax_registrar_inspeccion exija el grupo correcto según la
    etapa activa del ticket: un usuario de ALMACEN no puede escribir cuando
    le corresponde a CALIDAD (ticket CON_CALIDAD), y un usuario de CALIDAD no
    puede escribir cuando le corresponde a ALMACEN (ticket SOLO_ALMACEN).
    """

    @classmethod
    def setUpTestData(cls):
        cls.g_proveedores = Group.objects.create(name='PROVEEDORES')
        cls.g_compras     = Group.objects.create(name='COMPRAS')
        cls.g_almacen     = Group.objects.create(name='ALMACEN')
        cls.g_vigilancia  = Group.objects.create(name='VIGILANCIA')
        cls.g_calidad     = Group.objects.create(name='CALIDAD')

        cls.proveedor = User.objects.create_user('proveedor_test', password='x')
        cls.proveedor.groups.add(cls.g_proveedores)

        cls.u_compras = User.objects.create_user('compras_test', password='x')
        cls.u_compras.groups.add(cls.g_compras)

        cls.u_almacen = User.objects.create_user('almacen_test', password='x')
        cls.u_almacen.groups.add(cls.g_almacen)

        cls.u_vigilancia = User.objects.create_user('vigilancia_test', password='x')
        cls.u_vigilancia.groups.add(cls.g_vigilancia)

        cls.u_calidad = User.objects.create_user('calidad_test', password='x')
        cls.u_calidad.groups.add(cls.g_calidad)

    def _crear_ticket_en_etapa_almacen(self, u_mss_tdb: str, doc_num: int) -> Ticket:
        """
        Crea una cita completa (OC -> solicitud -> confirmación -> COA si aplica
        -> ingreso -> recepción de Almacén) y deja el Ticket justo en
        etapa_actual=ALMACEN, listo para el siguiente paso (Calidad o cierre
        de Almacén, según u_mss_tdb).
        """
        po = PurchaseOrder.objects.create(
            doc_entry=doc_num, doc_num=doc_num, card_code='TESTCODE', card_name='TEST SAC',
            e_mail='test@test.com', status='PENDIENTE', u_mss_tdb=u_mss_tdb,
        )
        PurchaseOrderLine.objects.create(
            purchase_order=po, line_num=1, item_code='ITEM-TEST', description='Item de prueba',
            quantity_sap=10, und_medida='KG', requiere_coa=(u_mss_tdb == 'MP'),
        )

        slot = AppointmentSlot.objects.create(
            date='2026-09-01', start_time='08:00', dock='TEST', max_capacity=5
        )
        appointment = AppointmentService.solicitar_cita_borrador(
            user=self.proveedor, slot_id=slot.id, oc_ids=[po.id]
        )
        ticket = AppointmentService.confirmar_cita(
            appointment_id=appointment.id, usuario_almacen=self.u_compras
        )

        if u_mss_tdb == 'MP':
            for line in po.lines.filter(requiere_coa=True):
                OperationsService.registrar_coa_proveedor(
                    ticket_id=ticket.id, po_line_id=line.id,
                    coa_url='https://onedrive.example.com/fake-coa.pdf', usuario=self.proveedor,
                )

        ticket = OperationsService.iniciar_ingreso_planta(
            ticket_id=ticket.id, usuario_vigilancia=self.u_vigilancia
        )
        OperationsService.autorizar_almacen(ticket_id=ticket.id, usuario_almacen=self.u_almacen)
        ticket.refresh_from_db()
        return ticket

    @staticmethod
    def _resultados_para(ticket: Ticket) -> list[dict]:
        return [
            {
                'inspeccion_id': insp.id,
                'estado': 'CONFORME',
                'cantidad_modificada': str(insp.cantidad_sap),
            }
            for insp in TicketLineInspection.objects.filter(ticket=ticket, etapa='ALMACEN')
        ]

    def _post_registrar_inspeccion(self, ticket: Ticket):
        return self.client.post(
            '/operations/api/registrar-inspeccion/',
            data={'ticket_id': ticket.id, 'resultados': self._resultados_para(ticket)},
            content_type='application/json',
        )

    # ── Caso 1: ALMACEN no puede escribir cuando le toca a CALIDAD ──────────

    def test_almacen_no_puede_registrar_inspeccion_que_le_corresponde_a_calidad(self):
        ticket = self._crear_ticket_en_etapa_almacen('MP', 555001)
        self.assertEqual(ticket.tipo_flujo, 'CON_CALIDAD')
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_ALMACEN)
        self.assertEqual(
            OperationsService.grupo_requerido_por_etapa(ticket), 'CALIDAD'
        )

        self.client.force_login(self.u_almacen)
        resp = self._post_registrar_inspeccion(ticket)

        self.assertEqual(resp.status_code, 403)
        ticket.refresh_from_db()
        self.assertEqual(
            ticket.etapa_actual, Ticket.ETAPA_ALMACEN,
            "El ticket no debía avanzar: el usuario de ALMACEN no tenía permiso "
            "para registrar la inspección de Calidad."
        )

    def test_calidad_si_puede_registrar_su_propia_inspeccion(self):
        """Camino positivo, mismo escenario: CALIDAD sí puede."""
        ticket = self._crear_ticket_en_etapa_almacen('MP', 555002)

        self.client.force_login(self.u_calidad)
        resp = self._post_registrar_inspeccion(ticket)

        self.assertEqual(resp.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_CALIDAD)

    # ── Caso 2: CALIDAD no puede escribir cuando le toca a ALMACEN ───────────

    def test_calidad_no_puede_registrar_cierre_que_le_corresponde_a_almacen(self):
        ticket = self._crear_ticket_en_etapa_almacen('CDL', 555003)
        self.assertEqual(ticket.tipo_flujo, 'SOLO_ALMACEN')
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_ALMACEN)
        self.assertEqual(
            OperationsService.grupo_requerido_por_etapa(ticket), 'ALMACEN'
        )

        self.client.force_login(self.u_calidad)
        resp = self._post_registrar_inspeccion(ticket)

        self.assertEqual(resp.status_code, 403)
        ticket.refresh_from_db()
        self.assertEqual(
            ticket.etapa_actual, Ticket.ETAPA_ALMACEN,
            "El ticket no debía avanzar: el usuario de CALIDAD no tenía permiso "
            "para registrar el cierre de Almacén."
        )

    def test_almacen_si_puede_registrar_su_propio_cierre(self):
        """Camino positivo, mismo escenario: ALMACEN sí puede cerrar su propio flujo."""
        ticket = self._crear_ticket_en_etapa_almacen('CDL', 555004)

        self.client.force_login(self.u_almacen)
        resp = self._post_registrar_inspeccion(ticket)

        self.assertEqual(resp.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_VIGILANCIA_SALIDA)

    # ── Superusuario siempre puede, sin importar el grupo ────────────────────

    def test_superusuario_siempre_puede(self):
        ticket = self._crear_ticket_en_etapa_almacen('MP', 555005)
        admin = User.objects.create_superuser('admin_test', 'admin@test.com', 'x')

        self.client.force_login(admin)
        resp = self._post_registrar_inspeccion(ticket)

        self.assertEqual(resp.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_CALIDAD)
