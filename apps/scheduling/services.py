# apps/scheduling/services.py
#
# CAPA DE SERVICIO — scheduling
# Encapsula toda la lógica de negocio de programación de horarios.
#
# CAPAS AFECTADAS / CONSUMIDORES:
#   - apps/scheduling/views.py  → Llama a SchedulingService para todas las acciones
#   - apps/appointments/models.py → Se crean/modifican instancias de AppointmentSlot
#   - apps/appointments/services.py → SlotService.get_semana_matrix() ya lee AppointmentSlots;
#     los Slots generados aquí aparecen automáticamente en el portal del proveedor.
#
# PRINCIPIO: Este servicio NO modifica datos de citas (Appointment) existentes.
# Solo crea, actualiza o elimina AppointmentSlots que AÚN no tengan citas vinculadas.

from datetime import date, timedelta
from django.db import transaction
from django.core.exceptions import ValidationError

# Importamos AppointmentSlot desde su app original — no duplicamos el modelo
from apps.appointments.models import AppointmentSlot
from .models import ScheduleTemplate, WeeklySlotRule


class SchedulingService:
    """
    Servicio central para la gestión de la programación de horarios semanales.

    Métodos principales:
        - get_lunes_de_semana(fecha)       → Obtiene el lunes de la semana de una fecha
        - generar_semana(template, lunes)  → Crea AppointmentSlots desde una plantilla
        - duplicar_semana(semana_origen)   → Clona Slots de una semana a la siguiente
        - get_slots_semana(lunes)          → Devuelve matriz de slots por día/hora
        - bloquear_slot(slot_id)           → Activa is_full_override
        - desbloquear_slot(slot_id)        → Desactiva is_full_override
        - eliminar_slot_vacio(slot_id)     → Elimina solo si no tiene citas vinculadas
    """

    # ─── Utilidades de fecha ────────────────────────────────────────────────────

    @staticmethod
    def get_lunes_de_semana(fecha: date) -> date:
        """
        Retorna el lunes de la semana a la que pertenece 'fecha'.
        Ejemplo: si fecha=miércoles 2026-04-08, retorna 2026-04-07.
        """
        return fecha - timedelta(days=fecha.weekday())

    @staticmethod
    def get_rango_semana(lunes: date) -> tuple[date, date]:
        """Retorna (lunes, sábado) de la semana."""
        return lunes, lunes + timedelta(days=5)

    # ─── Generación desde plantilla ─────────────────────────────────────────────

    @staticmethod
    @transaction.atomic
    def generar_semana(template: ScheduleTemplate, lunes: date) -> dict:
        """
        Lee las WeeklySlotRule activas de 'template' y crea AppointmentSlots
        para la semana que comienza en 'lunes'.

        Reglas de negocio:
          - Solo crea slots que NO existan aún (get_or_create para idempotencia).
          - No sobreescribe slots que ya tengan citas confirmadas.
          - Retorna un resumen: {'creados': N, 'existentes': M, 'total': N+M}

        Parámetros:
            template (ScheduleTemplate): Plantilla con las reglas de horario.
            lunes (date): Debe ser un lunes. Se valida internamente.
        """
        if lunes.weekday() != 0:
            raise ValidationError(
                f"La fecha proporcionada ({lunes}) no es lunes. "
                "Usa SchedulingService.get_lunes_de_semana() para obtenerlo."
            )

        reglas = template.reglas.filter(activa=True).order_by('dia_semana', 'hora')

        if not reglas.exists():
            raise ValidationError(
                f"La plantilla '{template.nombre}' no tiene reglas activas. "
                "Agrega horarios antes de generar la semana."
            )

        creados = 0
        existentes = 0

        for regla in reglas:
            # Calculamos la fecha concreta: lunes + offset del día
            fecha_slot = lunes + timedelta(days=regla.dia_semana)

            slot, created = AppointmentSlot.objects.get_or_create(
                date=fecha_slot,
                start_time=regla.hora,
                defaults={
                    # dock: campo requerido por AppointmentSlot, usamos valor neutro.
                    # El personal puede editar esto después desde el panel.
                    'dock': 'ALMACEN',
                    'max_capacity': regla.capacidad,
                    'is_full_override': False,
                    # end_time es opcional en el modelo (null=True)
                }
            )

            if created:
                creados += 1
            else:
                existentes += 1

        return {
            'creados': creados,
            'existentes': existentes,
            'total': creados + existentes,
            'lunes': lunes.strftime('%d/%m/%Y'),
            'sabado': (lunes + timedelta(days=5)).strftime('%d/%m/%Y'),
        }

    # ─── Duplicar semana anterior ────────────────────────────────────────────────

    @staticmethod
    @transaction.atomic
    def duplicar_semana(lunes_origen: date) -> dict:
        """
        Copia los AppointmentSlots de la semana 'lunes_origen' hacia la semana siguiente.
        Esta es la funcionalidad de 'duplicar semana anterior' para el personal.

        Reglas de negocio:
          - Solo copia slots de la semana origen que existan en BD.
          - Respeta max_capacity e is_full_override del origen.
          - No duplica slots que ya existan en la semana destino.
          - No requiere una plantilla: trabaja directo sobre slots existentes.

        Retorna:
            dict con 'copiados', 'omitidos' (ya existían), 'lunes_destino'.
        """
        if lunes_origen.weekday() != 0:
            raise ValidationError(
                f"La fecha origen ({lunes_origen}) no es lunes. "
                "Proporciona el primer día (lunes) de la semana a duplicar."
            )

        lunes_destino = lunes_origen + timedelta(weeks=1)
        sabado_origen = lunes_origen + timedelta(days=5)

        slots_origen = AppointmentSlot.objects.filter(
            date__range=[lunes_origen, sabado_origen]
        ).order_by('date', 'start_time')

        if not slots_origen.exists():
            raise ValidationError(
                f"No existen slots en la semana del "
                f"{lunes_origen.strftime('%d/%m/%Y')}. "
                "Genera primero esa semana antes de duplicarla."
            )

        copiados = 0
        omitidos = 0

        for slot_origen in slots_origen:
            # Offset de días dentro de la semana (0=lunes, 5=sábado)
            offset = (slot_origen.date - lunes_origen).days
            fecha_destino = lunes_destino + timedelta(days=offset)

            _, created = AppointmentSlot.objects.get_or_create(
                date=fecha_destino,
                start_time=slot_origen.start_time,
                defaults={
                    'dock': slot_origen.dock,
                    'max_capacity': slot_origen.max_capacity,
                    # Al duplicar, los bloqueos manuales NO se copian (se resetean).
                    # El personal puede volver a bloquearlos si lo necesita.
                    'is_full_override': False,
                    'end_time': slot_origen.end_time,
                }
            )

            if created:
                copiados += 1
            else:
                omitidos += 1

        return {
            'copiados': copiados,
            'omitidos': omitidos,
            'lunes_origen': lunes_origen.strftime('%d/%m/%Y'),
            'lunes_destino': lunes_destino.strftime('%d/%m/%Y'),
            'sabado_destino': (lunes_destino + timedelta(days=5)).strftime('%d/%m/%Y'),
        }

    # ─── Consulta de matriz semanal (para el panel admin) ───────────────────────

    @staticmethod
    def get_slots_semana_admin(lunes: date) -> list[dict]:
        """
        Devuelve los AppointmentSlots de la semana en formato de grilla para
        la vista de administración, incluyendo información de citas vinculadas.

        Retorna lista de dicts con estructura:
            [{
                'id': int,
                'fecha': str 'YYYY-MM-DD',
                'dia_nombre': str 'Lunes',
                'hora': str 'HH:MM',
                'max_capacity': int,
                'citas_count': int,
                'is_full_override': bool,
                'estado': 'disponible' | 'parcial' | 'lleno' | 'bloqueado',
                'puede_eliminarse': bool,
            }]

        NOTA: Este método es distinto a SlotService.get_semana_matrix() de appointments,
        que devuelve formato orientado al proveedor. Este devuelve datos para el admin.
        """
        DIAS = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado']
        sabado = lunes + timedelta(days=5)

        slots = AppointmentSlot.objects.filter(
            date__range=[lunes, sabado]
        ).prefetch_related('appointments').order_by('date', 'start_time')

        resultado = []
        for slot in slots:
            citas_count = slot.appointments.count()
            dia_index = slot.date.weekday()  # 0=Lunes

            if slot.is_full_override:
                estado = 'bloqueado'
            elif citas_count >= slot.max_capacity:
                estado = 'lleno'
            elif citas_count > 0:
                estado = 'parcial'
            else:
                estado = 'disponible'

            resultado.append({
                'id': slot.id,
                'fecha': slot.date.strftime('%Y-%m-%d'),
                'dia_nombre': DIAS[dia_index] if dia_index < 6 else 'Otro',
                'hora': slot.start_time.strftime('%H:%M'),
                'max_capacity': slot.max_capacity,
                'citas_count': citas_count,
                'is_full_override': slot.is_full_override,
                'estado': estado,
                # Solo puede eliminarse si no tiene citas activas vinculadas
                'puede_eliminarse': citas_count == 0,
            })

        return resultado

    # ─── Acciones de gestión individual de slots ────────────────────────────────

    @staticmethod
    def bloquear_slot(slot_id: int) -> AppointmentSlot:
        """
        Bloquea un slot manualmente para que no acepte nuevas citas.
        Equivale a activar is_full_override=True.
        NO cancela citas ya confirmadas en ese slot.
        """
        try:
            slot = AppointmentSlot.objects.get(id=slot_id)
        except AppointmentSlot.DoesNotExist:
            raise ValidationError(f"El slot #{slot_id} no existe.")

        slot.is_full_override = True
        slot.save(update_fields=['is_full_override', 'updated_at'])
        return slot

    @staticmethod
    def desbloquear_slot(slot_id: int) -> AppointmentSlot:
        """
        Desbloquea un slot previamente bloqueado manualmente.
        Equivale a desactivar is_full_override=False.
        """
        try:
            slot = AppointmentSlot.objects.get(id=slot_id)
        except AppointmentSlot.DoesNotExist:
            raise ValidationError(f"El slot #{slot_id} no existe.")

        slot.is_full_override = False
        slot.save(update_fields=['is_full_override', 'updated_at'])
        return slot

    @staticmethod
    def eliminar_slot_vacio(slot_id: int) -> dict:
        """
        Elimina un AppointmentSlot SOLO si no tiene citas vinculadas.
        Previene eliminar slots con solicitudes activas (SOLICITADO/CONFIRMADA).
        """
        try:
            slot = AppointmentSlot.objects.prefetch_related('appointments').get(id=slot_id)
        except AppointmentSlot.DoesNotExist:
            raise ValidationError(f"El slot #{slot_id} no existe.")

        citas_activas = slot.appointments.filter(
            status__in=['SOLICITADO', 'CONFIRMADA']
        ).count()

        if citas_activas > 0:
            raise ValidationError(
                f"No se puede eliminar: el slot tiene {citas_activas} "
                f"cita(s) activa(s). Cancélalas primero."
            )

        info = {
            'slot_id': slot.id,
            'fecha': slot.date.strftime('%d/%m/%Y'),
            'hora': slot.start_time.strftime('%H:%M'),
        }
        slot.delete()
        return info
