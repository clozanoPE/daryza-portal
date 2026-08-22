# apps/appointments/services.py
import uuid
from django.core.exceptions import ValidationError
from django.db import transaction
from datetime import datetime, timedelta
from django.utils import timezone
from apps.appointments.models import Appointment, AppointmentSlot
from apps.operations.models import Ticket
from apps.sap_sync.models import PurchaseOrder

class SlotService:
    # Abreviaturas indexadas por date.weekday() (0=Lunes ... 6=Domingo) —
    # solo para etiquetar columnas, no implica ninguna semántica de semana
    # calendario (la ventana puede empezar cualquier día).
    NOMBRES_DIA = ['LUN', 'MAR', 'MIÉ', 'JUE', 'VIE', 'SÁB', 'DOM']

    @staticmethod
    def get_semana_matrix(fecha_inicio, sede):
        """
        Genera una matriz de 6 días CONSECUTIVOS desde fecha_inicio, SOLO
        para 'sede' (sesión 48b: antes mostraba TODOS los slots sin filtrar
        por sede en un único pool visual — dos sedes con cupos a la misma
        fecha/hora aparecían mezclados en la misma celda) — ventana
        rodante, no una semana calendario lunes-sábado (sesión 38: ya se
        comportaba así de facto, con fecha_inicio siempre "hoy", pero el
        template mostraba encabezados de columna fijos "Lunes...Sábado" que
        no correspondían al día real. Sesión 39: se agrega 'dias', la lista
        de encabezados reales para que el template deje de usar texto fijo).

        También marca cada slot de HOY cuya hora de inicio ya pasó como
        color='pasado' (no seleccionable, sin importar cupos libres) —
        comparado en hora local de Lima vía timezone.localtime(), no con
        timezone.now() crudo (UTC con USE_TZ=True), que desfasaría la
        comparación hasta 5 horas.

        Retorna (matrix, dias):
            matrix: dict {hora_str: [ {id, fecha, color, disponibles}, ... ]}
            dias  : lista de 6 dicts {fecha, nombre, fecha_display} — igual
                    forma que apps/scheduling usa para su propio 'dias_semana',
                    para consistencia entre paneles.
        """
        fecha_fin = fecha_inicio + timedelta(days=5)  # 6 días consecutivos
        ahora = timezone.localtime(timezone.now())
        hoy = ahora.date()

        dias = [
            {
                'fecha': (fecha_inicio + timedelta(days=i)).strftime('%Y-%m-%d'),
                'nombre': SlotService.NOMBRES_DIA[(fecha_inicio + timedelta(days=i)).weekday()],
                'fecha_display': (fecha_inicio + timedelta(days=i)).strftime('%d/%m'),
            }
            for i in range(6)
        ]

        slots = AppointmentSlot.objects.filter(
            sede=sede,
            date__range=[fecha_inicio, fecha_fin]
        ).order_by('start_time', 'date')

        matrix = {}
        for slot in slots:
            hora_str = slot.start_time.strftime('%H:%M')
            if hora_str not in matrix:
                matrix[hora_str] = []

            es_pasado = slot.date == hoy and slot.start_time < ahora.time()

            if es_pasado:
                color = "pasado"  # Hora ya pasada hoy — no seleccionable, aunque tenga cupos
            elif slot.is_full_override:
                color = "gris"  # Bloqueado
            elif slot.appointments.count() >= slot.max_capacity:
                color = "rojo"  # Lleno
            else:
                color = "verde"  # Disponible

            matrix[hora_str].append({
                "id": slot.id,
                "fecha": slot.date.strftime('%Y-%m-%d'),
                "color": color,
                "disponibles": slot.max_capacity - slot.appointments.count()
            })
        return matrix, dias

    @staticmethod
    def validar_disponibilidad(slot_id):
        slot = AppointmentSlot.objects.get(id=slot_id)
        if slot.is_full_override:
            raise ValidationError("Este horario ha sido bloqueado por Almacén (No disponible).")

        if slot.appointments.count() >= slot.max_capacity:
            raise ValidationError("Capacidad agotada para este horario (Lleno).")

        # Sesión 39: rechazo en backend de un slot cuya fecha/hora ya pasó —
        # antes no existía ningún control temporal aquí ni en ningún otro
        # punto del flujo (confirmado en el análisis de la sesión 38), así
        # que un intento vía API directa (sin pasar por la grilla, donde el
        # slot ya aparece deshabilitado) podía reservar un horario pasado.
        # Comparación en hora local de Lima, mismo criterio que
        # get_semana_matrix (timezone.now() crudo es UTC con USE_TZ=True).
        ahora = timezone.localtime(timezone.now())
        if slot.date < ahora.date() or (slot.date == ahora.date() and slot.start_time < ahora.time()):
            raise ValidationError("No se puede reservar un horario que ya pasó.")

        return slot

class AppointmentService:
    @staticmethod
    def solicitar_cita_borrador(user, slot_id, oc_ids, coa_file=None):
        """
        Lógica centralizada para Daryza: Valida integridad y crea la cita.

        Sesión 48b: ya no recibe la sede como parámetro separado (antes
        `lugar_entrega`, un <select> del modal desacoplado de la grilla —
        un proveedor podía, en teoría, elegir un slot de la Agenda de una
        sede y una Sede de entrega distinta en el combo, dejando el
        Appointment con una sede que no correspondía a su propio slot). La
        sede de la cita se infiere directamente de `slot.sede` — la Agenda
        de Cupos ya está filtrada a una sola sede en el portal (cada slot
        pertenece a exactamente una), así que no hay combinación posible
        que discrepe.
        """
        with transaction.atomic():
            # 1. Validar disponibilidad del horario (Slot)
            slot = SlotService.validar_disponibilidad(slot_id)
            sede = slot.sede

            # 2. VALIDACIÓN DE OCs DUPLICADAS (No perder esta lógica)
            # Buscamos si alguna de las OCs ya pertenece a una cita que no sea rechazada/cancelada
            if oc_ids:
                ocs_en_uso = PurchaseOrder.objects.filter(
                    id__in=oc_ids,
                    appointment__status__in=['SOLICITADO', 'CONFIRMADA', 'FINALIZADA']
                ).values_list('doc_num', flat=True).distinct()

                if ocs_en_uso.exists():
                    # Mantenemos el mensaje detallado para el usuario
                    lista_docs = ", ".join([str(doc) for doc in ocs_en_uso])
                    raise ValidationError(
                        f"Las siguientes OCs ya tienen una solicitud activa: {lista_docs}. "
                        "Revise su historial de citas."
                    )

                # 2b. VALIDACIÓN: no mezclar OC Materia Prima con OC comerciales
                # en la misma solicitud (regla de negocio del rediseño de flujo
                # Materia Prima — el ticket resultante debe rutear a un solo
                # actor, ALMACEN o MATERIA_PRIMA, según el tipo de OC). Esta es
                # la capa que realmente importa: el picker del portal solo
                # deshabilita visualmente el tipo contrario, pero cualquier vía
                # que llegue directo aquí (bug de JS, request manual) queda
                # bloqueada igual, sin guardar nada.
                ocs_seleccionadas = PurchaseOrder.objects.filter(id__in=oc_ids)
                tipos_seleccionados = {po.es_materia_prima for po in ocs_seleccionadas}
                if len(tipos_seleccionados) > 1:
                    raise ValidationError(
                        "No se puede combinar Materia Prima con OC comerciales "
                        "en la misma solicitud."
                    )

            # 3. Creación del objeto Appointment (Atómico)
            appointment = Appointment.objects.create(
                user=user,
                slot=slot,
                status='SOLICITADO',
                sede=sede,
                coa_pdf=coa_file
            )
            
            # 4. RELACIÓN MANY-TO-MANY
            # Aquí es donde fallaba el IntegrityError si oc_ids contenía basura o nulos
            if oc_ids:
                # Limpiamos la lista por si acaso vienen strings o nulos del front-end
                clean_oc_ids = [int(oid) for oid in oc_ids if oid]
                appointment.purchase_orders.set(clean_oc_ids)
            
            return appointment

    @staticmethod
    def confirmar_cita(appointment_id, usuario_almacen):
        with transaction.atomic():
            # 1. Lock para evitar doble confirmación concurrente
            appointment = Appointment.objects.select_for_update().get(id=appointment_id)

            # 2. Idempotencia: si ya fue confirmada, retornar ticket existente
            if appointment.status == 'CONFIRMADA':
                return Ticket.objects.get(appointment=appointment)

            # 3. Marcar cita como confirmada
            ahora = timezone.now()
            appointment.status = 'CONFIRMADA'
            appointment.fecha_respuesta_admin = ahora

            # 4. Generar código QR único y consistente para Appointment y Ticket
            #    Formato: DYZ-{appointment_id}-{YYMMDD}-{6 chars únicos}
            sufijo_unico = uuid.uuid4().hex[:6].upper()
            codigo_qr = f"DYZ-{appointment.id}-{ahora.strftime('%y%m%d')}-{sufijo_unico}"

            appointment.token_qr = codigo_qr
            appointment.save(update_fields=['status', 'fecha_respuesta_admin', 'token_qr'])

            # 5. Determinar tipo_flujo:
            #    Si alguna OC es MP y tiene al menos una línea con requiere_coa=True
            #    (pre-marcadas automáticamente o ajustadas por Compras) → CON_CALIDAD.
            #    En caso contrario → SOLO_ALMACEN.
            #
            #    JERARQUÍA DE DECISIÓN: OC > Artículo.
            #    Para OCs MP: todas sus líneas se pre-marcan requiere_coa=True.
            #    Compras puede desmarcar líneas específicas antes de confirmar.
            #    Si tras el ajuste ninguna línea queda marcada → flujo SOLO_ALMACEN.
            ocs = appointment.purchase_orders.prefetch_related('lines').all()

            # Pre-marcar líneas de OCs MP que aún no fueron ajustadas manualmente.
            # activa=True (sesión 50): una línea cancelada en SAP nunca debe
            # marcarse como requiere_coa=True — no hay nada que inspeccionar.
            for po in ocs:
                if po.es_materia_prima:
                    po.lines.filter(requiere_coa=False, activa=True).update(requiere_coa=True)

            # Calcular tipo_flujo en base al estado final de las líneas
            #hay_lineas_coa = any(
            #    po.lines.filter(requiere_coa=True).exists()
            #    for po in ocs
            #)

            # 1. Identificamos si hay al menos un documento de Materia Prima
            es_materia_prima_total = any(po.es_materia_prima for po in ocs)

            tipo_flujo = 'CON_CALIDAD' if es_materia_prima_total else 'SOLO_ALMACEN'

            # 6. Crear o actualizar el Ticket vinculado
            ticket, created = Ticket.objects.update_or_create(
                appointment=appointment,
                defaults={
                    'estado': 'PROGRAMADO',
                    'codigo_qr': codigo_qr,
                    'tipo_flujo': tipo_flujo,
                }
            )

            # 7. Bloquear el slot si ya alcanzó su capacidad máxima
            slot = appointment.slot
            citas_confirmadas = slot.appointments.filter(
                status__in=['CONFIRMADA', 'SOLICITADO']
            ).count()
            if citas_confirmadas >= slot.max_capacity:
                slot.is_full_override = True
                slot.save(update_fields=['is_full_override'])

            # Nota: ya NO se crea aquí ningún esqueleto de TicketLineInspection.
            # La carga de COA por línea del proveedor se guarda en TicketLineCOA
            # (apps/operations/models.py), independiente de las etapas operativas.
            # Las filas de TicketLineInspection se crean recién cuando cada etapa
            # (Vigilancia, Almacén, Calidad) las procesa realmente.

            # 8. Notificar al proveedor con el QR (correo real, Graph) — un
            # fallo de envío NUNCA revierte la confirmación (pedido
            # explícito): se captura y se deja visible en un atributo
            # transitorio del ticket (no persistido, sin campo de BD
            # nuevo) para que la vista que llamó a confirmar_cita pueda
            # incluirlo en su respuesta JSON — no hay logging persistente
            # en este proyecto (sesión 49), así que perderlo en silencio
            # lo haría invisible por completo.
            resultado_correo = AppointmentService._notificar_proveedor_qr(appointment, ticket)
            ticket.email_notificacion_error = (
                resultado_correo.error if resultado_correo and not resultado_correo.enviado else None
            )

            return ticket

    @staticmethod
    def preparar_detalle_compras(appointment_id):
        """
        Llamar a esta función cuando Compras abra el detalle/configuración
        de COA de una cita SOLICITADA (ver ajax_get_lineas_oc). Automatiza
        el pre-marcado de Materia Prima: por cada OC tipo MP, sugiere
        requiere_coa=True en las líneas que aún estén en False — un valor
        por defecto inteligente, no una regla forzada; Compras sigue
        pudiendo desmarcarlas y guardar su propia decisión vía
        ajax_configurar_items_coa. Las OC no-MP nunca se tocan aquí.

        Solo actúa mientras Appointment.coa_configurado sea False (nunca
        se guardó una configuración manual para esta cita) — no-op en
        caso contrario, para no sobreescribir una decisión ya guardada
        (incluida la de desmarcar todo explícitamente).
        """
        appointment = Appointment.objects.prefetch_related('purchase_orders__lines').get(id=appointment_id)
        if appointment.coa_configurado:
            return appointment
        for po in appointment.purchase_orders.all():
            if po.es_materia_prima:
                # activa=True (sesión 50): no sugerir COA en una línea que
                # SAP ya canceló/eliminó — no hay nada que inspeccionar.
                po.lines.filter(requiere_coa=False, activa=True).update(requiere_coa=True)
        return appointment

    @staticmethod
    def _notificar_proveedor_qr(appointment: Appointment, ticket: Ticket):
        """
        Notifica al proveedor por correo real (Microsoft Graph, ver
        apps/base/services_correo.py) que su cita fue confirmada — cierra
        la deuda histórica de este método (antes solo hacía `print()`, sin
        ningún envío real, desde que se escribió por primera vez).

        Devuelve el ResultadoEnvioCorreo (o None si ni siquiera se intentó
        por falta de email) — el llamador (confirmar_cita) decide dónde
        dejarlo visible; esta función nunca lanza.
        """
        from django.conf import settings

        from apps.base.services_correo import enviar_correo

        # URL absoluta: el link va en un correo real, no en una página ya
        # cargada en el navegador — una ruta relativa no resuelve a nada
        # útil en un cliente de correo. Se construye desde ALLOWED_HOSTS
        # (ya configurado, sin agregar ninguna variable de entorno nueva
        # solo para esto) en vez de asumir un dominio fijo.
        dominio = settings.ALLOWED_HOSTS[0] if settings.ALLOWED_HOSTS else 'localhost:8000'
        esquema = 'http' if dominio.startswith('localhost') or dominio.startswith('127.') else 'https'
        url_qr = f"{esquema}://{dominio}/appointments/ticket/{appointment.token_qr}/"

        proveedor_email = appointment.user.email
        if not proveedor_email:
            print(
                f"[VBS NOTIFICACIÓN] Cita #{appointment.id} confirmada, pero el "
                f"proveedor ({appointment.user.username}) no tiene email registrado "
                f"— no se envió ningún correo."
            )
            return None

        cuerpo_html = (
            f"<p>Su cita ha sido confirmada.</p>"
            f"<ul>"
            f"<li>Fecha: {appointment.slot.date.strftime('%d/%m/%Y')}</li>"
            f"<li>Hora: {appointment.slot.start_time.strftime('%H:%M')}</li>"
            f"<li>Código QR: {appointment.token_qr}</li>"
            f"</ul>"
            f"<p>Acceda aquí: <a href=\"{url_qr}\">{url_qr}</a></p>"
            f"<p>Presente este código al llegar a planta.</p>"
        )
        resultado = enviar_correo(
            destinatario=proveedor_email,
            asunto=f'[Daryza VBS] Cita #{appointment.id} Confirmada',
            cuerpo_html=cuerpo_html,
        )
        if not resultado.enviado:
            print(
                f"[VBS NOTIFICACIÓN] Error al enviar correo de confirmación "
                f"(Cita #{appointment.id}, destinatario {proveedor_email}): {resultado.error}"
            )
        return resultado
