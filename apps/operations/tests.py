# apps/operations/tests.py
"""
Suite de pruebas de apps.operations — reescrita en la sesión 33 al cerrar
el rediseño de flujo Materia Prima (Fases 1-7, sesiones 27-33).

Cubre 2 cosas relacionadas pero distintas, sobre el mismo ciclo de vida
del Ticket:

  1. El candado de PERMISO por grupo: qué grupo Django puede ejecutar la
     siguiente acción de etapa sobre un Ticket concreto
     (OperationsService.grupo_requerido_por_etapa /
     ajax_registrar_inspeccion / ajax_autorizar_almacen / ajax_autorizar_
     ingreso / ajax_registrar_salida) — originalmente ALMACEN fijo para
     toda recepción (Fase 3, sesión 5), ahora bifurcado entre ALMACEN y
     MATERIA_PRIMA según el tipo de OC (Fase 3+4, sesión 30).

  2. El candado de ORDEN (Ticket.etapa_actual / OperationsService.
     _validar_etapa / TicketEtapaError): que no se pueda saltar ni repetir
     un paso del flujo, sin importar quién lo intente.

Todos los Tickets de prueba se construyen ejecutando los métodos reales
de OperationsService/AppointmentService (nunca escribiendo etapa_actual/
requiere_calidad directamente sobre el modelo) — así cualquier regresión
en el propio flujo de construcción también haría fallar la suite.
"""
import itertools
from decimal import Decimal

from django.contrib.auth.models import User, Group
from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.appointments.services import AppointmentService
from apps.appointments.models import AppointmentSlot
from apps.base.models import Sede
from apps.operations.models import (
    Ticket, TicketLineInspection, EntradaMercaderia, EntradaMercaderiaLinea,
)
from apps.operations.services import OperationsService, TicketEtapaError
from apps.sap_sync.models import PurchaseOrder, PurchaseOrderLine

# Contador global de doc_num para OCs de prueba — cada test crea sus propios
# datos dentro de su propia transacción (rollback automático al terminar),
# así que un contador compartido entre clases es seguro: nunca hay colisión
# real en la base de datos, solo evita reutilizar el mismo literal a mano
# en decenas de tests (como hacía la suite anterior con 555001, 555002...).
_doc_num_counter = itertools.count(700001)


class OperationsTestBase(TestCase):
    """
    Base compartida por todas las clases de este archivo. `setUpTestData`
    es un classmethod: Django lo ejecuta una vez POR CADA SUBCLASE que lo
    hereda sin sobreescribirlo (no una sola vez para todo el archivo), así
    que cada clase de test tiene su propio juego aislado de 6 grupos + 6
    usuarios (uno por grupo) sin duplicar el setup en cada una.
    """

    @classmethod
    def setUpTestData(cls):
        cls.g_proveedores   = Group.objects.create(name='PROVEEDORES')
        cls.g_compras       = Group.objects.create(name='COMPRAS')
        cls.g_almacen       = Group.objects.create(name='ALMACEN')
        cls.g_vigilancia    = Group.objects.create(name='VIGILANCIA')
        cls.g_calidad       = Group.objects.create(name='CALIDAD')
        cls.g_materia_prima = Group.objects.create(name='MATERIA_PRIMA')

        sufijo = cls.__name__.lower()
        cls.proveedor       = cls._crear_usuario(f'proveedor_{sufijo}', cls.g_proveedores)
        cls.u_compras       = cls._crear_usuario(f'compras_{sufijo}', cls.g_compras)
        cls.u_almacen       = cls._crear_usuario(f'almacen_{sufijo}', cls.g_almacen)
        cls.u_vigilancia    = cls._crear_usuario(f'vigilancia_{sufijo}', cls.g_vigilancia)
        cls.u_calidad       = cls._crear_usuario(f'calidad_{sufijo}', cls.g_calidad)
        cls.u_materia_prima = cls._crear_usuario(f'materia_prima_{sufijo}', cls.g_materia_prima)

    @staticmethod
    def _crear_usuario(username, grupo):
        user = User.objects.create_user(username, password='x')
        user.groups.add(grupo)
        return user

    # ── Construcción de Tickets reales, paso a paso ─────────────────────────

    def _crear_cita_confirmada(self, u_mss_tdb: str, requiere_coa: bool = True) -> Ticket:
        """OC -> solicitud -> confirmación. Ticket queda en PENDIENTE_INGRESO."""
        doc_num = next(_doc_num_counter)
        po = PurchaseOrder.objects.create(
            doc_entry=doc_num, doc_num=doc_num, card_code='TESTCODE', card_name='TEST SAC',
            e_mail='test@test.com', status='PENDIENTE', u_mss_tdb=u_mss_tdb,
        )
        PurchaseOrderLine.objects.create(
            purchase_order=po, line_num=1, item_code='ITEM-TEST', description='Item de prueba',
            quantity_sap=10, und_medida='KG', requiere_coa=(requiere_coa and u_mss_tdb == 'MP'),
        )
        slot = AppointmentSlot.objects.create(
            sede=Sede.objects.get(codigo='LURIN'),
            date='2026-09-01', start_time='08:00', dock='TEST', max_capacity=5
        )
        appointment = AppointmentService.solicitar_cita_borrador(
            user=self.proveedor, slot_id=slot.id, oc_ids=[po.id]
        )
        return AppointmentService.confirmar_cita(
            appointment_id=appointment.id, usuario_almacen=self.u_compras
        )

    def _avanzar_a_vigilancia_ingreso(self, ticket: Ticket) -> Ticket:
        """Carga el COA si la OC lo requiere y ejecuta iniciar_ingreso_planta."""
        for po in ticket.appointment.purchase_orders.all():
            for line in po.lines.filter(requiere_coa=True):
                OperationsService.registrar_coa_proveedor(
                    ticket_id=ticket.id, po_line_id=line.id,
                    coa_url='https://onedrive.example.com/fake-coa.pdf', usuario=self.proveedor,
                )
        return OperationsService.iniciar_ingreso_planta(
            ticket_id=ticket.id, usuario_vigilancia=self.u_vigilancia
        )

    def _avanzar_a_almacen(self, ticket: Ticket, requiere_calidad=None) -> Ticket:
        """
        Ejecuta autorizar_almacen ("Iniciar Recepción") con el actor real
        que le corresponde al ticket (Ticket.es_materia_prima), confirmando
        automáticamente cuando el actor es MATERIA_PRIMA — para probar el
        rechazo por falta de confirmación, no usar este helper: llamar a
        OperationsService.autorizar_almacen directamente.
        """
        es_mp = ticket.es_materia_prima
        usuario = self.u_materia_prima if es_mp else self.u_almacen
        OperationsService.autorizar_almacen(
            ticket_id=ticket.id, usuario=usuario,
            requiere_calidad=requiere_calidad, confirmado=es_mp,
        )
        ticket.refresh_from_db()
        return ticket

    def _crear_ticket_en_etapa(self, etapa: str, u_mss_tdb: str,
                                requiere_coa: bool = True, requiere_calidad=None) -> Ticket:
        """
        Construye un Ticket real hasta la etapa pedida
        (Ticket.ETAPA_VIGILANCIA_INGRESO o Ticket.ETAPA_ALMACEN).
        """
        ticket = self._crear_cita_confirmada(u_mss_tdb, requiere_coa)
        if etapa == Ticket.ETAPA_PENDIENTE_INGRESO:
            return ticket

        ticket = self._avanzar_a_vigilancia_ingreso(ticket)
        if etapa == Ticket.ETAPA_VIGILANCIA_INGRESO:
            return ticket

        ticket = self._avanzar_a_almacen(ticket, requiere_calidad=requiere_calidad)
        if etapa == Ticket.ETAPA_ALMACEN:
            return ticket

        raise ValueError(f"Etapa objetivo no soportada por este helper: {etapa}")

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

    # ── Llamadas HTTP a los 2 endpoints centrales de este archivo ───────────

    def _post_registrar_inspeccion(self, ticket: Ticket):
        return self.client.post(
            '/operations/api/registrar-inspeccion/',
            data={'ticket_id': ticket.id, 'resultados': self._resultados_para(ticket)},
            content_type='application/json',
        )

    def _post_autorizar_almacen(self, ticket: Ticket, **extra):
        return self.client.post(
            '/operations/api/autorizar-almacen/',
            data={'ticket_id': ticket.id, **extra},
            content_type='application/json',
        )


# ═══════════════════════════════════════════════════════════════════════════
# 1-2. Bifurcación del actor de recepción (ALMACEN vs MATERIA_PRIMA)
# ═══════════════════════════════════════════════════════════════════════════

class BifurcacionActorRecepcionTests(OperationsTestBase):
    """
    OperationsService.grupo_requerido_por_etapa, en la etapa
    VIGILANCIA_INGRESO, debe bifurcar entre ALMACEN y MATERIA_PRIMA según
    el tipo de OC vinculada (Ticket.es_materia_prima) — no fijo a ALMACEN
    como antes de la Fase 3+4 (sesión 30).
    """

    def test_oc_materia_prima_bifurca_a_materia_prima(self):
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_VIGILANCIA_INGRESO, 'MP')
        self.assertTrue(ticket.es_materia_prima)
        self.assertEqual(
            OperationsService.grupo_requerido_por_etapa(ticket), 'MATERIA_PRIMA'
        )

    def test_oc_comercial_bifurca_a_almacen(self):
        ticket = self._crear_ticket_en_etapa(
            Ticket.ETAPA_VIGILANCIA_INGRESO, 'CDL', requiere_coa=False
        )
        self.assertFalse(ticket.es_materia_prima)
        self.assertEqual(
            OperationsService.grupo_requerido_por_etapa(ticket), 'ALMACEN'
        )


# ═══════════════════════════════════════════════════════════════════════════
# 5. Confirmación explícita obligatoria para Materia Prima
# ═══════════════════════════════════════════════════════════════════════════

class ConfirmacionMateriaPrimaTests(OperationsTestBase):
    """
    "Iniciar Recepción" (autorizar_almacen / ajax_autorizar_almacen) exige
    confirmado=True cuando el actor es MATERIA_PRIMA — rechaza sin escribir
    nada si falta. Para ALMACEN no exige este paso (mismo comportamiento
    que antes de la Fase 3+4).
    """

    def test_service_materia_prima_sin_confirmado_lanza_validation_error(self):
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_VIGILANCIA_INGRESO, 'MP')

        with self.assertRaises(ValidationError):
            OperationsService.autorizar_almacen(
                ticket_id=ticket.id, usuario=self.u_materia_prima
            )

        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_VIGILANCIA_INGRESO)

    def test_service_almacen_no_requiere_confirmado(self):
        ticket = self._crear_ticket_en_etapa(
            Ticket.ETAPA_VIGILANCIA_INGRESO, 'CDL', requiere_coa=False
        )

        OperationsService.autorizar_almacen(ticket_id=ticket.id, usuario=self.u_almacen)

        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_ALMACEN)

    def test_endpoint_materia_prima_sin_confirmado_responde_400(self):
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_VIGILANCIA_INGRESO, 'MP')

        self.client.force_login(self.u_materia_prima)
        resp = self._post_autorizar_almacen(ticket)

        self.assertEqual(resp.status_code, 400)
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_VIGILANCIA_INGRESO)

    def test_endpoint_materia_prima_con_confirmado_responde_200(self):
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_VIGILANCIA_INGRESO, 'MP')

        self.client.force_login(self.u_materia_prima)
        resp = self._post_autorizar_almacen(ticket, confirmado=True)

        self.assertEqual(resp.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_ALMACEN)

    def test_endpoint_almacen_sin_confirmado_responde_200(self):
        """Regresión: el botón simple de Almacén (sin el paso de confirmación) sigue funcionando."""
        ticket = self._crear_ticket_en_etapa(
            Ticket.ETAPA_VIGILANCIA_INGRESO, 'CDL', requiere_coa=False
        )

        self.client.force_login(self.u_almacen)
        resp = self._post_autorizar_almacen(ticket)

        self.assertEqual(resp.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_ALMACEN)


# ═══════════════════════════════════════════════════════════════════════════
# 3-4. Fork de Ticket.requiere_calidad (CALIDAD vs "Continuar sin Calidad")
# ═══════════════════════════════════════════════════════════════════════════

class RequiereCalidadForkTests(OperationsTestBase):
    """
    Ticket.requiere_calidad (capturado en "Iniciar Recepción") decide si el
    ticket pasa por CALIDAD o salta directo a VIGILANCIA_SALIDA — ya no
    tipo_flujo (legacy). Incluye el test de regresión del bug corregido en
    la sesión 30b (panel_calidad filtraba por tipo_flujo y podía dejar
    tickets con requiere_calidad=True atascados, sin aparecer nunca en su
    propia bandeja de Pendientes).
    """

    def test_requiere_calidad_true_pasa_por_inspeccion_propia_antes_de_avanzar_a_calidad(self):
        """
        Corrección de flujo (sesión 37): a ETAPA_ALMACEN le corresponde
        SIEMPRE el actor de recepción, nunca CALIDAD directamente — antes,
        con requiere_calidad=True, grupo_requerido_por_etapa saltaba
        directo a 'CALIDAD' en cuanto el ticket llegaba a ALMACEN,
        bloqueando la propia inspección del actor (bug real reportado en
        prueba manual).
        """
        ticket = self._crear_ticket_en_etapa(
            Ticket.ETAPA_ALMACEN, 'MP', requiere_calidad=True
        )
        self.assertTrue(ticket.requiere_calidad)
        self.assertEqual(
            OperationsService.grupo_requerido_por_etapa(ticket), 'MATERIA_PRIMA',
            "A ETAPA_ALMACEN le corresponde siempre el actor de recepción, "
            "nunca CALIDAD directamente — sin importar requiere_calidad."
        )

        # Paso 1: el actor SIEMPRE hace su propia inspección y la guarda.
        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=self.u_materia_prima,
            resultados=self._resultados_para(ticket),
        )
        ticket.refresh_from_db()
        self.assertEqual(
            ticket.etapa_actual, Ticket.ETAPA_CALIDAD,
            "Tras su propia inspección, con requiere_calidad=True debe avanzar "
            "a CALIDAD para una inspección adicional (no saltársela)."
        )
        self.assertEqual(OperationsService.grupo_requerido_por_etapa(ticket), 'CALIDAD')

        # Paso 2: Calidad hace SU PROPIA inspección adicional, independiente.
        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=self.u_calidad,
            resultados=self._resultados_para(ticket),
        )
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_VIGILANCIA_SALIDA)

    def test_requiere_calidad_true_aparece_en_panel_calidad_pendientes_tras_inspeccion_propia(self):
        """
        Test de regresión del bug de la sesión 30b (panel_calidad filtraba
        por tipo_flujo) + de la corrección de flujo de esta sesión (37): el
        ticket solo debe aparecer en Pendientes de Calidad DESPUÉS de que
        el actor de recepción complete su propia inspección (etapa_actual
        == CALIDAD) — no apenas requiere_calidad quede en True.
        """
        ticket = self._crear_ticket_en_etapa(
            Ticket.ETAPA_ALMACEN, 'CDL', requiere_coa=False, requiere_calidad=True
        )
        # tipo_flujo (legacy) es SOLO_ALMACEN para esta OC comercial, aunque
        # requiere_calidad quedó en True — la divergencia exacta del bug.
        self.assertEqual(ticket.tipo_flujo, 'SOLO_ALMACEN')
        self.assertTrue(ticket.requiere_calidad)

        self.client.force_login(self.u_calidad)

        # Todavía NO debe aparecer: el actor (ALMACEN) no hizo su propia inspección.
        resp_antes = self.client.get('/operations/calidad/')
        ids_antes = [t.id for t in resp_antes.context['tickets']]
        self.assertNotIn(ticket.id, ids_antes)

        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=self.u_almacen,
            resultados=self._resultados_para(ticket),
        )
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_CALIDAD)

        resp = self.client.get('/operations/calidad/')

        self.assertEqual(resp.status_code, 200)
        ids_pendientes = [t.id for t in resp.context['tickets']]
        self.assertIn(
            ticket.id, ids_pendientes,
            "El ticket con requiere_calidad=True debía aparecer en Pendientes de "
            "Calidad una vez que el actor de recepción completó su propia "
            "inspección, sin importar tipo_flujo (bug de la sesión 30b, no debe "
            "reaparecer)."
        )

    def test_requiere_calidad_false_almacen_salta_a_vigilancia_salida(self):
        ticket = self._crear_ticket_en_etapa(
            Ticket.ETAPA_ALMACEN, 'CDL', requiere_coa=False, requiere_calidad=False
        )
        self.assertFalse(ticket.requiere_calidad)

        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=self.u_almacen,
            resultados=self._resultados_para(ticket),
        )
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_VIGILANCIA_SALIDA)

    def test_requiere_calidad_false_materia_prima_salta_a_vigilancia_salida(self):
        """Mismo camino que el caso Almacén, pero para el actor Materia Prima."""
        ticket = self._crear_ticket_en_etapa(
            Ticket.ETAPA_ALMACEN, 'MP', requiere_calidad=False
        )
        self.assertFalse(ticket.requiere_calidad)
        self.assertEqual(
            OperationsService.grupo_requerido_por_etapa(ticket), 'MATERIA_PRIMA',
            "Con requiere_calidad=False, el cierre debe volver al mismo actor "
            "de recepción (MATERIA_PRIMA), no a CALIDAD."
        )

        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=self.u_materia_prima,
            resultados=self._resultados_para(ticket),
        )
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_VIGILANCIA_SALIDA)

    def test_requiere_calidad_desacoplado_de_tipo_flujo(self):
        """
        Un ticket de OC Materia Prima (tipo_flujo='CON_CALIDAD', legacy)
        puede recibir requiere_calidad=False explícito al Iniciar
        Recepción, y el cierre debe saltar Calidad igual.
        """
        ticket = self._crear_ticket_en_etapa(
            Ticket.ETAPA_ALMACEN, 'MP', requiere_calidad=False
        )
        self.assertEqual(ticket.tipo_flujo, 'CON_CALIDAD')
        self.assertFalse(ticket.requiere_calidad)

        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=self.u_materia_prima,
            resultados=self._resultados_para(ticket),
        )
        ticket.refresh_from_db()
        self.assertEqual(
            ticket.etapa_actual, Ticket.ETAPA_VIGILANCIA_SALIDA,
            "Con requiere_calidad=False, debía saltar directo a VIGILANCIA_SALIDA "
            "aunque tipo_flujo siga siendo CON_CALIDAD (legacy)."
        )

    def test_requiere_calidad_default_marcado_para_materia_prima(self):
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'MP')
        self.assertTrue(ticket.requiere_calidad)

    def test_requiere_calidad_default_desmarcado_para_almacen(self):
        ticket = self._crear_ticket_en_etapa(
            Ticket.ETAPA_ALMACEN, 'CDL', requiere_coa=False
        )
        self.assertFalse(ticket.requiere_calidad)

    def test_accion_pendiente_materia_prima_no_dice_sin_calidad_si_requiere_calidad(self):
        """
        _tickets_pendientes_materia_prima (panel_materia_prima) decía
        siempre "Continuar sin Calidad" en ETAPA_ALMACEN — con la
        corrección de flujo (sesión 37), un ticket ahí con
        requiere_calidad=True SIGUE pendiente de la propia inspección del
        actor, y ese rótulo sería falso (el ticket SÍ avanzará a Calidad).
        """
        from apps.operations.views import _tickets_pendientes_materia_prima

        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'MP')  # requiere_calidad=True
        pendientes = {t.id: t.accion_pendiente for t in _tickets_pendientes_materia_prima()}
        self.assertIn(ticket.id, pendientes)
        self.assertNotEqual(pendientes[ticket.id], 'Continuar sin Calidad')

    def test_muelle_se_guarda_en_ticket_no_en_slot(self):
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_VIGILANCIA_INGRESO, 'CDL', requiere_coa=False)
        dock_original = ticket.appointment.slot.dock

        OperationsService.autorizar_almacen(
            ticket_id=ticket.id, usuario=self.u_almacen, muelle='MUELLE-9'
        )

        ticket.refresh_from_db()
        self.assertEqual(ticket.muelle, 'MUELLE-9')
        ticket.appointment.slot.refresh_from_db()
        self.assertEqual(ticket.appointment.slot.dock, dock_original)


# ═══════════════════════════════════════════════════════════════════════════
# 6-7. Candado de permiso por grupo (ALMACEN / CALIDAD / MATERIA_PRIMA)
# ═══════════════════════════════════════════════════════════════════════════

class PermisoPorGrupoTests(OperationsTestBase):
    """
    Verifica que ajax_registrar_inspeccion y ajax_autorizar_almacen exijan
    el grupo específico que le corresponde al turno real del ticket
    (grupo_requerido_por_etapa) — no basta con pertenecer a "algún" grupo
    interno. Cubre las 3 combinaciones de actor de recepción posibles
    (ALMACEN/CALIDAD ya cubiertas desde la Fase 3 original, sesión 5;
    MATERIA_PRIMA agregado en esta sesión) en ambas direcciones.
    """

    # ── ajax_registrar-inspeccion: ALMACEN vs MATERIA_PRIMA vs CALIDAD ──────
    # Corrección de flujo (sesión 37): a ETAPA_ALMACEN le corresponde SIEMPRE
    # el actor de recepción, sin importar requiere_calidad — CALIDAD solo
    # puede actuar DESPUÉS, en su propia etapa separada (ETAPA_CALIDAD).

    def test_almacen_no_puede_actuar_en_recepcion_de_ticket_materia_prima(self):
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'MP')  # requiere_calidad=True
        self.assertEqual(OperationsService.grupo_requerido_por_etapa(ticket), 'MATERIA_PRIMA')

        self.client.force_login(self.u_almacen)
        resp = self._post_registrar_inspeccion(ticket)

        self.assertEqual(resp.status_code, 403)
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_ALMACEN)

    def test_calidad_no_puede_actuar_mientras_es_turno_del_actor_de_recepcion(self):
        """
        Bug corregido: antes, con requiere_calidad=True, CALIDAD ya podía
        actuar apenas el ticket llegaba a ETAPA_ALMACEN (grupo_requerido_
        por_etapa saltaba directo a 'CALIDAD'), bloqueando por completo la
        propia inspección del actor de recepción — confirmado en prueba
        manual real con un ticket Materia Prima.
        """
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'MP')  # requiere_calidad=True

        self.client.force_login(self.u_calidad)
        resp = self._post_registrar_inspeccion(ticket)

        self.assertEqual(resp.status_code, 403)
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_ALMACEN)

    def test_materia_prima_puede_hacer_su_propia_inspeccion_aunque_requiera_calidad(self):
        """
        Verificación central del punto 3 del pedido: un ticket MATERIA_PRIMA
        con requiere_calidad=True debe poder editar CANT. REAL/ESTADO/
        OBSERVACIÓN normalmente, guardar, y recién ahí avanzar a Calidad
        (no saltárselo) — antes quedaba bloqueado por completo.
        """
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'MP')  # requiere_calidad=True

        self.client.force_login(self.u_materia_prima)
        resp = self._post_registrar_inspeccion(ticket)

        self.assertEqual(resp.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(
            ticket.etapa_actual, Ticket.ETAPA_CALIDAD,
            "Tras su propia inspección, con requiere_calidad=True debe avanzar "
            "a CALIDAD para una inspección adicional — no saltársela."
        )

        # Y ahora sí es el turno de Calidad, con su PROPIA inspección adicional.
        self.assertEqual(OperationsService.grupo_requerido_por_etapa(ticket), 'CALIDAD')
        self.client.force_login(self.u_calidad)
        resp2 = self._post_registrar_inspeccion(ticket)
        self.assertEqual(resp2.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_VIGILANCIA_SALIDA)

    def test_materia_prima_no_puede_actuar_una_vez_que_es_turno_de_calidad(self):
        """Tras su propia inspección, MATERIA_PRIMA queda bloqueado — el turno pasa exclusivamente a CALIDAD."""
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'MP')  # requiere_calidad=True
        self.client.force_login(self.u_materia_prima)
        self._post_registrar_inspeccion(ticket)
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_CALIDAD)

        resp = self._post_registrar_inspeccion(ticket)  # MATERIA_PRIMA reintenta

        self.assertEqual(resp.status_code, 403)
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_CALIDAD)

    def test_calidad_registra_su_propia_inspeccion_independiente_de_recepcion(self):
        """
        La inspección de Calidad es GENUINAMENTE independiente de la del
        actor de recepción — cada una queda en su propia fila (etapa=
        'ALMACEN' vs etapa='CALIDAD'), con sus propios valores, no una
        copia/relabeling de la misma.
        """
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'MP')
        insp_id = TicketLineInspection.objects.get(ticket=ticket, etapa='ALMACEN').id

        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=self.u_materia_prima,
            resultados=[{'inspeccion_id': insp_id, 'estado': 'CONFORME', 'cantidad_modificada': '9.5000'}],
        )
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_CALIDAD)

        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=self.u_calidad,
            resultados=[{'inspeccion_id': insp_id, 'estado': 'RECHAZADO', 'cantidad_modificada': '8.0000'}],
        )
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_VIGILANCIA_SALIDA)

        insp_almacen = TicketLineInspection.objects.get(ticket=ticket, etapa='ALMACEN')
        insp_calidad = TicketLineInspection.objects.get(ticket=ticket, etapa='CALIDAD')
        self.assertEqual(str(insp_almacen.cantidad_modificada), '9.5000')
        self.assertEqual(insp_almacen.estado, 'CONFORME')
        self.assertEqual(str(insp_calidad.cantidad_modificada), '8.0000')
        self.assertEqual(insp_calidad.estado, 'RECHAZADO')

    def test_calidad_no_puede_actuar_cuando_turno_es_almacen(self):
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'CDL', requiere_coa=False)
        self.assertEqual(OperationsService.grupo_requerido_por_etapa(ticket), 'ALMACEN')

        self.client.force_login(self.u_calidad)
        resp = self._post_registrar_inspeccion(ticket)

        self.assertEqual(resp.status_code, 403)
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_ALMACEN)

    def test_almacen_si_puede_actuar_en_su_propio_turno(self):
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'CDL', requiere_coa=False)

        self.client.force_login(self.u_almacen)
        resp = self._post_registrar_inspeccion(ticket)

        self.assertEqual(resp.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_VIGILANCIA_SALIDA)

    # ── ajax_registrar-inspeccion: ALMACEN vs MATERIA_PRIMA (nuevo, punto 6) ──

    def test_almacen_no_puede_cerrar_inspeccion_de_ticket_materia_prima_sin_calidad(self):
        """Ticket MP con requiere_calidad=False: el cierre le toca a MATERIA_PRIMA, no a ALMACEN."""
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'MP', requiere_calidad=False)
        self.assertEqual(OperationsService.grupo_requerido_por_etapa(ticket), 'MATERIA_PRIMA')

        self.client.force_login(self.u_almacen)
        resp = self._post_registrar_inspeccion(ticket)

        self.assertEqual(resp.status_code, 403)
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_ALMACEN)

    def test_materia_prima_si_puede_cerrar_su_propia_inspeccion_sin_calidad(self):
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'MP', requiere_calidad=False)

        self.client.force_login(self.u_materia_prima)
        resp = self._post_registrar_inspeccion(ticket)

        self.assertEqual(resp.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_VIGILANCIA_SALIDA)

    def test_materia_prima_no_puede_cerrar_inspeccion_de_ticket_comercial(self):
        """Ticket comercial con requiere_calidad=False: el cierre le toca a ALMACEN, no a MATERIA_PRIMA."""
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'CDL', requiere_coa=False)
        self.assertEqual(OperationsService.grupo_requerido_por_etapa(ticket), 'ALMACEN')

        self.client.force_login(self.u_materia_prima)
        resp = self._post_registrar_inspeccion(ticket)

        self.assertEqual(resp.status_code, 403)
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_ALMACEN)

    # ── ajax_autorizar-almacen ("Iniciar Recepción"): ALMACEN vs MATERIA_PRIMA ──

    def test_almacen_no_puede_iniciar_recepcion_de_ticket_materia_prima(self):
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_VIGILANCIA_INGRESO, 'MP')

        self.client.force_login(self.u_almacen)
        resp = self._post_autorizar_almacen(ticket, confirmado=True)

        self.assertEqual(resp.status_code, 403)
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_VIGILANCIA_INGRESO)

    def test_materia_prima_no_puede_iniciar_recepcion_de_ticket_comercial(self):
        ticket = self._crear_ticket_en_etapa(
            Ticket.ETAPA_VIGILANCIA_INGRESO, 'CDL', requiere_coa=False
        )

        self.client.force_login(self.u_materia_prima)
        resp = self._post_autorizar_almacen(ticket, confirmado=True)

        self.assertEqual(resp.status_code, 403)
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_VIGILANCIA_INGRESO)

    # ── Superusuario siempre puede, sin importar el grupo ────────────────────

    def test_superusuario_siempre_puede(self):
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'MP')
        admin = User.objects.create_superuser('admin_permiso_test', 'admin@test.com', 'x')

        self.client.force_login(admin)
        resp = self._post_registrar_inspeccion(ticket)

        self.assertEqual(resp.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_CALIDAD)


# ═══════════════════════════════════════════════════════════════════════════
# 7. Candado de orden (Ticket.etapa_actual / TicketEtapaError)
# ═══════════════════════════════════════════════════════════════════════════

class CandadoDeEtapaTests(OperationsTestBase):
    """
    OperationsService._validar_etapa impide saltarse o repetir un paso del
    flujo, sin importar qué grupo lo intente — cobertura que ya era válida
    antes del rediseño Materia Prima (sesión 4) y sigue siéndolo con la
    bifurcación de actor (sesiones 30/33).
    """

    def test_no_se_puede_iniciar_recepcion_sin_autorizar_ingreso_primero(self):
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_PENDIENTE_INGRESO, 'CDL', requiere_coa=False)

        with self.assertRaises(ValidationError):
            OperationsService.autorizar_almacen(ticket_id=ticket.id, usuario=self.u_almacen)

        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_PENDIENTE_INGRESO)

    def test_no_se_puede_reautorizar_recepcion_dos_veces(self):
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'CDL', requiere_coa=False)

        with self.assertRaises(TicketEtapaError):
            OperationsService.autorizar_almacen(ticket_id=ticket.id, usuario=self.u_almacen)

        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_ALMACEN)

    def test_no_se_puede_registrar_calidad_antes_de_iniciar_recepcion(self):
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_VIGILANCIA_INGRESO, 'MP')

        with self.assertRaises(TicketEtapaError):
            OperationsService.registrar_calidad(
                ticket_id=ticket.id, usuario_calidad=self.u_calidad, resultados=[]
            )

        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_VIGILANCIA_INGRESO)

    def test_no_se_puede_registrar_salida_antes_de_cerrar_calidad(self):
        """Ticket con requiere_calidad=True, todavía en ALMACEN (registrar_calidad no corrió)."""
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'MP', requiere_calidad=True)

        with self.assertRaises(TicketEtapaError):
            OperationsService.registrar_salida(ticket_id=ticket.id, usuario_vigilancia=self.u_vigilancia)

        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_ALMACEN)

    def test_no_se_puede_registrar_salida_antes_de_que_calidad_haga_su_propia_inspeccion(self):
        """
        Sesión 37: nuevo paso intermedio genuino (ETAPA_CALIDAD) — una vez
        que el actor de recepción guarda su propia inspección con
        requiere_calidad=True, el ticket queda esperando la inspección
        ADICIONAL de Calidad; registrar_salida debe seguir bloqueado hasta
        que esa segunda inspección ocurra.
        """
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'MP', requiere_calidad=True)
        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=self.u_materia_prima,
            resultados=self._resultados_para(ticket),
        )
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_CALIDAD)

        with self.assertRaises(TicketEtapaError):
            OperationsService.registrar_salida(ticket_id=ticket.id, usuario_vigilancia=self.u_vigilancia)

        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_CALIDAD)

    def test_boton_registrar_salida_no_aparece_prematuramente_mientras_calidad_no_actua(self):
        """
        puede_registrar_salida (contexto de detalle_ticket) debe basarse en
        etapa_actual == VIGILANCIA_SALIDA, no en la mera EXISTENCIA de la
        fila CALIDAD_INSPECCION — con el nuevo flujo de 2 pasos, esa fila
        se abre ni bien el actor de recepción termina su propia inspección,
        antes de que Calidad haga la suya.
        """
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'MP', requiere_calidad=True)
        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=self.u_materia_prima,
            resultados=self._resultados_para(ticket),
        )
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_CALIDAD)

        self.client.force_login(self.u_vigilancia)
        resp = self.client.get(f'/operations/ticket/{ticket.id}/')

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(
            resp.context['acciones']['puede_registrar_salida'],
            "No debe poder registrarse la salida antes de que Calidad haga su "
            "propia inspección adicional."
        )

    def test_no_se_puede_registrar_salida_dos_veces(self):
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'CDL', requiere_coa=False)
        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=self.u_almacen,
            resultados=self._resultados_para(ticket),
        )
        OperationsService.registrar_salida(ticket_id=ticket.id, usuario_vigilancia=self.u_vigilancia)
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, 'FINALIZADO')

        with self.assertRaises(ValidationError):
            OperationsService.registrar_salida(ticket_id=ticket.id, usuario_vigilancia=self.u_vigilancia)


# ═══════════════════════════════════════════════════════════════════════════
# 7. Permisos de Vigilancia (sin cambios respecto al rediseño Materia Prima)
# ═══════════════════════════════════════════════════════════════════════════

class PermisosVigilanciaTests(OperationsTestBase):
    """
    autorizar-ingreso y registrar-salida siguen siendo exclusivos de
    VIGILANCIA (@vigilancia_required) — el rediseño Materia Prima no tocó
    estos 2 endpoints, se cubren aquí para que una regresión futura no
    pase inadvertida.
    """

    def test_almacen_no_puede_autorizar_ingreso(self):
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_PENDIENTE_INGRESO, 'CDL', requiere_coa=False)

        self.client.force_login(self.u_almacen)
        resp = self.client.post(
            '/operations/api/autorizar-ingreso/',
            data={'ticket_id': ticket.id}, content_type='application/json',
        )
        self.assertEqual(resp.status_code, 302)

    def test_vigilancia_si_puede_autorizar_ingreso(self):
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_PENDIENTE_INGRESO, 'CDL', requiere_coa=False)

        self.client.force_login(self.u_vigilancia)
        resp = self.client.post(
            '/operations/api/autorizar-ingreso/',
            data={'ticket_id': ticket.id}, content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_VIGILANCIA_INGRESO)

    def test_almacen_no_puede_registrar_salida(self):
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'CDL', requiere_coa=False)
        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=self.u_almacen,
            resultados=self._resultados_para(ticket),
        )

        self.client.force_login(self.u_almacen)
        resp = self.client.post(
            '/operations/api/registrar-salida/',
            data={'ticket_id': ticket.id}, content_type='application/json',
        )
        self.assertEqual(resp.status_code, 302)

    def test_vigilancia_si_puede_registrar_salida(self):
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'CDL', requiere_coa=False)
        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=self.u_almacen,
            resultados=self._resultados_para(ticket),
        )

        self.client.force_login(self.u_vigilancia)
        resp = self.client.post(
            '/operations/api/registrar-salida/',
            data={'ticket_id': ticket.id}, content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, 'FINALIZADO')


# ═══════════════════════════════════════════════════════════════════════════
# Validación de COA obligatorio en "Autorizar Ingreso" (sesión 36)
# ═══════════════════════════════════════════════════════════════════════════

class ValidacionCoaObligatorioIngresoTests(OperationsTestBase):
    """
    Bug crítico corregido: iniciar_ingreso_planta nunca bloqueaba
    realmente el ingreso por falta de COA — antes solo validaba cuando
    tipo_flujo == 'CON_CALIDAD', y bastaba que UNA sola línea con
    requiere_coa=True tuviera su TicketLineCOA cargado (any(...)) para
    pasar, sin importar cuántas otras líneas requeridas siguieran sin él.
    Ahora exige TODAS las líneas requiere_coa=True, sin condicionar por
    tipo_flujo/tipo de OC.
    """

    def test_bloquea_ingreso_si_falta_coa_de_una_linea(self):
        ticket = self._crear_cita_confirmada('MP', requiere_coa=True)
        # A propósito no se carga ningún TicketLineCOA.
        with self.assertRaises(ValidationError) as ctx:
            OperationsService.iniciar_ingreso_planta(
                ticket_id=ticket.id, usuario_vigilancia=self.u_vigilancia,
            )
        self.assertIn('ITEM-TEST', str(ctx.exception))
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, 'PROGRAMADO')
        self.assertEqual(ticket.etapa_actual, Ticket.ETAPA_PENDIENTE_INGRESO)

    def test_bloquea_ingreso_via_endpoint_ajax_con_400_y_mensaje_claro(self):
        ticket = self._crear_cita_confirmada('MP', requiere_coa=True)
        self.client.force_login(self.u_vigilancia)
        resp = self.client.post(
            '/operations/api/autorizar-ingreso/',
            data={'ticket_id': ticket.id}, content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('COA', resp.json()['msg'])
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, 'PROGRAMADO')

    def test_permite_ingreso_una_vez_cargado_el_coa(self):
        ticket = self._avanzar_a_vigilancia_ingreso(
            self._crear_cita_confirmada('MP', requiere_coa=True)
        )
        self.assertEqual(ticket.estado, 'EN_PLANTA')

    def test_no_bloquea_si_ninguna_linea_requiere_coa(self):
        ticket = self._crear_cita_confirmada('CDL', requiere_coa=False)
        ticket = OperationsService.iniciar_ingreso_planta(
            ticket_id=ticket.id, usuario_vigilancia=self.u_vigilancia,
        )
        self.assertEqual(ticket.estado, 'EN_PLANTA')

    def test_bloquea_tambien_en_flujo_comercial_sin_depender_de_tipo_flujo(self):
        """
        Antes del fix, un ticket SOLO_ALMACEN (tipo_flujo != 'CON_CALIDAD')
        nunca validaba COA, sin importar requiere_coa por línea. Se fuerza
        requiere_coa=True en una línea comercial (el helper solo lo activa
        para 'MP') para confirmar que el candado nuevo no depende de
        tipo_flujo/tipo de OC.
        """
        ticket = self._crear_cita_confirmada('CDL', requiere_coa=False)
        self.assertNotEqual(ticket.tipo_flujo, 'CON_CALIDAD')
        po = ticket.appointment.purchase_orders.first()
        po.lines.update(requiere_coa=True)

        with self.assertRaises(ValidationError):
            OperationsService.iniciar_ingreso_planta(
                ticket_id=ticket.id, usuario_vigilancia=self.u_vigilancia,
            )


# ═══════════════════════════════════════════════════════════════════════════
# 8. Numeración visible al usuario: siempre appointment.id, nunca ticket.pk
#    (sesión 56)
# ═══════════════════════════════════════════════════════════════════════════

class NumeracionUnificadaTests(OperationsTestBase):
    """
    Ticket.id y Appointment.id son secuencias de BD independientes que solo
    coinciden por casualidad (sesión 54: confirmado con datos reales, un
    Appointment que nunca llega a confirmarse consume su id sin que exista
    ningún Ticket correspondiente). Toda pantalla que muestre "Ticket #X"
    debe mostrar appointment.id, el mismo número que ya vive en el código QR
    (DYZ-{appointment.id}-...) — nunca ticket.pk.

    Para probarlo de verdad (no solo confiar en que "debería" divergir), se
    fuerza la divergencia igual que ocurre en producción: se solicita y
    rechaza una cita de sobra ANTES de construir el Ticket real bajo prueba
    — consume un id de Appointment sin generar ningún Ticket, exactamente
    el mecanismo confirmado en la sesión 54.
    """

    def _forzar_divergencia_de_ids(self):
        """Consume 1 id de Appointment sin Ticket correspondiente."""
        doc_num = next(_doc_num_counter)
        po = PurchaseOrder.objects.create(
            doc_entry=doc_num, doc_num=doc_num, card_code='TESTCODE',
            card_name='TEST SAC DESCARTABLE', e_mail='test@test.com',
            status='PENDIENTE', u_mss_tdb='',
        )
        PurchaseOrderLine.objects.create(
            purchase_order=po, line_num=1, item_code='ITEM-DESCARTABLE',
            description='x', quantity_sap=1, und_medida='UND',
        )
        slot = AppointmentSlot.objects.create(
            sede=Sede.objects.get(codigo='LURIN'),
            date='2026-09-02', start_time='08:00', dock='TEST', max_capacity=5,
        )
        appointment = AppointmentService.solicitar_cita_borrador(
            user=self.proveedor, slot_id=slot.id, oc_ids=[po.id]
        )
        appointment.status = 'RECHAZADO'
        appointment.save(update_fields=['status'])

    def _crear_ticket_con_ids_divergentes(self, **kwargs) -> Ticket:
        self._forzar_divergencia_de_ids()
        ticket = self._crear_cita_confirmada(**kwargs)
        # Sanity check: si esto falla, la divergencia no se forzó y el resto
        # del test no estaría probando nada real.
        self.assertNotEqual(
            ticket.id, ticket.appointment_id,
            "No se logró forzar la divergencia de ids — el test no prueba nada.",
        )
        return ticket

    def test_detalle_ticket_muestra_appointment_id_no_ticket_pk(self):
        ticket = self._crear_ticket_con_ids_divergentes(u_mss_tdb='CDL', requiere_coa=False)
        self.client.force_login(self.u_vigilancia)

        resp = self.client.get(f'/operations/ticket/{ticket.id}/')
        html = resp.content.decode('utf-8')
        self.assertIn(f'Ticket #{ticket.appointment_id}', html)
        self.assertNotIn(f'Ticket #{ticket.id}', html)

    def test_trazabilidad_ticket_muestra_appointment_id_no_ticket_pk(self):
        ticket = self._crear_ticket_con_ids_divergentes(u_mss_tdb='CDL', requiere_coa=False)
        self.client.force_login(self.u_compras)

        resp = self.client.get(f'/operations/ticket/{ticket.id}/trazabilidad/')
        html = resp.content.decode('utf-8')
        self.assertIn(f'Ticket #{ticket.appointment_id}', html)
        self.assertNotIn(f'Ticket #{ticket.id}', html)

    def test_ticket_a_row_usa_appointment_id(self):
        from apps.base.reporting import ticket_a_row

        ticket = self._crear_ticket_con_ids_divergentes(u_mss_tdb='CDL', requiere_coa=False)
        row = ticket_a_row(Ticket.objects.select_related('appointment').get(id=ticket.id))
        self.assertEqual(row.ticket, f'#{ticket.appointment_id}')

    def test_mensajes_de_confirmacion_y_error_usan_appointment_id(self):
        """
        Los mensajes JSON (toasts) de confirmar_cita/autorizar_almacen/
        autorizar_ingreso/registrar_salida, y los de error de permiso/etapa,
        también deben citar appointment.id, no ticket.pk.
        """
        self._forzar_divergencia_de_ids()
        doc_num = next(_doc_num_counter)
        po = PurchaseOrder.objects.create(
            doc_entry=doc_num, doc_num=doc_num, card_code='TESTCODE', card_name='TEST SAC',
            e_mail='test@test.com', status='PENDIENTE', u_mss_tdb='',
        )
        PurchaseOrderLine.objects.create(
            purchase_order=po, line_num=1, item_code='ITEM-TEST', description='x',
            quantity_sap=1, und_medida='UND',
        )
        slot = AppointmentSlot.objects.create(
            sede=Sede.objects.get(codigo='LURIN'),
            date='2026-09-03', start_time='08:00', dock='TEST', max_capacity=5,
        )
        appointment = AppointmentService.solicitar_cita_borrador(
            user=self.proveedor, slot_id=slot.id, oc_ids=[po.id]
        )

        self.client.force_login(self.u_compras)
        resp = self.client.post(
            '/operations/api/compras/confirmar-cita/',
            data={'appointment_id': appointment.id}, content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        ticket = Ticket.objects.get(appointment_id=appointment.id)
        self.assertNotEqual(ticket.id, ticket.appointment_id, 'Sin divergencia el test no prueba nada.')
        self.assertIn(f'Ticket #{ticket.appointment_id}', resp.json()['msg'])
        self.assertNotIn(f'Ticket #{ticket.id}', resp.json()['msg'])


# ═══════════════════════════════════════════════════════════════════════════
# 9. EntradaMercaderia — generación automática al finalizar (sesión 57)
# ═══════════════════════════════════════════════════════════════════════════

class EntradaMercaderiaTests(OperationsTestBase):
    """
    Cubre lo pedido explícitamente: (1) creación automática al finalizar el
    Ticket, (2) idempotencia del upsert (get_or_create/update_or_create al
    reintentar el trigger), (3) EntradaMercaderiaLinea.cantidad = cantidad
    REAL inspeccionada (TicketLineInspection.cantidad_modificada), nunca
    PurchaseOrderLine.quantity_sap.

    Todos los Tickets se construyen con requiere_calidad=False (actor
    ALMACEN cierra su propia inspección y avanza directo a
    VIGILANCIA_SALIDA, sesión 37) — el trigger de EntradaMercaderia no
    depende de si el ticket pasó por Calidad o no, así que un solo camino
    basta para probarlo sin acoplar este test a esa otra máquina de estados.
    """

    def _finalizar_ticket(self, cantidad_real=None):
        """
        Construye un Ticket real hasta FINALIZADO. Si se pasa cantidad_real,
        se registra deliberadamente distinta a quantity_sap (fijo en 10 por
        _crear_cita_confirmada) para poder distinguir "cantidad real" de
        "cantidad SAP" en el test, en vez de asumirlo.
        """
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'CDL', requiere_coa=False)
        insp = TicketLineInspection.objects.get(ticket=ticket, etapa='ALMACEN')
        self.assertEqual(insp.cantidad_sap, Decimal('10.0000'))  # sanity del helper base

        if cantidad_real is None:
            resultados = self._resultados_para(ticket)
        else:
            resultados = [{
                'inspeccion_id': insp.id,
                'estado': 'CONFORME',
                'cantidad_modificada': str(cantidad_real),
            }]

        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=self.u_almacen, resultados=resultados,
        )
        OperationsService.registrar_salida(ticket_id=ticket.id, usuario_vigilancia=self.u_vigilancia)
        ticket.refresh_from_db()
        return ticket, insp.po_line

    def test_creacion_automatica_al_finalizar_ticket(self):
        """No existe EntradaMercaderia antes de FINALIZADO; existe justo después, con estado_sap='L'."""
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'CDL', requiere_coa=False)
        self.assertFalse(EntradaMercaderia.objects.filter(ticket=ticket).exists())

        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=self.u_almacen,
            resultados=self._resultados_para(ticket),
        )
        OperationsService.registrar_salida(ticket_id=ticket.id, usuario_vigilancia=self.u_vigilancia)

        entrada = EntradaMercaderia.objects.get(ticket=ticket)
        self.assertEqual(entrada.estado_sap, 'L')
        self.assertIsNotNone(entrada.fecha_generada)
        self.assertIsNone(entrada.doc_entry_borrador)
        self.assertIsNone(entrada.doc_entry_definitivo)
        self.assertTrue(entrada.lineas.exists())

    def test_idempotencia_no_duplica_registro_ni_lineas_en_reintento(self):
        """
        Reintentar el trigger sobre el mismo Ticket (simulando el caso
        defensivo — en la práctica registrar_salida ya está bloqueado por
        _validar_etapa, ETAPA_FINALIZADO no es ETAPA_VIGILANCIA_SALIDA) no
        crea un segundo registro ni duplica las líneas.
        """
        ticket, _ = self._finalizar_ticket()

        entrada_1 = EntradaMercaderia.objects.get(ticket=ticket)
        n_lineas_1 = entrada_1.lineas.count()
        self.assertEqual(EntradaMercaderia.objects.filter(ticket=ticket).count(), 1)

        entrada_2 = OperationsService._generar_entrada_mercaderia(ticket)

        self.assertEqual(entrada_1.id, entrada_2.id)
        self.assertEqual(EntradaMercaderia.objects.filter(ticket=ticket).count(), 1)
        self.assertEqual(EntradaMercaderiaLinea.objects.filter(entrada=entrada_1).count(), n_lineas_1)

    def test_cantidad_es_la_real_inspeccionada_no_la_de_sap(self):
        ticket, po_line = self._finalizar_ticket(cantidad_real='7.5000')
        entrada = EntradaMercaderia.objects.get(ticket=ticket)
        linea = entrada.lineas.get(po_line=po_line)

        self.assertEqual(linea.cantidad, Decimal('7.5000'))
        self.assertEqual(po_line.quantity_sap, Decimal('10.0000'))
        self.assertNotEqual(linea.cantidad, po_line.quantity_sap)

    def test_endpoint_ajax_registrar_salida_tambien_dispara_la_creacion(self):
        """Cubre el camino HTTP real (ajax_registrar_salida), no solo el service directo."""
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'CDL', requiere_coa=False)
        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=self.u_almacen,
            resultados=self._resultados_para(ticket),
        )
        self.client.force_login(self.u_vigilancia)
        resp = self.client.post(
            '/operations/api/registrar-salida/',
            data={'ticket_id': ticket.id}, content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(EntradaMercaderia.objects.filter(ticket=ticket, estado_sap='L').exists())


class EntradaMercaderiaAPITests(OperationsTestBase):
    """
    Endpoints del daemon (api/entrada_mercaderia_api.py) — mismo criterio de
    upsert retry-safe ya probado para el sync de OC (apps/sap_sync/tests.py,
    sesión 49): confirmar-borrador/confirmar-definitivo nunca fallan en un
    reintento, solo actualizan el valor.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.daemon_user = User.objects.create_user(username='daemon_test_em', password='x')
        cls.token = Token.objects.create(user=cls.daemon_user)

    def setUp(self):
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

    def _finalizar_ticket(self):
        ticket = self._crear_ticket_en_etapa(Ticket.ETAPA_ALMACEN, 'CDL', requiere_coa=False)
        OperationsService.registrar_calidad(
            ticket_id=ticket.id, usuario_calidad=self.u_almacen,
            resultados=self._resultados_para(ticket),
        )
        OperationsService.registrar_salida(ticket_id=ticket.id, usuario_vigilancia=self.u_vigilancia)
        return EntradaMercaderia.objects.get(ticket_id=ticket.id)

    def test_get_lista_solo_las_pendientes_L(self):
        entrada = self._finalizar_ticket()
        resp = self.api.get('/api/v1/entradas-pendientes/')
        self.assertEqual(resp.status_code, 200)
        ids = [row['id'] for row in resp.data]
        self.assertIn(entrada.id, ids)

        entrada.estado_sap = 'Y'
        entrada.save(update_fields=['estado_sap'])
        resp2 = self.api.get('/api/v1/entradas-pendientes/')
        ids2 = [row['id'] for row in resp2.data]
        self.assertNotIn(entrada.id, ids2, 'Una entrada ya en Y no debe listarse como pendiente.')

    def test_confirmar_borrador_marca_B_y_guarda_doc_entry(self):
        entrada = self._finalizar_ticket()
        resp = self.api.post(
            f'/api/v1/entradas-pendientes/{entrada.id}/confirmar-borrador/',
            {'doc_entry_borrador': 90001}, format='json',
        )
        self.assertEqual(resp.status_code, 200)
        entrada.refresh_from_db()
        self.assertEqual(entrada.estado_sap, 'B')
        self.assertEqual(entrada.doc_entry_borrador, 90001)
        self.assertIsNotNone(entrada.fecha_borrador_confirmado)

    def test_confirmar_borrador_es_idempotente_en_reintento(self):
        """Reintentar con el MISMO o distinto doc_entry no falla, solo actualiza."""
        entrada = self._finalizar_ticket()
        r1 = self.api.post(
            f'/api/v1/entradas-pendientes/{entrada.id}/confirmar-borrador/',
            {'doc_entry_borrador': 90002}, format='json',
        )
        r2 = self.api.post(
            f'/api/v1/entradas-pendientes/{entrada.id}/confirmar-borrador/',
            {'doc_entry_borrador': 90002}, format='json',
        )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        entrada.refresh_from_db()
        self.assertEqual(entrada.doc_entry_borrador, 90002)
        self.assertEqual(entrada.estado_sap, 'B')

    def test_confirmar_definitivo_marca_Y_y_guarda_doc_entry(self):
        entrada = self._finalizar_ticket()
        self.api.post(
            f'/api/v1/entradas-pendientes/{entrada.id}/confirmar-borrador/',
            {'doc_entry_borrador': 90003}, format='json',
        )
        resp = self.api.post(
            f'/api/v1/entradas-pendientes/{entrada.id}/confirmar-definitivo/',
            {'doc_entry_definitivo': 90004}, format='json',
        )
        self.assertEqual(resp.status_code, 200)
        entrada.refresh_from_db()
        self.assertEqual(entrada.estado_sap, 'Y')
        self.assertEqual(entrada.doc_entry_definitivo, 90004)
        self.assertIsNotNone(entrada.fecha_definitivo_confirmado)

    def test_confirmar_borrador_sin_doc_entry_devuelve_400(self):
        entrada = self._finalizar_ticket()
        resp = self.api.post(
            f'/api/v1/entradas-pendientes/{entrada.id}/confirmar-borrador/', {}, format='json',
        )
        self.assertEqual(resp.status_code, 400)
        entrada.refresh_from_db()
        self.assertEqual(entrada.estado_sap, 'L')

    def test_endpoints_exigen_token_del_daemon(self):
        entrada = self._finalizar_ticket()
        api_sin_token = APIClient()
        resp = api_sin_token.get('/api/v1/entradas-pendientes/')
        self.assertEqual(resp.status_code, 401)
