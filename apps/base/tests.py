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
import re
import time
from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth.models import Group, User
from django.contrib.auth.tokens import default_token_generator
from django.core.management import call_command
from django.db import IntegrityError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

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


class ProveedorSyncEndpointTests(TestCase):
    """
    Sub-etapa 2.3 (sesión 88): `POST /api/v1/sync-proveedores/` — los 3
    escenarios pedidos explícitamente. Mismo patrón de prueba ya
    establecido para los demás endpoints del daemon (`apps.sap_sync.
    tests`): `APIClient` + `Token` real de DRF contra la URL real, no
    llamando al serializer/vista directo. `services_correo.enviar_correo`
    mockeado (no se llama a Graph realmente).
    """

    @classmethod
    def setUpTestData(cls):
        cls.daemon_user = User.objects.create_user(username='daemon_test_proveedores', password='x')
        cls.token = Token.objects.create(user=cls.daemon_user)
        cls.url = reverse('sync-proveedores-list')

    def setUp(self):
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

        self.parche_correo = patch(
            'apps.base.supplier_onboarding.services_correo.enviar_correo'
        )
        self.mock_enviar_correo = self.parche_correo.start()
        self.addCleanup(self.parche_correo.stop)
        self.mock_enviar_correo.return_value = services_correo.ResultadoEnvioCorreo(enviado=True)

    def _payload_valido(self, card_code):
        return {
            'card_code': card_code,
            'card_name': f'PROVEEDOR {card_code}',
            'card_fname': f'PROVEEDOR {card_code} SAC',
            'e_mail': f'{card_code.lower()}@proveedor.com',
            'lic_trad_num': card_code.lstrip('P'),
        }

    def test_lote_con_1_registro_invalido_no_bloquea_a_los_demas(self):
        payload = [
            self._payload_valido('P10000000001'),
            {**self._payload_valido('P10000000002'), 'e_mail': ''},  # inválido
            self._payload_valido('P10000000003'),
        ]

        response = self.client.post(self.url, payload, format='json')

        self.assertEqual(response.status_code, 200)
        resultados = response.data['resultados']
        self.assertEqual(len(resultados), 3)

        self.assertEqual(resultados[0]['status'], ESTADO_CREADO)
        self.assertEqual(resultados[1]['status'], 'error')
        self.assertEqual(resultados[2]['status'], ESTADO_CREADO)

        # Los 2 válidos sí se crearon de verdad en la BD.
        self.assertTrue(User.objects.filter(username='P10000000001').exists())
        self.assertTrue(User.objects.filter(username='P10000000003').exists())
        # El inválido no dejó ningún rastro.
        self.assertFalse(User.objects.filter(username='P10000000002').exists())
        self.assertFalse(SupplierProfile.objects.filter(sap_card_code='P10000000002').exists())

    def test_email_vacio_rechaza_sin_crear_nada_para_ese_registro(self):
        payload = {**self._payload_valido('P20000000001'), 'e_mail': ''}

        response = self.client.post(self.url, payload, format='json')

        self.assertEqual(response.status_code, 200)
        resultado = response.data['resultados'][0]
        self.assertEqual(resultado['status'], 'error')
        self.assertIn('e_mail', resultado['errors'])

        self.assertFalse(User.objects.filter(username='P20000000001').exists())
        self.assertFalse(SupplierProfile.objects.filter(sap_card_code='P20000000001').exists())
        self.mock_enviar_correo.assert_not_called()

    def test_lic_trad_num_vacio_rechaza_sin_crear_nada_para_ese_registro(self):
        payload = {**self._payload_valido('P20000000002'), 'lic_trad_num': ''}

        response = self.client.post(self.url, payload, format='json')

        self.assertEqual(response.status_code, 200)
        resultado = response.data['resultados'][0]
        self.assertEqual(resultado['status'], 'error')
        self.assertIn('lic_trad_num', resultado['errors'])

        self.assertFalse(User.objects.filter(username='P20000000002').exists())
        self.assertFalse(SupplierProfile.objects.filter(sap_card_code='P20000000002').exists())
        self.mock_enviar_correo.assert_not_called()

    def test_lic_trad_num_ausente_rechaza_sin_crear_nada_para_ese_registro(self):
        payload = self._payload_valido('P20000000003')
        del payload['lic_trad_num']

        response = self.client.post(self.url, payload, format='json')

        self.assertEqual(response.status_code, 200)
        resultado = response.data['resultados'][0]
        self.assertEqual(resultado['status'], 'error')
        self.assertIn('lic_trad_num', resultado['errors'])
        self.assertFalse(SupplierProfile.objects.filter(sap_card_code='P20000000003').exists())

    def test_sin_token_da_401_antes_de_procesar(self):
        client_sin_auth = APIClient()

        response = client_sin_auth.post(self.url, self._payload_valido('P30000000001'), format='json')

        self.assertEqual(response.status_code, 401)
        self.assertFalse(SupplierProfile.objects.filter(sap_card_code='P30000000001').exists())
        self.mock_enviar_correo.assert_not_called()

    def test_token_invalido_da_401_antes_de_procesar(self):
        client_token_falso = APIClient()
        client_token_falso.credentials(HTTP_AUTHORIZATION='Token token-que-no-existe-en-la-bd')

        response = client_token_falso.post(self.url, self._payload_valido('P30000000002'), format='json')

        self.assertEqual(response.status_code, 401)
        self.assertFalse(SupplierProfile.objects.filter(sap_card_code='P30000000002').exists())
        self.mock_enviar_correo.assert_not_called()

    def test_payload_de_un_solo_objeto_no_en_lista_tambien_funciona(self):
        response = self.client.post(self.url, self._payload_valido('P40000000001'), format='json')

        self.assertEqual(response.status_code, 200)
        resultados = response.data['resultados']
        self.assertEqual(len(resultados), 1)
        self.assertEqual(resultados[0]['status'], ESTADO_CREADO)
        self.assertTrue(User.objects.filter(username='P40000000001').exists())

    def test_resync_de_proveedor_ya_vinculado_via_endpoint_no_reenvia_correo(self):
        grupo, _ = Group.objects.get_or_create(name='PROVEEDORES')
        user = User.objects.create_user(username='P50000000001', email='ya@existe.com')
        user.set_password('password-elegida-por-el-proveedor')
        user.save()
        user.groups.add(grupo)
        SupplierProfile.objects.create(sap_card_code='P50000000001', user=user)

        response = self.client.post(self.url, self._payload_valido('P50000000001'), format='json')

        self.assertEqual(response.status_code, 200)
        resultado = response.data['resultados'][0]
        self.assertEqual(resultado['status'], ESTADO_ACTUALIZADO_SIN_ALTA)
        self.assertFalse(resultado['email_enviado'])
        self.mock_enviar_correo.assert_not_called()

        user.refresh_from_db()
        self.assertTrue(user.check_password('password-elegida-por-el-proveedor'))


class ForzarCambioPasswordMiddlewareTests(TestCase):
    """
    Sub-etapa 2.4 (sesión 89): los 5 escenarios pedidos explícitamente.
    Usa `self.client` (Django, sesión de cookies normal) — NO `APIClient`
    (eso es para los endpoints Token del daemon, un mecanismo aparte).
    """

    URL_CAMBIO = '/cuenta/cambiar-password/'

    def _crear_proveedor(self, card_code, debe_cambiar=True, password='ruc-temporal-12345'):
        grupo, _ = Group.objects.get_or_create(name='PROVEEDORES')
        user = User.objects.create_user(username=card_code, password=password)
        user.groups.add(grupo)
        perfil = SupplierProfile.objects.create(
            sap_card_code=card_code, user=user, debe_cambiar_password=debe_cambiar,
        )
        return user, perfil

    def test_intercepta_en_el_primer_login(self):
        self._crear_proveedor('P60000000001', password='ruc-temporal-12345')

        response = self.client.post(
            '/login/',
            {'username': 'P60000000001', 'password': 'ruc-temporal-12345'},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request['PATH_INFO'], self.URL_CAMBIO)
        self.assertContains(response, 'Cambio de contraseña obligatorio')

    def test_intercepta_en_acceso_directo_a_url_profunda_con_sesion_ya_iniciada(self):
        self._crear_proveedor('P60000000002', password='ruc-temporal-12345')
        # login() de Django establece la sesión directamente, sin pasar
        # por /login/ -- simula exactamente "el usuario ya tenía una
        # sesión abierta de antes" (no el momento del login en sí).
        self.client.login(username='P60000000002', password='ruc-temporal-12345')

        response = self.client.get('/appointments/portal/')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, self.URL_CAMBIO)

    def test_tras_cambiar_password_deja_de_interceptar_sin_relogin(self):
        user, perfil = self._crear_proveedor('P60000000003', password='ruc-temporal-12345')
        self.client.login(username='P60000000003', password='ruc-temporal-12345')

        # Antes de cambiar: sigue interceptado.
        response = self.client.get('/appointments/portal/')
        self.assertEqual(response.url, self.URL_CAMBIO)

        response = self.client.post(self.URL_CAMBIO, {
            'new_password1': 'NuevaClaveSeguraDelProveedor2026',
            'new_password2': 'NuevaClaveSeguraDelProveedor2026',
        })
        self.assertEqual(response.status_code, 302)  # redirect a home_router

        perfil.refresh_from_db()
        self.assertFalse(perfil.debe_cambiar_password)

        # Mismo cliente/sesión, SIN self.client.login() de nuevo:
        response = self.client.get('/appointments/portal/')
        self.assertEqual(response.status_code, 200)  # ya no rebota

        # Y la sesión sigue siendo válida con la contraseña nueva
        # (update_session_auth_hash) -- si no lo estuviera, este login
        # fallaría o la sesión ya se habría invalidado en el paso de
        # arriba.
        self.client.logout()
        self.assertTrue(
            self.client.login(username='P60000000003', password='NuevaClaveSeguraDelProveedor2026')
        )

    def test_superusuario_sin_supplierprofile_nunca_es_interceptado(self):
        User.objects.create_superuser('admin_middleware_test', 'admin@test.com', 'x')
        self.client.login(username='admin_middleware_test', password='x')

        # No debe lanzar ninguna excepción (RelatedObjectDoesNotExist
        # mal manejado daría 500, no un redirect) y no debe rebotar a
        # cambiar_password_obligatorio.
        response = self.client.get('/home/', follow=True)

        self.assertNotEqual(response.status_code, 500)
        self.assertNotEqual(response.request['PATH_INFO'], self.URL_CAMBIO)

    def test_staff_interno_sin_supplierprofile_nunca_es_interceptado(self):
        grupo, _ = Group.objects.get_or_create(name='ALMACEN')
        user = User.objects.create_user(username='ualmacen_middleware_test', password='x')
        user.groups.add(grupo)
        self.client.login(username='ualmacen_middleware_test', password='x')

        response = self.client.get('/home/', follow=True)

        self.assertNotEqual(response.status_code, 500)
        self.assertNotEqual(response.request['PATH_INFO'], self.URL_CAMBIO)

    def test_logout_sigue_accesible_mientras_flag_en_true(self):
        self._crear_proveedor('P60000000004', password='ruc-temporal-12345')
        self.client.login(username='P60000000004', password='ruc-temporal-12345')

        response = self.client.post('/logout/')

        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(response.url, self.URL_CAMBIO)

    def test_estaticos_siguen_accesibles_mientras_flag_en_true(self):
        self._crear_proveedor('P60000000005', password='ruc-temporal-12345')
        self.client.login(username='P60000000005', password='ruc-temporal-12345')

        response = self.client.get('/static/css/daryza_style.css')

        # No hace falta que el archivo exista de verdad en el entorno de
        # test (sin collectstatic corrido) -- lo único que importa acá
        # es que el middleware nunca lo redirigió a cambiar_password_
        # obligatorio.
        self.assertNotEqual(response.status_code, 302)


class RecuperacionPasswordTests(TestCase):
    """
    Sub-etapa 2.5 (sesión 90): los escenarios pedidos explícitamente
    sobre `apps.base.services_recuperacion` + las 2 vistas nuevas.
    `services_correo.enviar_correo` mockeado (sin llamadas reales a
    Graph); la prueba end-to-end real ya se hizo en la sesión 73 para
    el mecanismo de correo en sí.
    """

    def setUp(self):
        self.parche_correo = patch('apps.base.services_recuperacion.services_correo.enviar_correo')
        self.mock_enviar_correo = self.parche_correo.start()
        self.addCleanup(self.parche_correo.stop)
        self.mock_enviar_correo.return_value = services_correo.ResultadoEnvioCorreo(enviado=True)

    def _crear_proveedor(self, card_code, debe_cambiar=False, email='proveedor@test.com'):
        grupo, _ = Group.objects.get_or_create(name='PROVEEDORES')
        user = User.objects.create_user(username=card_code, password='x', email=email)
        user.groups.add(grupo)
        perfil = SupplierProfile.objects.create(
            sap_card_code=card_code, user=user, debe_cambiar_password=debe_cambiar,
        )
        return user, perfil

    def _token_y_uid(self, user):
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        return uidb64, token

    @staticmethod
    def _sin_csrf_token(content_bytes):
        # Django enmascara el valor del CSRF token por request (previene
        # ataques estilo BREACH) -- 2 renders del MISMO template, incluso
        # sin ningún cambio real de contenido, producen un value=""
        # distinto cada vez. Sin quitarlo, una comparación byte a byte
        # fallaría siempre, sin que eso signifique ninguna diferencia de
        # contenido real -- justo lo que este test necesita verificar.
        return re.sub(rb'name="csrfmiddlewaretoken" value="[^"]*"', b'', content_bytes)

    def test_mensaje_neutro_no_revela_existencia_del_usuario(self):
        self._crear_proveedor('P80000000001')

        resp_real = self.client.post(reverse('solicitar_recuperacion'), {'username': 'P80000000001'})
        resp_inventado = self.client.post(reverse('solicitar_recuperacion'), {'username': 'USUARIO_QUE_JAMAS_EXISTIO'})

        self.assertEqual(resp_real.status_code, resp_inventado.status_code)
        self.assertEqual(
            self._sin_csrf_token(resp_real.content),
            self._sin_csrf_token(resp_inventado.content),
        )

        # Internamente SÍ hay una diferencia real (solo el usuario real
        # dispara el envío) -- lo que nunca debe diferir es lo que la
        # persona ve en pantalla, ya verificado arriba.
        self.mock_enviar_correo.assert_called_once()

    def test_token_valido_permite_cambiar_password_y_limpia_flag_si_aplicaba(self):
        user, perfil = self._crear_proveedor('P80000000002', debe_cambiar=True)
        uidb64, token = self._token_y_uid(user)
        url = reverse('confirmar_recuperacion', args=[uidb64, token])

        response = self.client.post(url, {
            'new_password1': 'ClaveNuevaValidaDelProveedor2026',
            'new_password2': 'ClaveNuevaValidaDelProveedor2026',
        })

        self.assertEqual(response.status_code, 302)

        user.refresh_from_db()
        self.assertTrue(user.check_password('ClaveNuevaValidaDelProveedor2026'))

        perfil.refresh_from_db()
        self.assertFalse(perfil.debe_cambiar_password)

    def test_token_valido_sin_flag_previo_no_lo_activa_por_error(self):
        # Un proveedor que ya estaba activado (debe_cambiar_password=False)
        # y usa recuperación por otro motivo (ej. olvidó su clave elegida)
        # -- el flag debe seguir en False después, no "reactivarse".
        user, perfil = self._crear_proveedor('P80000000003', debe_cambiar=False)
        uidb64, token = self._token_y_uid(user)
        url = reverse('confirmar_recuperacion', args=[uidb64, token])

        self.client.post(url, {
            'new_password1': 'OtraClaveValidaDelProveedor2026',
            'new_password2': 'OtraClaveValidaDelProveedor2026',
        })

        perfil.refresh_from_db()
        self.assertFalse(perfil.debe_cambiar_password)

    def test_membresia_de_grupo_proveedores_queda_intacta(self):
        user, _ = self._crear_proveedor('P80000000004', debe_cambiar=True)
        uidb64, token = self._token_y_uid(user)
        url = reverse('confirmar_recuperacion', args=[uidb64, token])

        self.client.post(url, {
            'new_password1': 'ClaveNuevaValidaDelProveedor2026',
            'new_password2': 'ClaveNuevaValidaDelProveedor2026',
        })

        user.refresh_from_db()
        self.assertTrue(user.groups.filter(name='PROVEEDORES').exists())

    def test_token_usado_no_se_puede_reutilizar(self):
        user, _ = self._crear_proveedor('P80000000005')
        uidb64, token = self._token_y_uid(user)
        url = reverse('confirmar_recuperacion', args=[uidb64, token])

        # Primer uso: exitoso.
        resp1 = self.client.post(url, {
            'new_password1': 'PrimeraClaveValida2026',
            'new_password2': 'PrimeraClaveValida2026',
        })
        self.assertEqual(resp1.status_code, 302)

        # Segundo uso del MISMO token: la contraseña ya cambió, el hash
        # contra el que el token se valida ya no coincide -- Django lo
        # rechaza solo, sin ningún registro de "tokens usados" del lado
        # del Portal.
        resp2 = self.client.get(url)
        self.assertContains(resp2, 'no es válido o ya venció')

    @override_settings(PASSWORD_RESET_TIMEOUT=1)
    def test_token_vencido_rechaza(self):
        user, _ = self._crear_proveedor('P80000000006')
        uidb64, token = self._token_y_uid(user)
        url = reverse('confirmar_recuperacion', args=[uidb64, token])

        time.sleep(2)  # supera el PASSWORD_RESET_TIMEOUT=1s de este test

        response = self.client.get(url)

        self.assertContains(response, 'no es válido o ya venció')

    def test_token_con_uid_mal_formado_rechaza_sin_lanzar(self):
        response = self.client.get(reverse('confirmar_recuperacion', args=['no-es-un-uid-valido', 'token-cualquiera']))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'no es válido o ya venció')

    def test_usuario_inexistente_no_intenta_enviar_correo(self):
        # Sin crear ningún proveedor con ese username en este test --
        # aislado a propósito, para no depender de los demás.
        response = self.client.post(reverse('solicitar_recuperacion'), {'username': 'NUNCA_EXISTIO'})

        self.assertEqual(response.status_code, 200)
        self.mock_enviar_correo.assert_not_called()

    def test_forzar_cambio_password_middleware_no_bloquea_el_flujo_de_recuperacion(self):
        # Proveedor CON el flag en True Y con una sesión activa (caso de
        # borde: otra pestaña logueada) -- el link de recuperación debe
        # seguir siendo accesible, no rebotar a cambiar_password_obligatorio.
        user, _ = self._crear_proveedor('P80000000007', debe_cambiar=True)
        self.client.login(username='P80000000007', password='x')
        uidb64, token = self._token_y_uid(user)

        response = self.client.get(reverse('confirmar_recuperacion', args=[uidb64, token]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Restablecer contraseña')
