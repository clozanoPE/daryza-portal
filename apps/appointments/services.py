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
    def get_semana_matrix(fecha_inicio):
        """
        Genera una matriz de 6 días CONSECUTIVOS desde fecha_inicio —
        ventana rodante, no una semana calendario lunes-sábado (sesión 38:
        ya se comportaba así de facto, con fecha_inicio siempre "hoy", pero
        el template mostraba encabezados de columna fijos "Lunes...Sábado"
        que no correspondían al día real. Sesión 39: se agrega 'dias', la
        lista de encabezados reales para que el template deje de usar texto
        fijo).

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
    def solicitar_cita_borrador(user, slot_id, oc_ids, coa_file=None, lugar_entrega='LURIN'):
        """
        Lógica centralizada para Daryza: Valida integridad y crea la cita.
        """
        with transaction.atomic():
            # 1. Validar disponibilidad del horario (Slot)
            slot = SlotService.validar_disponibilidad(slot_id)

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
                lugar_entrega=lugar_entrega,
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

            # Pre-marcar líneas de OCs MP que aún no fueron ajustadas manualmente
            for po in ocs:
                if po.es_materia_prima:
                    po.lines.filter(requiere_coa=False).update(requiere_coa=True)

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

            # 8. Notificar al proveedor con el QR
            AppointmentService._notificar_proveedor_qr(appointment, ticket)

            return ticket

    @staticmethod
    def preparar_detalle_compras(appointment_id):
        """
        Llamar a esta función cuando Compras abra el detalle de una cita SOLICITADA.
        Automatiza el pre-marcado de Materia Prima.
        """ 
        appointment = Appointment.objects.prefetch_related('purchase_orders__lines').get(id=appointment_id)
        for po in appointment.purchase_orders.all():
            if po.es_materia_prima:
                # Pre-marcamos solo las que no han sido tocadas (opcional)
                po.lines.filter(requiere_coa=False).update(requiere_coa=True)
        return appointment

    @staticmethod
    def _notificar_proveedor_qr(appointment: Appointment, ticket: Ticket) -> None:
        """
        Centraliza la lógica de notificación al proveedor tras confirmar su cita.

        Estado actual: Preparado para integración.
        - Genera la URL del QR lista para ser incluida en un email o WhatsApp.
        - El campo appointment.token_qr ya está guardado en BD.

        Para integrar email real: descomentar el bloque de send_mail
        y añadir las variables EMAIL_* en core/settings.py y .env

        Para integrar WhatsApp (Twilio/Meta): llamar al servicio correspondiente
        pasando appointment.user.profile.telefono y la url_qr generada.

        CAPAS AFECTADAS cuando se implemente:
          - core/settings.py  → EMAIL_BACKEND, EMAIL_HOST, etc.
          - requirements.txt  → añadir twilio si se usa WhatsApp
        """
        # URL pública del QR (el proveedor accede a esta URL para ver su ticket)
        # En producción, reemplazar con la URL real del dominio.
        url_qr = f"/appointments/ticket/{appointment.token_qr}/"

        # ── Integración email (lista para activar) ──────────────────────────────
        # from django.core.mail import send_mail
        # from django.conf import settings
        #
        # proveedor_email = appointment.user.email
        # if proveedor_email:
        #     send_mail(
        #         subject=f'[Daryza VBS] Cita #{appointment.id} Confirmada',
        #         message=(
        #             f'Su cita ha sido confirmada.\n'
        #             f'Fecha: {appointment.slot.date.strftime("%d/%m/%Y")}\n'
        #             f'Hora: {appointment.slot.start_time.strftime("%H:%M")}\n'
        #             f'Código QR: {appointment.token_qr}\n'
        #             f'Acceda aquí: {url_qr}\n'
        #             f'Presente este código al llegar a planta.'
        #         ),
        #         from_email=settings.DEFAULT_FROM_EMAIL,
        #         recipient_list=[proveedor_email],
        #         fail_silently=True,  # No interrumpir el flujo si el email falla
        #     )

        # Por ahora: log en consola para desarrollo
        print(
            f"[VBS NOTIFICACIÓN] Cita #{appointment.id} confirmada. "
            f"QR: {appointment.token_qr} | URL: {url_qr}"
        )
