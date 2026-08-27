# apps/base/tests.py
"""
apps.base — motor de notificaciones real (Microsoft Graph):
graph_auth.obtener_token_graph (Client Credentials Flow, compartida con
OneDriveClient) + services_correo.enviar_correo (POST /users/{buzón}/
sendMail).

Mockeado (requests.post/get parcheado) — la prueba end-to-end contra
Graph real ya se hizo manualmente antes de escribir este código (correo
real recibido en clozano@daryza.com, ver CLAUDE.md); esta suite cubre
los caminos de error sin depender de que Graph esté disponible cada vez
que corre `manage.py test`.
"""
from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.db import IntegrityError
from django.test import SimpleTestCase, TestCase, override_settings

from apps.base import graph_auth, services_correo
from apps.base.models import SupplierProfile
from apps.base.supplier_onboarding import (
    ESTADO_ACTUALIZADO_SIN_ALTA,
    ESTADO_CREADO,
    onboard_proveedor,
)


class ObtenerTokenGraphTests(SimpleTestCase):

    @patch('apps.base.graph_auth.requests.post')
    def test_token_exitoso(self, mock_post):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {'access_token': 'tok123'}
        mock_post.return_value = mock_resp

        token = graph_auth.obtener_token_graph()

        self.assertEqual(token, 'tok123')

    @patch('apps.base.graph_auth.requests.post')
    def test_credenciales_invalidas_lanza(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = requests.HTTPError('401 Unauthorized')
        mock_post.return_value = mock_resp

        with self.assertRaises(requests.HTTPError):
            graph_auth.obtener_token_graph()


@override_settings(GRAPH_SENDER_EMAIL='remitente@daryza.com')
class EnviarCorreoTests(SimpleTestCase):

    def test_sin_destinatario_no_intenta_obtener_token(self):
        with patch('apps.base.services_correo.obtener_token_graph') as mock_token:
            resultado = services_correo.enviar_correo('', 'Asunto', '<p>x</p>')

        self.assertFalse(resultado.enviado)
        self.assertIn('destinatario', resultado.error.lower())
        mock_token.assert_not_called()

    @override_settings(GRAPH_SENDER_EMAIL='')
    def test_sin_sender_configurado_no_intenta_obtener_token(self):
        with patch('apps.base.services_correo.obtener_token_graph') as mock_token:
            resultado = services_correo.enviar_correo('x@x.com', 'Asunto', '<p>x</p>')

        self.assertFalse(resultado.enviado)
        self.assertIn('GRAPH_SENDER_EMAIL', resultado.error)
        mock_token.assert_not_called()

    @patch('apps.base.services_correo.obtener_token_graph')
    def test_error_al_obtener_token_no_lanza(self, mock_token):
        mock_token.side_effect = requests.HTTPError('401 Unauthorized')

        resultado = services_correo.enviar_correo('x@x.com', 'Asunto', '<p>x</p>')

        self.assertFalse(resultado.enviado)
        self.assertIn('token', resultado.error.lower())

    @patch('apps.base.services_correo.requests.post')
    @patch('apps.base.services_correo.obtener_token_graph')
    def test_graph_202_es_exito_y_usa_el_endpoint_users_sendmail(self, mock_token, mock_post):
        mock_token.return_value = 'tok123'
        mock_post.return_value = MagicMock(status_code=202)

        resultado = services_correo.enviar_correo('x@x.com', 'Asunto', '<p>x</p>')

        self.assertTrue(resultado.enviado)
        self.assertIsNone(resultado.error)

        url_llamada = mock_post.call_args[0][0]
        self.assertEqual(
            url_llamada,
            'https://graph.microsoft.com/v1.0/users/remitente@daryza.com/sendMail',
        )

    @patch('apps.base.services_correo.requests.post')
    @patch('apps.base.services_correo.obtener_token_graph')
    def test_graph_rechaza_el_envio_no_lanza(self, mock_token, mock_post):
        mock_token.return_value = 'tok123'
        mock_post.return_value = MagicMock(status_code=403, text='Forbidden: buzón mal configurado')

        resultado = services_correo.enviar_correo('x@x.com', 'Asunto', '<p>x</p>')

        self.assertFalse(resultado.enviado)
        self.assertIn('403', resultado.error)

    @patch('apps.base.services_correo.requests.post')
    @patch('apps.base.services_correo.obtener_token_graph')
    def test_error_de_red_al_enviar_no_lanza(self, mock_token, mock_post):
        mock_token.return_value = 'tok123'
        mock_post.side_effect = requests.ConnectionError('timeout')

        resultado = services_correo.enviar_correo('x@x.com', 'Asunto', '<p>x</p>')

        self.assertFalse(resultado.enviado)
        self.assertIn('red', resultado.error.lower())


class SupplierProfileCamposNuevosTests(TestCase):
    """
    Sub-etapa 2.1 (Proveedores, sesión 86): campos nuevos sobre
    SupplierProfile — sin ningún consumidor todavía (lo llenan/leen las
    sub-etapas siguientes), solo se verifica que el modelo/migración
    quedaron bien: defaults seguros, sin romper la creación de un perfil
    ya existente sin especificarlos.
    """

    def test_defaults_no_afectan_a_un_perfil_creado_sin_especificarlos(self):
        perfil = SupplierProfile.objects.create(sap_card_code='P00000000001')

        self.assertEqual(perfil.razon_social_legal, '')
        self.assertFalse(perfil.debe_cambiar_password)

    def test_se_pueden_asignar_ambos_campos_nuevos(self):
        perfil = SupplierProfile.objects.create(
            sap_card_code='P00000000002',
            razon_social_legal='PROVEEDOR EJEMPLO SOCIEDAD ANONIMA CERRADA',
            debe_cambiar_password=True,
        )
        perfil.refresh_from_db()

        self.assertEqual(perfil.razon_social_legal, 'PROVEEDOR EJEMPLO SOCIEDAD ANONIMA CERRADA')
        self.assertTrue(perfil.debe_cambiar_password)


class CrearGruposInicialesTests(TestCase):
    """
    Sub-etapa 2.1: el comando ahora también garantiza PROVEEDORES (antes
    solo cubría los 5 grupos operativos internos) — necesario para que el
    alta automática de proveedores (Etapa 2.2/2.3) no dependa de que el
    grupo ya exista por haberse creado a mano en algún momento.
    """

    GRUPOS_ESPERADOS = {'COMPRAS', 'ALMACEN', 'VIGILANCIA', 'CALIDAD', 'MATERIA_PRIMA', 'PROVEEDORES'}

    def test_crea_los_6_grupos_en_una_bd_vacia(self):
        self.assertEqual(Group.objects.count(), 0)

        call_command('crear_grupos_iniciales')

        nombres = set(Group.objects.values_list('name', flat=True))
        self.assertEqual(nombres, self.GRUPOS_ESPERADOS)

    def test_correrlo_dos_veces_no_duplica_ni_falla(self):
        call_command('crear_grupos_iniciales')
        call_command('crear_grupos_iniciales')

        for nombre in self.GRUPOS_ESPERADOS:
            self.assertEqual(
                Group.objects.filter(name=nombre).count(), 1,
                msg=f'{nombre} quedó duplicado tras correr el comando 2 veces',
            )

    def test_no_pisa_ni_rompe_un_grupo_proveedores_ya_existente(self):
        grupo_previo = Group.objects.create(name='PROVEEDORES')

        call_command('crear_grupos_iniciales')

        grupo_previo.refresh_from_db()
        self.assertEqual(Group.objects.filter(name='PROVEEDORES').count(), 1)


class OnboardProveedorTests(TestCase):
    """
    Sub-etapa 2.2 (sesión 87): los 3 escenarios pedidos explícitamente,
    sobre `onboard_proveedor` — el servicio de alta de UN proveedor,
    diseñado para ser una unidad transaccional independiente (ver
    docstring de `supplier_onboarding.py`). `services_correo.enviar_correo`
    se mockea en los 3 (no se hace ninguna llamada real a Graph) — la
    prueba end-to-end real contra Graph ya se hizo en la sesión 73 para
    el mecanismo de correo en sí; acá solo importa CUÁNDO se llama.
    """

    def setUp(self):
        self.parche_correo = patch(
            'apps.base.supplier_onboarding.services_correo.enviar_correo'
        )
        self.mock_enviar_correo = self.parche_correo.start()
        self.addCleanup(self.parche_correo.stop)
        self.mock_enviar_correo.return_value = services_correo.ResultadoEnvioCorreo(enviado=True)

    def test_alta_nueva_crea_user_grupo_y_perfil_correctamente(self):
        # LicTradNum real: 11 dígitos, 100% numérico — si set_password()
        # pasara por un formulario validador (AUTH_PASSWORD_VALIDATORS
        # default de Django, sin sobreescribir en este proyecto), esta
        # contraseña sería rechazada por NumericPasswordValidator. Que
        # el alta funcione sin fallar ya demuestra el punto 4 confirmado.
        resultado = onboard_proveedor(
            card_code='P20266614803',
            card_name='PROVEEDOR EJEMPLO S.A.C.',
            card_fname='PROVEEDOR EJEMPLO SOCIEDAD ANONIMA CERRADA',
            e_mail='contacto@proveedor-ejemplo.com',
            lic_trad_num='20266614803',
        )

        self.assertEqual(resultado.estado, ESTADO_CREADO)
        self.assertTrue(resultado.email_enviado)
        self.assertIsNone(resultado.email_error)

        user = User.objects.get(username='P20266614803')
        self.assertEqual(user.email, 'contacto@proveedor-ejemplo.com')
        self.assertTrue(user.check_password('20266614803'))
        self.assertTrue(user.groups.filter(name='PROVEEDORES').exists())

        perfil = SupplierProfile.objects.get(sap_card_code='P20266614803')
        self.assertEqual(perfil.user_id, user.id)
        self.assertEqual(perfil.razon_social, 'PROVEEDOR EJEMPLO S.A.C.')
        self.assertEqual(perfil.razon_social_legal, 'PROVEEDOR EJEMPLO SOCIEDAD ANONIMA CERRADA')
        self.assertEqual(perfil.ruc, '20266614803')
        self.assertTrue(perfil.debe_cambiar_password)

        self.mock_enviar_correo.assert_called_once()
        destinatario_llamado = self.mock_enviar_correo.call_args[0][0]
        self.assertEqual(destinatario_llamado, 'contacto@proveedor-ejemplo.com')

    def test_resync_de_proveedor_ya_vinculado_no_toca_password_ni_reenvia_correo(self):
        grupo, _ = Group.objects.get_or_create(name='PROVEEDORES')
        user = User.objects.create_user(username='P20100055237', email='viejo@proveedor.com')
        user.set_password('contraseña-elegida-por-el-proveedor')
        user.save()
        user.groups.add(grupo)

        perfil = SupplierProfile.objects.create(
            sap_card_code='P20100055237',
            razon_social='NOMBRE VIEJO',
            correo_electronico='viejo@proveedor.com',
            user=user,
            debe_cambiar_password=False,  # ya se activó formalmente antes
        )

        resultado = onboard_proveedor(
            card_code='P20100055237',
            card_name='NOMBRE ACTUALIZADO DESDE SAP',
            card_fname='RAZON SOCIAL LEGAL ACTUALIZADA',
            e_mail='nuevo@proveedor.com',
            lic_trad_num='20100055237',
        )

        self.assertEqual(resultado.estado, ESTADO_ACTUALIZADO_SIN_ALTA)
        self.assertFalse(resultado.email_enviado)
        self.mock_enviar_correo.assert_not_called()

        # Los datos maestros de SAP sí se refrescan...
        perfil.refresh_from_db()
        self.assertEqual(perfil.razon_social, 'NOMBRE ACTUALIZADO DESDE SAP')
        self.assertEqual(perfil.razon_social_legal, 'RAZON SOCIAL LEGAL ACTUALIZADA')
        self.assertEqual(perfil.correo_electronico, 'nuevo@proveedor.com')

        # ...pero la cuenta ya vinculada queda completamente intacta.
        user.refresh_from_db()
        self.assertTrue(user.check_password('contraseña-elegida-por-el-proveedor'))
        self.assertFalse(user.check_password('20100055237'))
        self.assertFalse(perfil.debe_cambiar_password)
        self.assertEqual(perfil.user_id, user.id)

    def test_fallo_dentro_del_atomic_no_dispara_correo(self):
        # Username ya ocupado por un User real, SIN ningún SupplierProfile
        # que lo referencie todavía (caso de borde real: una cuenta creada
        # a mano con ese username, sin pasar nunca por este flujo) — al
        # intentar el alta, User.objects.create_user() colisiona con la
        # constraint de unicidad de username, DENTRO del atomic().
        User.objects.create_user(username='P99999999999', email='otro@dominio.com')

        with self.assertRaises(IntegrityError):
            onboard_proveedor(
                card_code='P99999999999',
                card_name='PROVEEDOR CON COLISION',
                card_fname='PROVEEDOR CON COLISION SAC',
                e_mail='proveedor-colision@dominio.com',
                lic_trad_num='99999999999',
            )

        # El SupplierProfile que se había empezado a crear en el mismo
        # atomic() también se revirtió — no queda ningún residuo.
        self.assertFalse(SupplierProfile.objects.filter(sap_card_code='P99999999999').exists())
        self.mock_enviar_correo.assert_not_called()
