# apps/operations/services.py
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from collections import defaultdict
from .models import (
    Ticket, TicketStage, TicketLineInspection, TicketLineCOA, TicketDatosIngreso,
    EntradaMercaderia, EntradaMercaderiaLinea,
)


class TicketEtapaError(ValidationError):
    """
    Se lanza cuando una acción de OperationsService se ejecuta con el Ticket
    fuera de la etapa (Ticket.etapa_actual) que le corresponde.

    Hereda de ValidationError a propósito: los endpoints AJAX existentes ya
    capturan ValidationError y la devuelven como error JSON, así que este
    candado no requiere tocar las vistas (eso queda para la Fase 3, permisos).
    """
    pass


class OperationsService:

    # ─── Candado de la máquina de estados (Ticket.etapa_actual) ─────────────

    @staticmethod
    def _validar_etapa(ticket: Ticket, esperada: str) -> None:
        """
        Verifica que ticket.etapa_actual sea la esperada antes de ejecutar una
        acción de etapa. Si no corresponde, lanza TicketEtapaError sin escribir
        nada (debe llamarse antes de cualquier save()).
        """
        if ticket.etapa_actual != esperada:
            labels = dict(Ticket.ETAPA_ACTUAL_CHOICES)
            raise TicketEtapaError(
                f"El Ticket #{ticket.appointment_id} está en la etapa "
                f"'{labels.get(ticket.etapa_actual, ticket.etapa_actual)}'; "
                f"se esperaba '{labels.get(esperada, esperada)}' para ejecutar esta acción."
            )

    # ─── Autorización por grupo según la etapa activa (Fase 3; bifurcación
    #     Materia Prima Fase 3+4, sesión 30) ─────────────────────────────────

    @staticmethod
    def _grupo_actor_recepcion(ticket: Ticket) -> str:
        """
        Devuelve el grupo (ALMACEN o MATERIA_PRIMA) al que le corresponde
        ejecutar la recepción física de este ticket, según el tipo de OC
        vinculada (Ticket.es_materia_prima — ver análisis de la sesión 27).

        Determinístico: desde la Decisión 1 (sesión 28), el portal del
        proveedor bloquea combinar OCs Materia Prima con OCs comerciales en
        la misma solicitud, así que ya no existen tickets con OCs mixtas —
        basta con verificar si ALGUNA OC del ticket es tipo MP.

        Sesión 31: reutiliza Ticket.es_materia_prima (single source of
        verdad, ahora también consumida por las plantillas para las mismas
        preguntas de "tipo de OC") en vez de repetir la query aquí.
        """
        return 'MATERIA_PRIMA' if ticket.es_materia_prima else 'ALMACEN'

    @staticmethod
    def grupo_requerido_por_etapa(ticket: Ticket) -> str | None:
        """
        Devuelve el grupo (VIGILANCIA/ALMACEN/MATERIA_PRIMA/CALIDAD) al que
        le corresponde ejecutar la siguiente acción de etapa, según
        Ticket.etapa_actual. Es la contraparte "de rol" del candado de
        orden que ya aplica _validar_etapa (que valida la secuencia, no
        quién la ejecuta).

        Devuelve None si no queda ninguna acción de etapa pendiente (FINALIZADO).

        Sesión 37 — corrección de flujo (contradecía el diseño original de
        la Fase 4/sesión 30, no un cambio de comportamiento nuevo): ALMACEN
        ya NO bifurca según ticket.requiere_calidad — le corresponde
        SIEMPRE al actor de recepción (_grupo_actor_recepcion), sin
        importar requiere_calidad. Antes, con requiere_calidad=True, esta
        etapa saltaba directo a 'CALIDAD' apenas se completaba "Iniciar
        Recepción" — bloqueando por completo la propia inspección línea
        por línea del actor (CANT. REAL/ESTADO/OBSERVACIÓN), confirmado en
        prueba manual real con un ticket Materia Prima. requiere_calidad
        decide únicamente si, DESPUÉS de que el actor guarda su propia
        inspección, hay un paso ADICIONAL de Calidad (ETAPA_CALIDAD, ahora
        una etapa activa genuina con su propio turno) o se salta directo a
        VIGILANCIA_SALIDA — ver OperationsService.registrar_calidad, que
        aplica el mismo fork en el mismo punto (debe coincidir siempre con
        este mapa o el candado de permiso queda desalineado).

        Sesión 30 (rediseño Materia Prima, Fase 3+4): VIGILANCIA_INGRESO ya
        no es fijo a 'ALMACEN' — bifurca a ALMACEN o MATERIA_PRIMA según
        _grupo_actor_recepcion (el actor que ejecutará "Iniciar Recepción").
        """
        mapa = {
            Ticket.ETAPA_PENDIENTE_INGRESO:  'VIGILANCIA',   # siguiente: iniciar_ingreso_planta
            Ticket.ETAPA_VIGILANCIA_INGRESO: OperationsService._grupo_actor_recepcion(ticket),
                                                              # siguiente: autorizar_almacen ("Iniciar Recepción")
            Ticket.ETAPA_ALMACEN:            OperationsService._grupo_actor_recepcion(ticket),
                                                              # siguiente: registrar_calidad (paso 1, SIEMPRE el actor)
            Ticket.ETAPA_CALIDAD:            'CALIDAD',      # siguiente: registrar_calidad (paso 2, inspección ADICIONAL)
            Ticket.ETAPA_VIGILANCIA_SALIDA:  'VIGILANCIA',   # siguiente: registrar_salida (ambos caminos convergen aquí)
        }
        return mapa.get(ticket.etapa_actual)

    # ─── Etapa 1: Vigilancia — Entrada ──────────────────────────────────────
    @staticmethod
    @transaction.atomic
    def iniciar_ingreso_planta(ticket_id: int, usuario_vigilancia) -> Ticket:
        """
        Registra el ingreso a planta.

        Ya no depende de un esqueleto previo en TicketLineInspection (etapa='ALMACEN'):
        lee las líneas directamente de las OCs de la cita y valida el COA consultando
        TicketLineCOA. Esta es la primera vez que se crean filas de TicketLineInspection
        para este ticket (etapa='VIGILANCIA').

        Sesión 51: las 3 lecturas de líneas de este método (construcción de
        `lineas`, validación de COA obligatorio, creación de inspecciones)
        excluyen líneas activa=False — una línea cancelada por SAP tras
        confirmarse la cita no debe exigir COA ni generar inspección, como
        si siguiera pendiente de recibir.
        """
        try:
            ticket = Ticket.objects.select_related('appointment').get(
                id=ticket_id,
                estado='PROGRAMADO'
            )
        except Ticket.DoesNotExist:
            raise ValidationError(f"No se encontró el Ticket #{ticket_id} PROGRAMADO.")

        # Candado de etapa: esta acción solo puede ejecutarse en PENDIENTE_INGRESO
        OperationsService._validar_etapa(ticket, Ticket.ETAPA_PENDIENTE_INGRESO)

        appointment = ticket.appointment

        # 1. Traemos las líneas de OC directamente desde la cita (SAP), no desde
        #    un esqueleto de TicketLineInspection.
        ocs = list(appointment.purchase_orders.prefetch_related('lines').all())
        lineas = [line for po in ocs for line in po.lines.filter(activa=True)]

        if not lineas:
            raise ValidationError("La cita no tiene líneas de OC vinculadas.")

        # 2. Mapa de COAs ya cargados por el proveedor (TicketLineCOA, no TicketLineInspection)
        coas_por_linea = {
            coa.po_line_id: coa.coa_url
            for coa in TicketLineCOA.objects.filter(ticket=ticket)
        }

        # 3. Validación de COA obligatorio: TODAS las líneas marcadas con
        #    requiere_coa=True (el flag que Compras fija por línea vía
        #    "Configurar COA", independiente de tipo_flujo/MP-comercial)
        #    deben tener su TicketLineCOA cargado. Antes solo se exigía
        #    cuando tipo_flujo == 'CON_CALIDAD', y bastaba que UNA sola
        #    línea tuviera el COA para pasar (any(...)) — un ticket con 3
        #    líneas requiere_coa=True y solo 1 cargada pasaba igual, y un
        #    ticket SOLO_ALMACEN con líneas requiere_coa=True nunca se
        #    validaba. Bug crítico confirmado: la validación nunca fue
        #    realmente bloqueante, solo una revisión visual (ver bloque
        #    "Estado de Certificados (COA)" en detalle_ticket.html).
        lineas_faltantes = [
            (po, line)
            for po in ocs
            for line in po.lines.filter(activa=True)
            if line.requiere_coa and not coas_por_linea.get(line.id)
        ]
        # El PDF legacy a nivel de cita (appointment.coa_pdf, previo a la
        # carga por línea vía OneDrive — Fase 1/3) sigue aceptándose como
        # respaldo global si existe, mismo criterio ya vigente antes.
        if lineas_faltantes and not appointment.coa_pdf:
            detalle = ", ".join(
                f"{line.item_code} (OC {po.doc_num})" for po, line in lineas_faltantes
            )
            raise ValidationError(
                "No se puede autorizar el ingreso a planta: falta el Certificado "
                f"de Análisis (COA) de la(s) línea(s): {detalle}."
            )

        # 4. Actualizamos el Ticket y avanzamos la etapa (candado de servicio)
        ticket.estado = 'EN_PLANTA'
        ticket.etapa_actual = Ticket.ETAPA_VIGILANCIA_INGRESO
        ticket.save(update_fields=['estado', 'etapa_actual'])

        # 5. Marcamos la etapa en el historial
        TicketStage.objects.get_or_create(
            ticket=ticket,
            etapa='VIGILANCIA_ENTRADA',
            defaults={'usuario': usuario_vigilancia}
        )

        # 6. CREACIÓN de líneas de inspección para VIGILANCIA (primera vez que existen)
        for po in ocs:
            for line in po.lines.filter(activa=True):
                # Sincronizamos el documento solo si la línea lo requiere
                url_documento = coas_por_linea.get(line.id) if line.requiere_coa else None

                TicketLineInspection.objects.update_or_create(
                    ticket=ticket,
                    po_line=line,
                    etapa='VIGILANCIA',
                    defaults={
                        'doc_num': po.doc_num,
                        'usuario': usuario_vigilancia,
                        'cantidad_sap': line.quantity_sap,
                        'cantidad_modificada': line.quantity_sap,
                        'estado': 'EN_PLANTA',
                        'requiere_coa': line.requiere_coa,
                        'coa_url': url_documento,
                        'comentario': f"Ingreso autorizado - Flujo {ticket.tipo_flujo}"
                    }
                )

        return ticket

    # ─── Etapa 2: Recepción ("Iniciar Recepción") — ALMACEN o MATERIA_PRIMA ──

    @staticmethod
    @transaction.atomic
    def autorizar_almacen(ticket_id: int, usuario, muelle: str = '',
                           requiere_calidad: bool | None = None,
                           confirmado: bool = False) -> TicketStage:
        """
        "Iniciar Recepción": ejecutado por ALMACEN o MATERIA_PRIMA, según a
        cuál de los 2 le corresponde el ticket (_grupo_actor_recepcion, según
        el tipo de OC). El nombre del método se mantiene (no se renombra a
        "iniciar_recepcion") por compatibilidad — el endpoint AJAX que lo
        expone sigue siendo el mismo que ya usa el botón "Iniciar Recepción"
        de la UI (que la Fase 5 actualizará para ambos actores; hasta
        entonces, este método sigue aceptando la llamada tal como la hace
        hoy el botón de Almacén, sin romper nada).

        Cierra VIGILANCIA_ENTRADA y abre ALMACEN_RECEPCION. Comportamiento
        de conteo/COA idéntico sin importar el actor — la diferencia real
        del flujo (si pasa por Calidad o no) ahora depende de
        requiere_calidad, capturado aquí mismo (Fase 1, sesión 27), no de
        tipo_flujo.

        Parámetros nuevos (sesión 30):
          muelle           : puerta de descarga real — se guarda en
                              Ticket.muelle (Fase 1, sesión 27), YA NO en
                              AppointmentSlot.dock (decisión explícita de
                              diseño: el muelle es un dato propio del
                              Ticket, no del horario compartido).
          requiere_calidad : decide si, al cerrar esta etapa
                              (registrar_calidad), el ticket pasa a CALIDAD
                              o salta directo a VIGILANCIA_SALIDA. Si no se
                              especifica (None), toma el default de
                              negocio: marcado si el actor es MATERIA_PRIMA,
                              desmarcado si es ALMACEN — el llamador puede
                              sobreescribirlo explícitamente.
          confirmado       : para MATERIA_PRIMA es obligatorio pasar
                              confirmado=True o se rechaza con
                              ValidationError, sin escribir nada — el actor
                              debe confirmar conscientemente antes de
                              arrancar el proceso (decisión de negocio,
                              sesión 30). ALMACEN no tiene este paso.
        """
        ticket = OperationsService._get_ticket_en_planta(ticket_id)

        # Candado de etapa: solo puede recibirse tras el ingreso de Vigilancia
        OperationsService._validar_etapa(ticket, Ticket.ETAPA_VIGILANCIA_INGRESO)

        actor = OperationsService._grupo_actor_recepcion(ticket)
        es_materia_prima_actor = (actor == 'MATERIA_PRIMA')

        if requiere_calidad is None:
            requiere_calidad = es_materia_prima_actor

        if es_materia_prima_actor and not confirmado:
            raise ValidationError(
                "Materia Prima debe confirmar explícitamente antes de iniciar la recepción."
            )

        OperationsService._cerrar_etapa(ticket, 'VIGILANCIA_ENTRADA', usuario)

        stage, _ = TicketStage.objects.get_or_create(
            ticket=ticket,
            etapa='ALMACEN_RECEPCION',
            defaults={'usuario': usuario}
        )

        if muelle:
            ticket.muelle = muelle

        # Inicializar líneas de inspección para etapa ALMACEN. El valor de
        # 'etapa' se mantiene fijo en 'ALMACEN' sin importar si el actor es
        # ALMACEN o MATERIA_PRIMA (recomendación confirmada del análisis de
        # la sesión 27): representa la etapa del flujo de recepción, no el
        # grupo Django que la ejecutó — evita tocar TicketLineInspection.
        # ETAPA_CHOICES y todos sus consumidores (get_grouped_by_oc,
        # registrar_calidad, ajax_registrar_inspeccion, etc.).
        # NOTA: ya no existe un esqueleto previo (se eliminó de confirmar_cita), así
        # que este get_or_create es ahora el que realmente CREA estas filas por
        # primera vez — por eso debe incluir 'doc_num' (campo obligatorio del modelo).
        # activa=True (sesión 51): no crear inspección de recepción para una
        # línea que SAP ya canceló — no hay nada físico que recibir.
        appointment = ticket.appointment
        for po in appointment.purchase_orders.prefetch_related('lines').all():
            for line in po.lines.filter(activa=True):
                TicketLineInspection.objects.get_or_create(
                    ticket=ticket,
                    po_line=line,
                    etapa='ALMACEN',
                    defaults={
                        'doc_num': po.doc_num,
                        'usuario': usuario,
                        'cantidad_sap': line.quantity_sap,
                        'cantidad_modificada': line.quantity_sap,
                        'estado': 'PENDIENTE',#'EN_PROCESO',
                        'requiere_coa': line.requiere_coa,
                    }
                )

        # Avanzamos la etapa (candado de servicio) y guardamos los 2 datos
        # nuevos capturados en este paso.
        ticket.requiere_calidad = requiere_calidad
        ticket.etapa_actual = Ticket.ETAPA_ALMACEN
        ticket.save(update_fields=['muelle', 'requiere_calidad', 'etapa_actual'])

        return stage

# ─── Etapa 3: Inspección (2 pasos genuinamente separados desde la sesión 37) ───
    @staticmethod
    @transaction.atomic
    def registrar_calidad(ticket_id: int, usuario_calidad,
                          resultados: list[dict]) -> TicketStage:
        """
        Registra una inspección por línea de OC — despacha según la etapa
        activa del ticket (mismo nombre/firma de siempre — el parámetro
        sigue llamándose usuario_calidad porque ajax_registrar_inspeccion
        ya lo invoca así; el nombre "registrar_calidad" se mantiene por
        compatibilidad aunque hoy también cubre el paso del actor de
        recepción).

        Corrección de flujo (sesión 37): contradecía el diseño original de
        la Fase 4/sesión 30, no un cambio de comportamiento nuevo. Antes,
        este método conflaba 2 pasos en 1 solo: si requiere_calidad=True,
        escribía directo una fila etapa='CALIDAD' con los datos de quien
        llamara — y como grupo_requerido_por_etapa ya saltaba a 'CALIDAD'
        apenas el ticket llegaba a ALMACEN, el actor de recepción (ALMACEN/
        MATERIA_PRIMA) NUNCA llegaba a registrar su propia inspección
        (CANT. REAL/ESTADO/OBSERVACIÓN) — bug confirmado en prueba manual
        real con un ticket Materia Prima. Ahora son 2 pasos separados:

          1. ETAPA_ALMACEN -> el actor de recepción registra SU PROPIA
             inspección línea por línea, SIEMPRE, sin importar
             requiere_calidad. Escribe/actualiza las filas etapa='ALMACEN'
             in situ (_registrar_inspeccion_recepcion). Avanza a
             ETAPA_CALIDAD si requiere_calidad=True (Calidad tiene una
             inspección ADICIONAL pendiente), o directo a
             ETAPA_VIGILANCIA_SALIDA si no.

          2. ETAPA_CALIDAD (solo alcanzable si requiere_calidad=True) ->
             Calidad registra SU PROPIA inspección adicional e
             independiente, en filas NUEVAS etapa='CALIDAD'
             (_registrar_inspeccion_calidad) — no sobreescribe ni depende
             de los valores que el actor ya guardó en el paso 1 (etapa=
             'ALMACEN', que queda intacto como registro de lo recibido
             físicamente). Avanza a ETAPA_VIGILANCIA_SALIDA.

        Cualquier otro etapa_actual (el ticket no llegó todavía a ALMACEN)
        cae en el paso 1 por defecto, que rechaza con TicketEtapaError vía
        _validar_etapa — mismo mensaje de siempre.
        """
        ticket = OperationsService._get_ticket_en_planta(ticket_id)

        if ticket.etapa_actual == Ticket.ETAPA_CALIDAD:
            return OperationsService._registrar_inspeccion_calidad(ticket, usuario_calidad, resultados)
        return OperationsService._registrar_inspeccion_recepcion(ticket, usuario_calidad, resultados)

    @staticmethod
    def _registrar_inspeccion_recepcion(ticket: Ticket, usuario,
                                         resultados: list[dict]) -> TicketStage:
        """Paso 1 de registrar_calidad — ver su docstring."""
        # Candado de etapa: el actor de recepción actúa siempre desde ALMACEN,
        # sin importar requiere_calidad.
        OperationsService._validar_etapa(ticket, Ticket.ETAPA_ALMACEN)

        OperationsService._cerrar_etapa(ticket, 'ALMACEN_RECEPCION', usuario)

        etapa_historial = 'CALIDAD_INSPECCION' if ticket.requiere_calidad else 'ALMACEN_FINALIZADO'
        stage, _ = TicketStage.objects.get_or_create(
            ticket=ticket,
            etapa=etapa_historial,
            defaults={'usuario': usuario}
        )

        for resultado in resultados:
            try:
                # Fila base generada al "Iniciar Recepción" (etapa='VIGILANCIA'
                # ya se cerró; esta es la fila etapa='ALMACEN' que el propio
                # actor va a completar ahora con sus valores reales).
                insp_base = TicketLineInspection.objects.get(
                    id=resultado.get('inspeccion_id'),
                    ticket=ticket,
                    etapa='ALMACEN'
                )
            except TicketLineInspection.DoesNotExist:
                continue

            TicketLineInspection.objects.update_or_create(
                ticket=ticket,
                po_line=insp_base.po_line,
                etapa='ALMACEN',  # SIEMPRE en situ — la propia inspección del actor
                defaults={
                    'doc_num': insp_base.doc_num,
                    'usuario': usuario,
                    'cantidad_sap': insp_base.cantidad_sap,
                    'cantidad_modificada': resultado.get(
                        'cantidad_modificada', insp_base.cantidad_modificada
                    ),
                    'estado': resultado.get('estado', 'CONFORME'),
                    'comentario': resultado.get('comentario', ''),
                    'requiere_coa': insp_base.requiere_coa,
                    'coa_url': resultado.get('coa_url') or insp_base.coa_url or '',
                }
            )

        ticket.etapa_actual = (
            Ticket.ETAPA_CALIDAD if ticket.requiere_calidad else Ticket.ETAPA_VIGILANCIA_SALIDA
        )
        ticket.save(update_fields=['etapa_actual'])
        return stage

    @staticmethod
    def _registrar_inspeccion_calidad(ticket: Ticket, usuario,
                                       resultados: list[dict]) -> TicketStage:
        """Paso 2 de registrar_calidad — ver su docstring. Solo alcanzable si requiere_calidad=True."""
        OperationsService._validar_etapa(ticket, Ticket.ETAPA_CALIDAD)

        stage = TicketStage.objects.filter(
            ticket=ticket, etapa='CALIDAD_INSPECCION'
        ).order_by('-fecha_inicio').first()
        OperationsService._cerrar_etapa(ticket, 'CALIDAD_INSPECCION', usuario)

        for resultado in resultados:
            try:
                # Fila del actor de recepción (paso 1) — sirve de referencia
                # (cantidad_sap) pero Calidad escribe SUS PROPIOS valores en
                # una fila nueva, nunca sobreescribe esta.
                insp_almacen = TicketLineInspection.objects.get(
                    id=resultado.get('inspeccion_id'),
                    ticket=ticket,
                    etapa='ALMACEN'
                )
            except TicketLineInspection.DoesNotExist:
                continue

            TicketLineInspection.objects.update_or_create(
                ticket=ticket,
                po_line=insp_almacen.po_line,
                etapa='CALIDAD',  # Fila NUEVA, independiente de la del actor
                defaults={
                    'doc_num': insp_almacen.doc_num,
                    'usuario': usuario,
                    'cantidad_sap': insp_almacen.cantidad_sap,
                    'cantidad_modificada': resultado.get(
                        'cantidad_modificada', insp_almacen.cantidad_modificada
                    ),
                    'estado': resultado.get('estado', 'CONFORME'),
                    'comentario': resultado.get('comentario', ''),
                    'requiere_coa': insp_almacen.requiere_coa,
                    'coa_url': resultado.get('coa_url') or insp_almacen.coa_url or '',
                }
            )

        ticket.etapa_actual = Ticket.ETAPA_VIGILANCIA_SALIDA
        ticket.save(update_fields=['etapa_actual'])

        return stage


    # ─── Etapa 4: Vigilancia — Salida (AJUSTE DE ETIQUETAS) ──────────────────

    @staticmethod
    @transaction.atomic
    def registrar_salida(ticket_id: int, usuario_vigilancia) -> Ticket:
        """
        Vigilancia registra la salida física del proveedor.
        Mantenemos la estructura original pero sincronizamos etiquetas de cierre.
        """
        ticket = OperationsService._get_ticket_en_planta(ticket_id)

        # Candado de etapa: desde la sesión 37, ambos caminos (requiere_calidad
        # True o False) convergen en ETAPA_VIGILANCIA_SALIDA ANTES de que
        # Vigilancia actúe — la inspección adicional de Calidad (paso 2 de
        # registrar_calidad) ya avanza ahí por sí misma, en vez de dejar el
        # ticket en ETAPA_CALIDAD hasta que Vigilancia lo cierre. Antes
        # dependía de requiere_calidad (cerraba desde CALIDAD) porque el
        # propio registrar_salida era quien determinaba si la inspección de
        # Calidad ya había ocurrido — ya no hace falta esa distinción aquí.
        OperationsService._validar_etapa(ticket, Ticket.ETAPA_VIGILANCIA_SALIDA)

        ahora = timezone.now()

        # AJUSTE QUIRÚRGICO: Sincronización de etiquetas para evitar etapas huérfanas
        if ticket.requiere_calidad:
            # Cerramos la etapa abierta por el analista de Calidad
            OperationsService._cerrar_etapa(ticket, 'CALIDAD_INSPECCION', usuario_vigilancia)
        else:
            # requiere_calidad=False: Cerramos la etapa abierta por el actor
            # de recepción (ALMACEN o MATERIA_PRIMA, registrar_calidad modificado)
            # Reemplazamos 'ALMACEN_RECEPCION' por 'ALMACEN_FINALIZADO'
            OperationsService._cerrar_etapa(ticket, 'ALMACEN_FINALIZADO', usuario_vigilancia)

        # Crear y cerrar la etapa de salida (registro puntual)
        stage_salida, _ = TicketStage.objects.get_or_create(
            ticket=ticket,
            etapa='VIGILANCIA_SALIDA',
            defaults={'usuario': usuario_vigilancia}
        )
        if not stage_salida.fecha_fin:
            stage_salida.fecha_fin = ahora
            stage_salida.save(update_fields=['fecha_fin'])

        # Calcular tiempo total en planta (Correlación con VIGILANCIA_ENTRADA)
        etapa_entrada = ticket.stages.filter(etapa='VIGILANCIA_ENTRADA').first()
        if etapa_entrada:
            ticket.tiempo_total_planta = ahora - etapa_entrada.fecha_inicio

        # Finalizar Ticket y Appointment; avanzamos la etapa (candado de servicio)
        ticket.estado = 'FINALIZADO'
        ticket.etapa_actual = Ticket.ETAPA_FINALIZADO
        ticket.save(update_fields=['estado', 'tiempo_total_planta', 'etapa_actual'])

        appointment = ticket.appointment
        appointment.status = 'FINALIZADA'
        appointment.save(update_fields=['status'])

        OperationsService._generar_entrada_mercaderia(ticket)

        return ticket

    @staticmethod
    def _generar_entrada_mercaderia(ticket: Ticket) -> EntradaMercaderia:
        """
        Crea automáticamente la EntradaMercaderia (borrador de Entrada de
        Mercadería para SAP) al finalizar el Ticket — mismo trigger que
        registrar_salida usa para cerrar el ciclo (sesión 57).

        Idempotente por diseño (get_or_create por ticket, update_or_create
        por línea): un reintento de este método sobre el mismo Ticket no
        duplica el registro ni sus líneas. En la práctica, registrar_salida
        ya está protegido por _validar_etapa (el ticket deja de estar en
        ETAPA_VIGILANCIA_SALIDA en cuanto se ejecuta una vez), así que no
        debería reintentarse solo — la idempotencia es una capa de
        seguridad adicional, no la única barrera.

        La cantidad de cada línea es la fila MÁS RECIENTE de
        TicketLineInspection por po_line, entre TODAS las etapas ya
        ejecutadas (mismo criterio exacto que OperationsService.
        get_estado_actual_por_oc, sesión 6/9 — "cantidad real final",
        nunca PurchaseOrderLine.quantity_sap ni TicketLineInspection.
        cantidad_sap).
        """
        entrada, created = EntradaMercaderia.objects.get_or_create(
            ticket=ticket, defaults={'estado_sap': 'L'},
        )
        if not created:
            return entrada

        inspecciones = TicketLineInspection.objects.filter(
            ticket=ticket
        ).order_by('po_line_id', '-fecha_registro')

        mas_reciente_por_linea = {}
        for insp in inspecciones:
            mas_reciente_por_linea.setdefault(insp.po_line_id, insp)

        for po_line_id, insp in mas_reciente_por_linea.items():
            EntradaMercaderiaLinea.objects.update_or_create(
                entrada=entrada, po_line_id=po_line_id,
                defaults={'cantidad': insp.cantidad_modificada},
            )

        return entrada

    # ─── Gestión de COA / OneDrive (Fase 3: Proveedor sube archivos) ─────────

    @staticmethod
    @transaction.atomic
    def registrar_coa_proveedor(ticket_id: int, po_line_id: int,
                                 coa_url: str, usuario) -> TicketLineCOA:
        """
        Registra el link de OneDrive del COA para una línea específica.
        Invocado cuando el proveedor carga el archivo en el portal (Fase 3).

        Guarda el link en TicketLineCOA — independiente de las etapas operativas.
        Ya NO toca TicketLineInspection en absoluto (evita que la carga de COA del
        proveedor sobreescriba filas usadas después por Almacén/Calidad/Vigilancia).

        Parámetros:
            ticket_id  : ID del Ticket
            po_line_id : ID de PurchaseOrderLine
            coa_url    : URL de acceso al archivo en OneDrive M365
            usuario    : User que registra (proveedor)
        """
        try:
            ticket = Ticket.objects.get(id=ticket_id)
        except Ticket.DoesNotExist:
            raise ValidationError(f"No existe el Ticket #{ticket_id}.")

        from apps.sap_sync.models import PurchaseOrderLine
        try:
            po_line = PurchaseOrderLine.objects.get(id=po_line_id)
        except PurchaseOrderLine.DoesNotExist:
            raise ValidationError(f"No existe la línea de OC #{po_line_id}.")

        coa, _ = TicketLineCOA.objects.update_or_create(
            ticket=ticket,
            po_line=po_line,
            defaults={
                'coa_url': coa_url,
                'subido_por': usuario,
            }
        )
        return coa

    # ─── Datos de Ingreso: vehículo/conductor (sesión 13) ────────────────────

    @staticmethod
    @transaction.atomic
    def registrar_datos_ingreso(ticket_id: int, placa_vehiculo: str,
                                 conductor_dni: str, conductor_nombre: str,
                                 usuario) -> TicketDatosIngreso:
        """
        Registra/actualiza la placa del vehículo y los datos del conductor
        para la visita del Ticket. Invocado cuando el proveedor completa el
        formulario en el portal, en cualquier momento mientras el ticket
        sigue PROGRAMADO (ese guard vive en la vista AJAX, no aquí — mismo
        criterio que registrar_coa_proveedor).

        Un solo registro por Ticket (no por línea, a diferencia del COA):
        update_or_create permite que el proveedor lo corrija varias veces
        antes del ingreso sin crear filas duplicadas. No toca etapa_actual
        ni ninguna otra lógica operativa — es informativo, no bloqueante
        (decisión de negocio explícita, sesión 13): Vigilancia lo consulta
        pero el sistema no impide autorizar el ingreso si falta o está
        incompleto.

        Parámetros:
            ticket_id        : ID del Ticket
            placa_vehiculo   : Placa del vehículo (puede venir vacía)
            conductor_dni    : DNI o Carné de Extranjería del conductor (puede venir vacío)
            conductor_nombre : Nombre y apellidos del conductor (puede venir vacío)
            usuario          : User que registra (proveedor)
        """
        try:
            ticket = Ticket.objects.get(id=ticket_id)
        except Ticket.DoesNotExist:
            raise ValidationError(f"No existe el Ticket #{ticket_id}.")

        datos, _ = TicketDatosIngreso.objects.update_or_create(
            ticket=ticket,
            defaults={
                'placa_vehiculo': placa_vehiculo.strip(),
                'conductor_dni': conductor_dni.strip(),
                'conductor_nombre': conductor_nombre.strip(),
                'actualizado_por': usuario,
            }
        )
        return datos

    # ─── Consultas de soporte para COA (independientes de las etapas) ────────

    @staticmethod
    def calcular_coa_completo(ticket) -> bool:
        """
        True si el ticket no requiere COA (SOLO_ALMACEN, o CON_CALIDAD sin líneas
        que lo exijan), o si todas las líneas que lo requieren ya tienen su
        TicketLineCOA cargado (o existe el PDF general legacy de la cita).

        Usado tanto por el Kanban de Vigilancia (panel_vigilancia) como por el
        detalle del ticket (detalle_ticket), para no duplicar la lógica.

        Sesión 51: excluye líneas activa=False del cálculo — una línea
        cancelada por SAP con requiere_coa=True residual ya no cuenta como
        pendiente, para no mostrar "Faltan COAs" por algo que ya no existe.
        """
        if ticket.tipo_flujo != 'CON_CALIDAD':
            return True

        appointment = ticket.appointment
        lineas_requeridas = [
            line
            for po in appointment.purchase_orders.all()
            for line in po.lines.filter(activa=True)
            if line.requiere_coa
        ]

        if not lineas_requeridas:
            return bool(appointment.coa_pdf)

        ids_requeridas = {line.id for line in lineas_requeridas}
        ids_con_coa = set(
            TicketLineCOA.objects.filter(
                ticket=ticket, po_line_id__in=ids_requeridas
            ).values_list('po_line_id', flat=True)
        )
        return ids_requeridas.issubset(ids_con_coa)

    # ─── Vistas de inspección: "mi sesión" (editable) vs "estado actual" (solo lectura) ───

    @staticmethod
    def get_mi_sesion(ticket: Ticket) -> dict:
        """
        Filas de TicketLineInspection editables en la etapa activa del ticket
        (Ticket.etapa_actual).

        Sesión 37 (corrección de flujo): ahora son 2 etapas activas con
        formulario editable por línea, no solo ALMACEN —
          - ALMACEN: paso 1, el actor de recepción (ALMACEN/MATERIA_PRIMA)
            registra su propia inspección.
          - CALIDAD: paso 2 (solo si requiere_calidad=True), la inspección
            ADICIONAL de Calidad.
        Ambas leen las MISMAS filas base etapa='ALMACEN' — en el paso de
        Calidad ya contienen los valores reales que el actor de recepción
        registró en su propio paso (no los defaults de SAP); cada paso
        guarda en su propia etapa (ver OperationsService.registrar_calidad
        y sus 2 sub-métodos). El resto de etapas activas (PENDIENTE_INGRESO,
        VIGILANCIA_INGRESO, VIGILANCIA_SALIDA) avanzan con una acción de un
        solo clic sin edición por línea, así que no tienen "mi sesión"
        propia — se devuelve {} en esos casos.
        """
        if ticket.etapa_actual not in (Ticket.ETAPA_ALMACEN, Ticket.ETAPA_CALIDAD):
            return {}
        return OperationsService.get_grouped_by_oc(ticket.id, etapa='ALMACEN')

    @staticmethod
    def get_estado_actual_por_oc(ticket_id: int) -> dict:
        """
        Agrupa por OC la fila MÁS RECIENTE (por fecha_registro) de
        TicketLineInspection por línea, considerando TODAS las etapas ya
        ejecutadas (VIGILANCIA/ALMACEN/CALIDAD) — no solo ALMACEN. Siempre
        de solo lectura.

        Corrige el bug de visibilidad diagnosticado: detalle_ticket.html
        solo leía la etapa ALMACEN (el registro base, previo a Calidad), así
        que un RECHAZADO o una cantidad ajustada por Calidad (etapa='CALIDAD',
        fila aparte por diseño — ver registrar_calidad) quedaba guardado en
        BD pero nunca se veía en la UI. Esta consulta siempre toma la fila
        con fecha_registro más reciente por línea, sea cual sea su etapa.

        El campo coa_url se lee de TicketLineCOA (fuente única desde la Fase 1),
        NO de TicketLineInspection.coa_url: ese campo es solo una foto tomada
        una vez, al momento del ingreso (ver iniciar_ingreso_planta), que nunca
        se sincroniza en las filas posteriores de ALMACEN/CALIDAD (quedan en
        None/''). Como esta función toma la fila más reciente por línea, una
        vez que Calidad actúa, esa fila más reciente casi siempre tiene el COA
        vacío aunque el proveedor sí lo haya cargado — bug confirmado con
        datos reales (Ticket #11, línea MP00000041).
        """
        inspecciones = TicketLineInspection.objects.filter(
            ticket_id=ticket_id
        ).select_related('po_line__purchase_order').order_by('po_line_id', '-fecha_registro')

        mas_reciente_por_linea = {}
        for insp in inspecciones:
            mas_reciente_por_linea.setdefault(insp.po_line_id, insp)

        coas_cargados = {
            coa.po_line_id: coa.coa_url
            for coa in TicketLineCOA.objects.filter(ticket_id=ticket_id)
        }

        grouped = defaultdict(lambda: {
            'oc_num': None,
            'card_name': '',
            'lineas': []
        })

        for insp in mas_reciente_por_linea.values():
            oc = insp.po_line.purchase_order
            oc_num = oc.doc_num
            grouped[oc_num]['oc_num'] = oc_num
            grouped[oc_num]['card_name'] = oc.card_name
            grouped[oc_num]['lineas'].append({
                'id': insp.id,
                'item_code': insp.po_line.item_code,
                'descripcion': insp.po_line.description,
                'cantidad_sap': insp.cantidad_sap,
                'cantidad_modificada': insp.cantidad_modificada,
                'estado': insp.estado,
                'requiere_coa': insp.requiere_coa,
                'coa_url': coas_cargados.get(insp.po_line_id, ''),
                'comentario': insp.comentario or '',
                'etapa_display': insp.get_etapa_display(),
            })

        return dict(grouped)

    @staticmethod
    def get_coa_status_por_oc(ticket) -> dict:
        """
        Agrupa por OC el estado del COA por línea, leyendo TicketLineCOA.
        Usado en el detalle del ticket para la revisión pre-ingreso de Vigilancia
        (pantalla "Estado de Certificados (COA)" antes de autorizar el ingreso).

        Sesión 51: excluye líneas activa=False, mismo criterio que
        calcular_coa_completo — una línea cancelada por SAP no debe listarse
        como pendiente de COA en esta tabla.
        """
        appointment = ticket.appointment
        coas_cargados = {
            coa.po_line_id: coa.coa_url
            for coa in TicketLineCOA.objects.filter(ticket=ticket)
        }

        grouped = defaultdict(lambda: {
            'oc_num': None,
            'card_name': '',
            'lineas': []
        })

        for po in appointment.purchase_orders.prefetch_related('lines').all():
            oc_num = po.doc_num
            grouped[oc_num]['oc_num'] = oc_num
            grouped[oc_num]['card_name'] = po.card_name

            for line in po.lines.filter(activa=True):
                grouped[oc_num]['lineas'].append({
                    'item_code': line.item_code,
                    'descripcion': line.description,
                    'requiere_coa': line.requiere_coa,
                    'coa_url': coas_cargados.get(line.id, ''),
                })

        return dict(grouped)

    # ─── Consultas de soporte ─────────────────────────────────────────────────

    @staticmethod
    def get_grouped_by_oc(ticket_id: int, etapa: str = 'ALMACEN'):
        """
        Agrupa las inspecciones de un ticket por Número de OC (alimenta
        get_mi_sesion → pestaña "Mi Sesión", editable por Almacén/Calidad).

        El COA por línea se lee de TicketLineCOA (fuente única desde la
        Fase 1), cruzado por po_line_id — mismo patrón ya usado en
        get_estado_actual_por_oc y get_coa_status_por_oc. Corregido en la
        sesión 24: antes leía TicketLineInspection.coa_url (que
        autorizar_almacen nunca puebla en la fila etapa='ALMACEN', queda
        siempre None) con un fallback frágil a la fila etapa='VIGILANCIA'
        cruzada por item_code+doc_num en vez de por po_line_id — mostraba
        "Falta" para líneas con COA real ya cargado (bug confirmado con
        datos reales, Ticket #12).
        """
        inspecciones = TicketLineInspection.objects.filter(
            ticket_id=ticket_id,
            etapa=etapa
        ).select_related('po_line__purchase_order')

        coas_cargados = {
            coa.po_line_id: coa.coa_url
            for coa in TicketLineCOA.objects.filter(ticket_id=ticket_id)
        }

        # Usamos defaultdict para agrupar de forma eficiente O(n)
        grouped = defaultdict(lambda: {
            'oc_num': None,
            'card_name': '',
            'es_materia_prima': False,
            'lineas': []
        })

        for insp in inspecciones:
            oc = insp.po_line.purchase_order
            oc_num = oc.doc_num

            if not grouped[oc_num]['oc_num']:
                grouped[oc_num]['oc_num'] = oc_num
                grouped[oc_num]['card_name'] = oc.card_name
                grouped[oc_num]['es_materia_prima'] = oc.es_materia_prima

            grouped[oc_num]['lineas'].append({
                'id': insp.id,
                'item_code': insp.po_line.item_code,
                'descripcion': insp.po_line.description,
                'cantidad_sap': insp.cantidad_sap,
                'cantidad_modificada': insp.cantidad_modificada,
                'estado': insp.estado,
                'requiere_coa': insp.requiere_coa,
                'coa_url': coas_cargados.get(insp.po_line_id, ''),
                'comentario': insp.comentario or '',
            })

        return dict(grouped)

    # ─── Helpers internos ────────────────────────────────────────────────────

    @staticmethod
    def _get_ticket_en_planta(ticket_id: int) -> Ticket:
        """Obtiene un Ticket en estado EN_PLANTA. Lanza ValidationError si no existe."""
        try:
            return Ticket.objects.select_related('appointment__slot').get(
                id=ticket_id,
                estado='EN_PLANTA'
            )
        except Ticket.DoesNotExist:
            raise ValidationError(
                f"El Ticket #{ticket_id} no está en estado EN_PLANTA o no existe. "
                "Solo se pueden procesar tickets con el proveedor ya en planta."
            )

    @staticmethod
    def _cerrar_etapa(ticket: Ticket, etapa: str, usuario) -> None:
        """
        Estampa fecha_fin en una TicketStage si aún no tiene.
        No lanza error si la etapa no existe (flujo flexible ante reintentos).
        """
        TicketStage.objects.filter(
            ticket=ticket,
            etapa=etapa,
            fecha_fin__isnull=True
        ).update(fecha_fin=timezone.now())
