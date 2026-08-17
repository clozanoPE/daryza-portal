# apps/sap_sync/tests.py
"""
Suite de pruebas de apps.sap_sync — sesión 49.

Cubre el bug de robustez del sync de OC (PurchaseOrderSerializer.create):
antes, cada resincronización hacía `purchase_order.lines.all().delete()` +
`bulk_create()`, sin importar si esas líneas ya tenían
TicketLineInspection/TicketLineCOA apuntándolas con on_delete=PROTECT — un
resync de cualquier OC con un Ticket ya en curso lanzaba ProtectedError.

El fix reescribe el sync de líneas como upsert por (purchase_order,
line_num) — line_num es el LineNum de SAP, estable dentro del documento —
y desactiva (PurchaseOrderLine.activa=False) en vez de borrar las líneas
que ya no vienen en el payload.

De paso se encontró y corrigió un segundo bug, independiente pero
bloqueante para poder ejercitar el primero vía el endpoint HTTP real:
doc_entry/doc_num son unique=True, así que DRF les agregaba un
UniqueValidator automático que rechazaba con 400 CUALQUIER resync de una
OC ya existente, antes de que create() llegara a ejecutarse — la
"idempotencia" documentada en el serializer nunca funcionó vía POST real.
Se neutraliza vía Meta.extra_kwargs (la unicidad real la sigue
garantizando la constraint de BD + update_or_create).

Se prueba contra el endpoint HTTP real (APIClient + Token de DRF), no
llamando al serializer directo, para que la suite ejercite exactamente lo
que golpea el daemon VB.NET en producción.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.operations.models import Ticket, TicketLineInspection, TicketLineCOA
from apps.sap_sync.models import PurchaseOrder, PurchaseOrderLine


class SyncOCUpsertLineaPorLineaTests(TestCase):
    """Regresión: resync de una OC con Ticket/inspecciones ya registradas."""

    @classmethod
    def setUpTestData(cls):
        cls.daemon_user = User.objects.create_user(username='daemon_test', password='x')
        cls.token = Token.objects.create(user=cls.daemon_user)
        cls.url = reverse('sync-oc-list')

        cls.po = PurchaseOrder.objects.create(
            doc_entry=90001, doc_num=190001,
            card_code='P900001', card_name='PROVEEDOR TEST SYNC',
            e_mail='x@x.com', status='O', u_mss_tdb='MP',
        )
        cls.linea_a = PurchaseOrderLine.objects.create(
            purchase_order=cls.po, line_num=0, item_code='ITEM-A',
            description='Linea A', quantity_sap=Decimal('100.0000'),
            und_medida='KG', requiere_coa=True,
        )
        cls.linea_b = PurchaseOrderLine.objects.create(
            purchase_order=cls.po, line_num=1, item_code='ITEM-B',
            description='Linea B', quantity_sap=Decimal('50.0000'),
            und_medida='KG', requiere_coa=True,
        )

    def setUp(self):
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

    def _payload_base(self, lines):
        return {
            'doc_entry': self.po.doc_entry,
            'doc_num': self.po.doc_num,
            'card_code': self.po.card_code,
            'card_name': self.po.card_name,
            'e_mail': self.po.e_mail,
            'status': self.po.status,
            'u_mss_tdb': self.po.u_mss_tdb,
            'lines': lines,
        }

    def test_resync_simple_no_falla_por_unique_validator(self):
        """El endpoint real debe aceptar re-enviar una OC ya existente."""
        payload = self._payload_base([
            {'line_num': 0, 'item_code': 'ITEM-A', 'description': 'Linea A',
             'quantity_sap': '100.0000', 'und_medida': 'KG'},
            {'line_num': 1, 'item_code': 'ITEM-B', 'description': 'Linea B',
             'quantity_sap': '50.0000', 'und_medida': 'KG'},
        ])
        response = self.client.post(self.url, payload, format='json')
        self.assertEqual(response.status_code, 201, response.data)

    def test_resync_de_oc_con_ticket_e_inspecciones_no_rompe_fk(self):
        """
        Caso exacto reportado: la OC ya tiene un Ticket con
        TicketLineInspection/TicketLineCOA apuntando a sus líneas
        (on_delete=PROTECT). Un resync con cantidad cambiada en una línea,
        vía el endpoint HTTP real, no debe lanzar ProtectedError/500, y
        debe preservar el id de PurchaseOrderLine (y por lo tanto esas FK).
        """
        # Construye un Ticket real con inspecciones sobre linea_a/linea_b,
        # sin pasar por todo el flujo de negocio (no es el objeto de este
        # test) — inserta directo las filas de trazabilidad protegidas.
        from apps.appointments.models import Appointment, AppointmentSlot
        from apps.base.models import Sede
        import datetime

        sede = Sede.objects.filter(codigo='LURIN').first() or Sede.objects.create(
            codigo='LURIN', nombre='Planta Lurin'
        )
        proveedor = User.objects.create_user(username='P900001', password='x')
        slot = AppointmentSlot.objects.create(
            sede=sede, date=datetime.date(2026, 12, 1),
            start_time=datetime.time(8, 0), end_time=datetime.time(8, 45),
            dock='', max_capacity=2,
        )
        appointment = Appointment.objects.create(
            user=proveedor, slot=slot, status='FINALIZADA', sede=sede,
        )
        appointment.purchase_orders.set([self.po])
        ticket = Ticket.objects.create(appointment=appointment, estado='FINALIZADO')

        TicketLineInspection.objects.create(
            ticket=ticket, po_line=self.linea_a, etapa='ALMACEN',
            doc_num=self.po.doc_num, usuario=self.daemon_user,
            cantidad_sap=self.linea_a.quantity_sap,
            cantidad_modificada=Decimal('100.0000'),
            estado='CONFORME',
        )
        TicketLineCOA.objects.create(
            ticket=ticket, po_line=self.linea_b, coa_url='https://sharepoint.com/x',
            subido_por=proveedor,
        )

        id_linea_a_antes = self.linea_a.id
        id_linea_b_antes = self.linea_b.id

        # Payload de SAP: cambia cantidad de linea_a, OMITE linea_b por
        # completo (simula que SAP la canceló/eliminó del documento).
        payload = self._payload_base([
            {'line_num': 0, 'item_code': 'ITEM-A', 'description': 'Linea A actualizada',
             'quantity_sap': '123.4500', 'und_medida': 'KG'},
        ])

        response = self.client.post(self.url, payload, format='json')

        self.assertEqual(response.status_code, 201, response.data)

        # La linea_a se actualizo IN-SITU (mismo id), sin tocar requiere_coa.
        self.linea_a.refresh_from_db()
        self.assertEqual(self.linea_a.id, id_linea_a_antes)
        self.assertEqual(self.linea_a.quantity_sap, Decimal('123.4500'))
        self.assertTrue(self.linea_a.requiere_coa)
        self.assertTrue(self.linea_a.activa)

        # La linea_b NO se borro (habria roto la FK protegida): sigue
        # existiendo con el mismo id, solo se desactivo.
        self.linea_b.refresh_from_db()
        self.assertEqual(self.linea_b.id, id_linea_b_antes)
        self.assertFalse(self.linea_b.activa)

        # Las FK protegidas siguen apuntando a los mismos objetos, validas.
        insp = TicketLineInspection.objects.get(ticket=ticket, etapa='ALMACEN')
        self.assertEqual(insp.po_line_id, id_linea_a_antes)
        coa = TicketLineCOA.objects.get(ticket=ticket)
        self.assertEqual(coa.po_line_id, id_linea_b_antes)

    def test_codigo_viejo_delete_masivo_si_hubiera_lanzado_protected_error(self):
        """
        Ancla de regresión: documenta que el patron viejo (delete() masivo
        sobre queryset con FK protegidas) SI rompe — para que quede claro
        por que el upsert linea por linea (probado arriba) es necesario,
        no una preferencia de estilo.
        """
        from apps.appointments.models import Appointment, AppointmentSlot
        from apps.base.models import Sede
        import datetime

        sede = Sede.objects.filter(codigo='LURIN').first() or Sede.objects.create(
            codigo='LURIN', nombre='Planta Lurin'
        )
        proveedor = User.objects.create_user(username='P900002', password='x')
        slot = AppointmentSlot.objects.create(
            sede=sede, date=datetime.date(2026, 12, 2),
            start_time=datetime.time(8, 0), end_time=datetime.time(8, 45),
            dock='', max_capacity=2,
        )
        appointment = Appointment.objects.create(
            user=proveedor, slot=slot, status='FINALIZADA', sede=sede,
        )
        appointment.purchase_orders.set([self.po])
        ticket = Ticket.objects.create(appointment=appointment, estado='FINALIZADO')
        TicketLineInspection.objects.create(
            ticket=ticket, po_line=self.linea_a, etapa='ALMACEN',
            doc_num=self.po.doc_num, usuario=self.daemon_user,
            cantidad_sap=self.linea_a.quantity_sap,
            cantidad_modificada=Decimal('100.0000'),
            estado='CONFORME',
        )

        with self.assertRaises(ProtectedError):
            self.po.lines.all().delete()
