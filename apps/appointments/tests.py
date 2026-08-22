# apps/appointments/tests.py
"""
apps.appointments — primera suite de esta app (no existía ninguna
todavía). Cubre específicamente la notificación real por correo al
proveedor tras confirmar su cita (AppointmentService.confirmar_cita /
_notificar_proveedor_qr, motor de notificaciones real vía Microsoft
Graph, apps/base/services_correo.py) — cierra la deuda histórica del
`print()` que llevaba varias sesiones sin activar.

Mockeado aquí (apps.base.services_correo.enviar_correo parcheado en su
propio módulo, no en apps.appointments.services — _notificar_proveedor_qr
hace `from apps.base.services_correo import enviar_correo` DENTRO de la
función, así que el nombre nunca queda vivo como atributo de
apps.appointments.services; hay que parchear la fuente real) — la prueba
end-to-end contra Graph real (correo real recibido en clozano@daryza.com)
ya se hizo manualmente antes de escribir este código, documentada en
CLAUDE.md; esta suite corre sin depender de que Graph esté disponible
cada vez que se ejecuta `manage.py test`.

Reutiliza OperationsTestBase (apps.operations.tests) para construir una
cita real de principio a fin — mismo patrón ya establecido en
apps.invoicing.tests.
"""
from unittest.mock import patch

from apps.base.services_correo import ResultadoEnvioCorreo
from apps.operations.tests import OperationsTestBase


class NotificarProveedorQRTests(OperationsTestBase):

    def _con_email(self):
        self.proveedor.email = 'proveedor_test@daryza-test.com'
        self.proveedor.save(update_fields=['email'])

    @patch('apps.base.services_correo.enviar_correo')
    def test_envio_exitoso_no_deja_error_visible(self, mock_enviar):
        mock_enviar.return_value = ResultadoEnvioCorreo(enviado=True)
        self._con_email()

        ticket = self._crear_cita_confirmada('CDL', requiere_coa=False)

        mock_enviar.assert_called_once()
        self.assertIsNone(getattr(ticket, 'email_notificacion_error', None))

    @patch('apps.base.services_correo.enviar_correo')
    def test_fallo_de_envio_no_revierte_la_confirmacion(self, mock_enviar):
        mock_enviar.return_value = ResultadoEnvioCorreo(
            enviado=False, error='Graph respondió 403: Forbidden',
        )
        self._con_email()

        ticket = self._crear_cita_confirmada('CDL', requiere_coa=False)

        # La cita/ticket se confirmaron igual — un fallo de correo no
        # rompe el flujo principal (pedido explícito).
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, 'PROGRAMADO')
        self.assertEqual(ticket.appointment.status, 'CONFIRMADA')

    @patch('apps.base.services_correo.enviar_correo')
    def test_fallo_de_envio_queda_visible_en_el_ticket_devuelto(self, mock_enviar):
        mock_enviar.return_value = ResultadoEnvioCorreo(
            enviado=False, error='Graph respondió 403: Forbidden',
        )
        self._con_email()

        ticket = self._crear_cita_confirmada('CDL', requiere_coa=False)

        self.assertEqual(ticket.email_notificacion_error, 'Graph respondió 403: Forbidden')

    @patch('apps.base.services_correo.enviar_correo')
    def test_proveedor_sin_email_no_intenta_enviar(self, mock_enviar):
        # Sin _con_email(): OperationsTestBase._crear_usuario no fija
        # ningún email — este es el estado por defecto, no un caso forzado.
        ticket = self._crear_cita_confirmada('CDL', requiere_coa=False)

        mock_enviar.assert_not_called()
        self.assertIsNone(getattr(ticket, 'email_notificacion_error', None))

    @patch('apps.base.services_correo.enviar_correo')
    def test_endpoint_ajax_compras_expone_email_error_en_la_respuesta(self, mock_enviar):
        """
        El error de correo debe quedar visible en la respuesta JSON del
        endpoint (pedido explícito: "no lo pierdas en silencio del
        todo") — no solo en el atributo transitorio del ticket.
        """
        mock_enviar.return_value = ResultadoEnvioCorreo(
            enviado=False, error='Graph respondió 403: Forbidden',
        )
        self._con_email()

        from apps.appointments.services import AppointmentService
        from apps.sap_sync.models import PurchaseOrder
        from apps.base.models import Sede
        from apps.appointments.models import AppointmentSlot

        po = PurchaseOrder.objects.create(
            doc_entry=777001, doc_num=777001, card_code='TESTAJAX', card_name='TEST AJAX SAC',
            e_mail='ajax@test.com', status='PENDIENTE', u_mss_tdb='CDL',
        )
        from apps.sap_sync.models import PurchaseOrderLine
        PurchaseOrderLine.objects.create(
            purchase_order=po, line_num=1, item_code='ITEM-AJAX', description='Item',
            quantity_sap=5, und_medida='UND',
        )
        slot = AppointmentSlot.objects.create(
            sede=Sede.objects.get(codigo='LURIN'),
            date='2026-09-20', start_time='08:00', dock='TEST', max_capacity=5,
        )
        appointment = AppointmentService.solicitar_cita_borrador(
            user=self.proveedor, slot_id=slot.id, oc_ids=[po.id],
        )

        self.client.force_login(self.u_compras)
        resp = self.client.post(
            '/operations/api/compras/confirmar-cita/',
            data={'appointment_id': appointment.id},
            content_type='application/json',
        )

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload['status'], 'success')  # el fallo de correo no rompe el flujo
        self.assertIn('email_error', payload)
        self.assertEqual(payload['email_error'], 'Graph respondió 403: Forbidden')
