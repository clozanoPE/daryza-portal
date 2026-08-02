# apps/operations/services.py
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from collections import defaultdict
from .models import Ticket, TicketStage, TicketLineInspection, TicketLineCOA, TicketDatosIngreso


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
                f"El Ticket #{ticket.id} está en la etapa "
                f"'{labels.get(ticket.etapa_actual, ticket.etapa_actual)}'; "
                f"se esperaba '{labels.get(esperada, esperada)}' para ejecutar esta acción."
            )

    # ─── Autorización por grupo según la etapa activa (Fase 3) ──────────────

    @staticmethod
    def grupo_requerido_por_etapa(ticket: Ticket) -> str | None:
        """
        Devuelve el grupo (VIGILANCIA/ALMACEN/CALIDAD) al que le corresponde
        ejecutar la siguiente acción de etapa, según Ticket.etapa_actual y
        tipo_flujo. Es la contraparte "de rol" del candado de orden que ya
        aplica _validar_etapa (que valida la secuencia, no quién la ejecuta).

        Devuelve None si no queda ninguna acción de etapa pendiente (FINALIZADO).
        """
        mapa = {
            Ticket.ETAPA_PENDIENTE_INGRESO:  'VIGILANCIA',   # siguiente: iniciar_ingreso_planta
            Ticket.ETAPA_VIGILANCIA_INGRESO: 'ALMACEN',      # siguiente: autorizar_almacen
            Ticket.ETAPA_ALMACEN: (
                'CALIDAD' if ticket.tipo_flujo == 'CON_CALIDAD' else 'ALMACEN'
            ),                                               # siguiente: registrar_calidad
            Ticket.ETAPA_CALIDAD:            'VIGILANCIA',   # siguiente: registrar_salida (CON_CALIDAD)
            Ticket.ETAPA_VIGILANCIA_SALIDA:  'VIGILANCIA',   # siguiente: registrar_salida (SOLO_ALMACEN)
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
        lineas = [line for po in ocs for line in po.lines.all()]

        if not lineas:
            raise ValidationError("La cita no tiene líneas de OC vinculadas.")

        # 2. Mapa de COAs ya cargados por el proveedor (TicketLineCOA, no TicketLineInspection)
        coas_por_linea = {
            coa.po_line_id: coa.coa_url
            for coa in TicketLineCOA.objects.filter(ticket=ticket)
        }

        # 3. Validación de seguridad según el flujo (CON_CALIDAD vs SOLO_ALMACEN)
        if ticket.tipo_flujo == 'CON_CALIDAD':
            lineas_con_coa_req = [line for line in lineas if line.requiere_coa]
            # Para Materia Prima (MP), el COA es mandatorio si alguna línea lo exige
            if lineas_con_coa_req:
                tiene_documentos = any(coas_por_linea.get(line.id) for line in lineas_con_coa_req)
                # Bloqueo estricto: ni en línea (TicketLineCOA) ni en el PDF general de la cita
                if not tiene_documentos and not appointment.coa_pdf:
                    raise ValidationError("Acceso Denegado: Este flujo (MATERIA PRIMA) requiere Certificados (COA) obligatorios.")

        # Nota: Si es SOLO_ALMACEN, se permite el ingreso aunque falte el COA,
        # delegando la validación física al personal de Almacén.

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
            for line in po.lines.all():
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

    # ─── Etapa 2: Almacén — Recepción ───────────────────────────────────────

    @staticmethod
    @transaction.atomic
    def autorizar_almacen(ticket_id: int, usuario_almacen, muelle: str = '') -> TicketStage:
        """
        Almacén autoriza el ingreso al muelle y registra su etapa.
        Cierra VIGILANCIA_ENTRADA y abre ALMACEN_RECEPCION.

        Comportamiento idéntico para CON_CALIDAD y SOLO_ALMACEN.
        La diferencia del flujo se gestiona en registrar_salida() y en
        las validaciones del detalle_ticket (acciones disponibles).
        """
        ticket = OperationsService._get_ticket_en_planta(ticket_id)

        # Candado de etapa: solo puede recibirse tras el ingreso de Vigilancia
        OperationsService._validar_etapa(ticket, Ticket.ETAPA_VIGILANCIA_INGRESO)

        OperationsService._cerrar_etapa(ticket, 'VIGILANCIA_ENTRADA', usuario_almacen)

        stage, _ = TicketStage.objects.get_or_create(
            ticket=ticket,
            etapa='ALMACEN_RECEPCION',
            defaults={'usuario': usuario_almacen}
        )

        if muelle:
            slot = ticket.appointment.slot
            slot.dock = muelle
            slot.save(update_fields=['dock'])

        # Inicializar líneas de inspección para etapa ALMACEN.
        # NOTA: ya no existe un esqueleto previo (se eliminó de confirmar_cita), así
        # que este get_or_create es ahora el que realmente CREA estas filas por
        # primera vez — por eso debe incluir 'doc_num' (campo obligatorio del modelo).
        appointment = ticket.appointment
        for po in appointment.purchase_orders.prefetch_related('lines').all():
            for line in po.lines.all():
                TicketLineInspection.objects.get_or_create(
                    ticket=ticket,
                    po_line=line,
                    etapa='ALMACEN',
                    defaults={
                        'doc_num': po.doc_num,
                        'usuario': usuario_almacen,
                        'cantidad_sap': line.quantity_sap,
                        'cantidad_modificada': line.quantity_sap,
                        'estado': 'PENDIENTE',#'EN_PROCESO',
                        'requiere_coa': line.requiere_coa,
                    }
                )

        # Avanzamos la etapa (candado de servicio)
        ticket.etapa_actual = Ticket.ETAPA_ALMACEN
        ticket.save(update_fields=['etapa_actual'])

        return stage

# ─── Etapa 3: Inspección Final (Adaptable para flujo CON_CALIDAD y SOLO_ALMACEN) ───
    @staticmethod
    @transaction.atomic
    def registrar_calidad(ticket_id: int, usuario_calidad,
                          resultados: list[dict]) -> TicketStage:
        """
        Registra resultados de inspección por línea de OC.
        Si es CON_CALIDAD: Registra como CALIDAD.
        Si es SOLO_ALMACEN: Registra como ALMACEN (Cierre de flujo).
        """
        ticket = OperationsService._get_ticket_en_planta(ticket_id)

        # Candado de etapa: tanto Calidad (CON_CALIDAD) como el cierre de
        # Almacén (SOLO_ALMACEN) parten de la misma etapa previa: ALMACEN.
        OperationsService._validar_etapa(ticket, Ticket.ETAPA_ALMACEN)

        # 1. Determinamos dinámicamente las etiquetas según el flujo
        es_mp = (ticket.tipo_flujo == 'CON_CALIDAD')

        etapa_historial = 'CALIDAD_INSPECCION' if es_mp else 'ALMACEN_FINALIZADO'
        etapa_linea = 'CALIDAD' if es_mp else 'ALMACEN'

        # 2. Cierre y apertura de etapas en el historial
        OperationsService._cerrar_etapa(ticket, 'ALMACEN_RECEPCION', usuario_calidad)

        stage, _ = TicketStage.objects.get_or_create(
            ticket=ticket,
            etapa=etapa_historial,
            defaults={'usuario': usuario_calidad}
        )

        for resultado in resultados:
            try:
                # Buscamos la línea base generada en el ingreso
                insp_almacen = TicketLineInspection.objects.get(
                    id=resultado.get('inspeccion_id'),
                    ticket=ticket,
                    etapa='ALMACEN' # Siempre leemos de la base de almacén
                )
            except TicketLineInspection.DoesNotExist:
                continue

            # Mantenemos el nombre de la variable 'insp_calidad' para no alterar estructura
            insp_calidad, _ = TicketLineInspection.objects.update_or_create(
                ticket=ticket,
                po_line=insp_almacen.po_line,
                etapa=etapa_linea, # <--- Operación quirúrgica: CALIDAD o ALMACEN
                defaults={
                    'doc_num': insp_almacen.doc_num,
                    'usuario': usuario_calidad,
                    'cantidad_sap': insp_almacen.cantidad_sap,
                    'cantidad_modificada': resultado.get(
                        'cantidad_modificada', insp_almacen.cantidad_modificada
                    ),                    
                    'estado': resultado.get('estado', 'CONFORME'), # Default Conforme si no es MP
                    'comentario': resultado.get('comentario', ''),
                    'requiere_coa': insp_almacen.requiere_coa,
                    'coa_url': resultado.get('coa_url') or insp_almacen.coa_url or '',
                }
            )

        # Avanzamos la etapa (candado de servicio): CALIDAD si CON_CALIDAD,
        # o directo a VIGILANCIA_SALIDA si SOLO_ALMACEN (se omite CALIDAD).
        ticket.etapa_actual = Ticket.ETAPA_CALIDAD if es_mp else Ticket.ETAPA_VIGILANCIA_SALIDA
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

        # Candado de etapa: depende del flujo, igual que el resto del método
        # (CON_CALIDAD cierra desde CALIDAD; SOLO_ALMACEN ya saltó a VIGILANCIA_SALIDA
        # en registrar_calidad).
        etapa_esperada = Ticket.ETAPA_CALIDAD if ticket.tipo_flujo == 'CON_CALIDAD' else Ticket.ETAPA_VIGILANCIA_SALIDA
        OperationsService._validar_etapa(ticket, etapa_esperada)

        ahora = timezone.now()

        # AJUSTE QUIRÚRGICO: Sincronización de etiquetas para evitar etapas huérfanas
        if ticket.tipo_flujo == 'CON_CALIDAD':
            # Cerramos la etapa abierta por el analista de Calidad
            OperationsService._cerrar_etapa(ticket, 'CALIDAD_INSPECCION', usuario_vigilancia)
        else:
            # SOLO_ALMACEN: Cerramos la etapa abierta por el Jefe de Almacén (registrar_calidad modificado)
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

        return ticket
    
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
        """
        if ticket.tipo_flujo != 'CON_CALIDAD':
            return True

        appointment = ticket.appointment
        lineas_requeridas = [
            line
            for po in appointment.purchase_orders.all()
            for line in po.lines.all()
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
        (Ticket.etapa_actual). La única etapa activa que corresponde a un
        formulario editable por línea es ALMACEN: es la que alimenta
        ajax_registrar_inspeccion / registrar_calidad, que siempre lee y
        escribe sobre las filas etapa='ALMACEN' (ver registrar_calidad, que
        busca `insp_almacen` por esa etapa sin importar si el flujo es
        CON_CALIDAD o SOLO_ALMACEN). El resto de etapas activas
        (PENDIENTE_INGRESO, VIGILANCIA_INGRESO, CALIDAD, VIGILANCIA_SALIDA)
        avanzan con una acción de un solo clic sin edición por línea, así
        que no tienen "mi sesión" propia — se devuelve {} en esos casos.
        """
        if ticket.etapa_actual != Ticket.ETAPA_ALMACEN:
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

            for line in po.lines.all():
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
        Motor Quirúrgico: Agrupa las inspecciones de un ticket por Número de OC.
        Permite que la UI renderice 'Tarjetas de OC' evitando redundancia visual.
        """
      
        inspecciones = TicketLineInspection.objects.filter(
            ticket_id=ticket_id,
            etapa=etapa
        ).select_related('po_line__purchase_order')

       

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

            # Lógica de cruce: Si el registro actual no tiene COA, buscamos en Vigilancia
            # para esa misma línea de orden de compra (po_line_id)
            coa_actual = insp.coa_url
            #print (f"coa_actual {coa_actual}  insp.po_line.item_code {insp.po_line.item_code} ticket_id {ticket_id}  oc_num={oc_num} insp.requiere_coa {insp.requiere_coa}")
            if not coa_actual:
                #print (f"not coa_actual {coa_actual} oc_num={oc_num}")
                coa_vigilancia = TicketLineInspection.objects.filter(
                    ticket_id=ticket_id,
                    etapa='VIGILANCIA',
                    po_line__item_code=insp.po_line.item_code, # Enlace por código de producto
                    doc_num=insp.doc_num
                   
                ).values_list('coa_url', flat=True).first()
                coa_actual = coa_vigilancia if coa_vigilancia else ''
                #print (f"if coa_actual {coa_actual} oc_num={oc_num} insp.po_line.item_code {insp.po_line.item_code} ticket_id {ticket_id} insp.requiere_coa {insp.requiere_coa}")

            grouped[oc_num]['lineas'].append({
                'id': insp.id,
                'item_code': insp.po_line.item_code,
                'descripcion': insp.po_line.description,
                'cantidad_sap': insp.cantidad_sap,
                'cantidad_modificada': insp.cantidad_modificada,
                'estado': insp.estado,
                'requiere_coa': insp.requiere_coa,
                'coa_url': coa_actual or '',
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

    @staticmethod
    def get_grouped_ocs_for_appointment(appointment_id: int) -> dict:
        """
        Agrupa las líneas de OC de una cita por número de OC.
        Usado por el portal del proveedor para mostrar tarjetas colapsables.
        Incluye estado de COA por línea (si ya fue subido a OneDrive).
        """
        from apps.appointments.models import Appointment
        from apps.operations.models import TicketLineInspection

        try:
            appointment = Appointment.objects.prefetch_related(
                'purchase_orders__lines'
            ).get(id=appointment_id)
        except Appointment.DoesNotExist:
            return {}

        grouped = defaultdict(lambda: {
            'oc_num': None,
            'card_name': '',
            'es_materia_prima': False,
            'total_lineas': 0,
            'lineas_requieren_coa': 0,
            'lineas_con_coa': 0,
            'qr_habilitado': False,
            'lineas': []
        })

        # Intentar obtener el ticket asociado (puede no existir aún)
        ticket = getattr(appointment, 'ticket', None)
        inspecciones_map = {}
        if ticket:
            for insp in ticket.inspections.filter(etapa='VIGILANCIA').select_related('po_line'):
                inspecciones_map[insp.po_line_id] = insp

        for po in appointment.purchase_orders.all():
            oc_num = po.doc_num
            grouped[oc_num]['oc_num'] = oc_num
            grouped[oc_num]['card_name'] = po.card_name
            grouped[oc_num]['es_materia_prima'] = po.es_materia_prima

            for line in po.lines.all():
                grouped[oc_num]['total_lineas'] += 1
                insp = inspecciones_map.get(line.id)
                coa_url = insp.coa_url if insp else ''
                
                if line.requiere_coa:
                    grouped[oc_num]['lineas_requieren_coa'] += 1
                    if coa_url:
                        grouped[oc_num]['lineas_con_coa'] += 1

                grouped[oc_num]['lineas'].append({
                    'po_line_id': line.id,
                    'item_code': line.item_code,
                    'descripcion': line.description,
                    'cantidad_sap': str(line.quantity_sap),
                    'und_medida': line.und_medida,
                    'requiere_coa': line.requiere_coa,
                    'coa_url': coa_url,
                })

        # Calcular si el QR está habilitado (todos los COA obligatorios cargados)
        for oc_data in grouped.values():
            req = oc_data['lineas_requieren_coa']
            cargados = oc_data['lineas_con_coa']
            oc_data['qr_habilitado'] = (req == 0) or (req == cargados)

        return dict(grouped)
