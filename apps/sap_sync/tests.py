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

SyncOCCreaSupplierProfileTests (sesión posterior, SupplierProfile mínimo):
confirma que el mismo POST real a /sync-oc/ crea/actualiza un
SupplierProfile por card_code, sin necesitar ningún endpoint aparte —
ver apps/base/supplier_sync.py.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.base.models import SupplierProfile
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
        # Sesión 99b: gestionado_por_lote es obligatorio en el serializer
        # (required=True). Los tests que no lo especifican reciben False
        # por defecto acá para no tener que repetirlo en cada línea inline.
        for l in lines:
            l.setdefault('gestionado_por_lote', False)
        return {
            'doc_entry': self.po.doc_entry,
            'doc_num': self.po.doc_num,
            'card_code': self.po.card_code,
            'card_name': self.po.card_name,
            'e_mail': self.po.e_mail,
            'status': self.po.status,
            'u_mss_tdb': self.po.u_mss_tdb,
            # Sesión 93: doc_cur (DocCur de SAP) ahora obligatorio en el
            # payload, mismo criterio que precio_unitario/precio_total_
            # linea/tax_code (sesión 92).
            'doc_cur': 'PEN',
            'lines': lines,
        }

    def test_resync_simple_no_falla_por_unique_validator(self):
        """El endpoint real debe aceptar re-enviar una OC ya existente."""
        payload = self._payload_base([
            {'line_num': 0, 'item_code': 'ITEM-A', 'description': 'Linea A',
             'quantity_sap': '100.0000', 'und_medida': 'KG',
             'precio_unitario': '10.0000', 'precio_total_linea': '1000.0000', 'tax_code': 'IGV'},
            {'line_num': 1, 'item_code': 'ITEM-B', 'description': 'Linea B',
             'quantity_sap': '50.0000', 'und_medida': 'KG',
             'precio_unitario': '20.0000', 'precio_total_linea': '1000.0000', 'tax_code': 'IGV'},
        ])
        response = self.client.post(self.url, payload, format='json')
        self.assertEqual(response.status_code, 201, response.data)

    def test_precio_igv_sincronizan_correctamente(self):
        """
        Sesión 92: precio_unitario/precio_total_linea/tax_code llegan
        reales de SAP (PriceBefDi/LineTotal/TaxCode) por cada línea, no
        calculados por el demonio. Confirma persistencia correcta y que
        un resync los refresca (no solo el alta inicial).
        """
        payload = self._payload_base([
            {'line_num': 0, 'item_code': 'ITEM-A', 'description': 'Linea A',
             'quantity_sap': '100.0000', 'und_medida': 'KG',
             'precio_unitario': '15.5000', 'precio_total_linea': '1550.0000', 'tax_code': 'IGV'},
            {'line_num': 1, 'item_code': 'ITEM-B', 'description': 'Linea B',
             'quantity_sap': '50.0000', 'und_medida': 'KG',
             'precio_unitario': '8.2500', 'precio_total_linea': '412.5000', 'tax_code': 'IGV_EXE'},
        ])
        response = self.client.post(self.url, payload, format='json')
        self.assertEqual(response.status_code, 201, response.data)

        self.linea_a.refresh_from_db()
        self.linea_b.refresh_from_db()
        self.assertEqual(self.linea_a.precio_unitario, Decimal('15.5000'))
        self.assertEqual(self.linea_a.precio_total_linea, Decimal('1550.0000'))
        self.assertEqual(self.linea_a.tax_code, 'IGV')
        self.assertEqual(self.linea_b.precio_unitario, Decimal('8.2500'))
        self.assertEqual(self.linea_b.tax_code, 'IGV_EXE')

        # Un resync con precio distinto REFRESCA el valor (no es un
        # snapshot inmutable a este nivel — el snapshot inmutable vive
        # en FacturaLinea.precio_oc/tax_code, no acá).
        payload_2 = self._payload_base([
            {'line_num': 0, 'item_code': 'ITEM-A', 'description': 'Linea A',
             'quantity_sap': '100.0000', 'und_medida': 'KG',
             'precio_unitario': '16.0000', 'precio_total_linea': '1600.0000', 'tax_code': 'IGV'},
        ])
        response_2 = self.client.post(self.url, payload_2, format='json')
        self.assertEqual(response_2.status_code, 201, response_2.data)
        self.linea_a.refresh_from_db()
        self.assertEqual(self.linea_a.precio_unitario, Decimal('16.0000'))

    def test_payload_sin_los_3_campos_nuevos_rechaza_con_400(self):
        """
        precio_unitario/precio_total_linea/tax_code son obligatorios en
        el serializer (sin required=False) — un daemon desactualizado
        que no los envíe recibe un 400 claro, no un 201 con datos en su
        default transitorio.
        """
        payload = self._payload_base([
            {'line_num': 0, 'item_code': 'ITEM-A', 'description': 'Linea A',
             'quantity_sap': '100.0000', 'und_medida': 'KG'},
        ])
        response = self.client.post(self.url, payload, format='json')
        self.assertEqual(response.status_code, 400)

    def test_gestionado_por_lote_se_sincroniza_y_es_obligatorio(self):
        """
        Sesión 99b: gestionado_por_lote (OITM.ManBtchNum) llega por línea
        y se persiste tal cual. Omitirlo -> 400 (required=True, mismo
        criterio que precio_unitario/tax_code).
        """
        payload = self._payload_base([
            {'line_num': 0, 'item_code': 'ITEM-A', 'description': 'Linea A',
             'quantity_sap': '100.0000', 'und_medida': 'KG',
             'precio_unitario': '10.0000', 'precio_total_linea': '1000.0000', 'tax_code': 'IGV',
             'gestionado_por_lote': True},
            {'line_num': 1, 'item_code': 'ITEM-B', 'description': 'Linea B',
             'quantity_sap': '50.0000', 'und_medida': 'KG',
             'precio_unitario': '20.0000', 'precio_total_linea': '1000.0000', 'tax_code': 'IGV',
             'gestionado_por_lote': False},
        ])
        response = self.client.post(self.url, payload, format='json')
        self.assertEqual(response.status_code, 201, response.data)

        self.linea_a.refresh_from_db()
        self.linea_b.refresh_from_db()
        self.assertTrue(self.linea_a.gestionado_por_lote)
        self.assertFalse(self.linea_b.gestionado_por_lote)

        # Omitir el campo en una línea -> 400.
        payload_incompleto = self._payload_base([
            {'line_num': 0, 'item_code': 'ITEM-A', 'description': 'Linea A',
             'quantity_sap': '100.0000', 'und_medida': 'KG',
             'precio_unitario': '10.0000', 'precio_total_linea': '1000.0000', 'tax_code': 'IGV'},
        ])
        # _payload_base ya inyectó gestionado_por_lote=False por el setdefault;
        # lo quitamos para simular un daemon desactualizado.
        del payload_incompleto['lines'][0]['gestionado_por_lote']
        response_2 = self.client.post(self.url, payload_incompleto, format='json')
        self.assertEqual(response_2.status_code, 400)

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
             'quantity_sap': '123.4500', 'und_medida': 'KG',
             'precio_unitario': '11.0000', 'precio_total_linea': '1358.0000', 'tax_code': 'IGV'},
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


class SyncOCCreaSupplierProfileTests(TestCase):
    """
    El daemon nunca llama a ningún endpoint separado para proveedores —
    el mismo POST a /sync-oc/ (que ya trae card_code/card_name/e_mail)
    debe upsertear el SupplierProfile correspondiente.
    """

    @classmethod
    def setUpTestData(cls):
        cls.daemon_user = User.objects.create_user(username='daemon_test_supplier', password='x')
        cls.token = Token.objects.create(user=cls.daemon_user)
        cls.url = reverse('sync-oc-list')

    def setUp(self):
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

    def _payload(self, doc_entry, doc_num, card_code, card_name, e_mail):
        return {
            'doc_entry': doc_entry, 'doc_num': doc_num,
            'card_code': card_code, 'card_name': card_name, 'e_mail': e_mail,
            'status': 'O', 'u_mss_tdb': 'CDL',
            # Sesión 93: doc_cur obligatorio, mismo criterio que la línea 2 arriba.
            'doc_cur': 'PEN',
            'lines': [
                {'line_num': 0, 'item_code': 'ITEM-SP', 'description': 'Item',
                 'quantity_sap': '10.0000', 'und_medida': 'UND',
                 'precio_unitario': '5.0000', 'precio_total_linea': '50.0000', 'tax_code': 'IGV',
                 'gestionado_por_lote': False},
            ],
        }

    def test_primer_sync_de_un_card_code_nuevo_crea_supplier_profile(self):
        payload = self._payload(95001, 195001, 'P900010', 'PROVEEDOR SYNC TEST SAC', 'sync@test.com')
        response = self.client.post(self.url, payload, format='json')
        self.assertEqual(response.status_code, 201, response.data)

        perfil = SupplierProfile.objects.get(sap_card_code='P900010')
        self.assertEqual(perfil.razon_social, 'PROVEEDOR SYNC TEST SAC')
        self.assertEqual(perfil.correo_electronico, 'sync@test.com')
        self.assertEqual(perfil.ruc, 'P900010')
        self.assertEqual(perfil.estado, SupplierProfile.ESTADO_ACTIVO)
        self.assertIsNone(perfil.user)

    def test_resync_refresca_razon_social_y_correo_sin_tocar_estado_ni_user(self):
        proveedor_user = User.objects.create_user(username='P900011', password='x')
        perfil = SupplierProfile.objects.create(
            sap_card_code='P900011', ruc='P900011', razon_social='NOMBRE VIEJO',
            correo_electronico='viejo@test.com', estado=SupplierProfile.ESTADO_SUSPENDIDO,
            user=proveedor_user,
        )

        payload = self._payload(95002, 195002, 'P900011', 'NOMBRE ACTUALIZADO SAC', 'nuevo@test.com')
        response = self.client.post(self.url, payload, format='json')
        self.assertEqual(response.status_code, 201, response.data)

        perfil.refresh_from_db()
        # Los datos maestros de SAP se refrescan...
        self.assertEqual(perfil.razon_social, 'NOMBRE ACTUALIZADO SAC')
        self.assertEqual(perfil.correo_electronico, 'nuevo@test.com')
        # ...pero estado/user (decisiones que un humano tomó) NUNCA se pisan
        # por un resync automático de OC.
        self.assertEqual(perfil.estado, SupplierProfile.ESTADO_SUSPENDIDO)
        self.assertEqual(perfil.user_id, proveedor_user.id)

    def test_dos_ocs_del_mismo_card_code_no_duplican_el_perfil(self):
        payload_a = self._payload(95003, 195003, 'P900012', 'PROVEEDOR DOS OC', 'dosoc@test.com')
        payload_b = self._payload(95004, 195004, 'P900012', 'PROVEEDOR DOS OC', 'dosoc@test.com')
        self.client.post(self.url, payload_a, format='json')
        self.client.post(self.url, payload_b, format='json')

        self.assertEqual(SupplierProfile.objects.filter(sap_card_code='P900012').count(), 1)
