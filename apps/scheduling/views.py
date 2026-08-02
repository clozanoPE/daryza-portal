# apps/scheduling/views.py
#
# VISTAS DE ADMINISTRACIÓN DE HORARIOS — Fase 3
#
# CAPAS AFECTADAS:
#   - apps/scheduling/urls.py  → Registra todas estas vistas
#   - apps/scheduling/services.py  → Toda la lógica de negocio delega aquí
#   - apps/appointments/models.py  → Se leen/crean AppointmentSlots
#   - templates/scheduling/panel_horarios.html  → Template principal
#   - core/urls.py  → Debe incluir 'apps.scheduling.urls' bajo /scheduling/
#
# SEGURIDAD: Todas las vistas requieren login Y el permiso de staff (is_staff).
# El portal del proveedor NO tiene acceso a estas URLs.

import json
from datetime import date, timedelta

from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.views.decorators.http import require_POST, require_GET
from django.utils.decorators import method_decorator
from django.views import View

from .models import ScheduleTemplate, WeeklySlotRule
from .services import SchedulingService
from apps.base.decorators import (
    almacen_required, vigilancia_required, calidad_required,
    compras_required, staff_interno_required, staff_o_proveedor_required,
)


# ─── Vista principal del panel ───────────────────────────────────────────────────

@compras_required
def panel_horarios(request):
    """
    Vista principal del panel de administración de horarios semanales.
    Renderiza la grilla de la semana activa y el selector de semanas.

    GET params:
        ?semana=YYYY-MM-DD  → Fecha dentro de la semana a visualizar.
                              Si no se provee, usa la semana actual.

    Context para el template:
        semana_actual   : date del lunes visualizado
        semana_anterior : date del lunes anterior
        semana_siguiente: date del lunes siguiente
        slots_data      : JSON de la grilla (consumido por el JS del panel)
        templates       : Queryset de ScheduleTemplate activas (para el modal de generación)
        dias_semana     : Lista de dicts {fecha, nombre, iso} para renderizar columnas
    """
    # Determinar semana a visualizar
    fecha_param = request.GET.get('semana')
    if fecha_param:
        try:
            fecha_base = date.fromisoformat(fecha_param)
        except ValueError:
            fecha_base = date.today()
    else:
        fecha_base = date.today()

    lunes = SchedulingService.get_lunes_de_semana(fecha_base)
    sabado = lunes + timedelta(days=5)

    # Grilla de la semana para el template
    slots_data = SchedulingService.get_slots_semana_admin(lunes)

    # Estructura de días para renderizar cabeceras de columna
    NOMBRES_DIAS = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado']
    dias_semana = [
        {
            'fecha': (lunes + timedelta(days=i)).strftime('%Y-%m-%d'),
            'nombre': NOMBRES_DIAS[i],
            'fecha_display': (lunes + timedelta(days=i)).strftime('%d/%m'),
        }
        for i in range(6)
    ]

    context = {
        'semana_actual': lunes,
        'semana_anterior': (lunes - timedelta(weeks=1)),
        'semana_siguiente': (lunes + timedelta(weeks=1)),
        'sabado': sabado,
        'slots_json': json.dumps(slots_data),   # Para consumo JS sin llamada extra
        'templates': ScheduleTemplate.objects.filter(activa=True).order_by('nombre'),
        'dias_semana': dias_semana,
        'hay_semana_anterior': AppointmentSlot_existe_en_semana(lunes - timedelta(weeks=1)),
    }
    return render(request, 'scheduling/panel_horarios.html', context)


# ─── AJAX: Generar semana desde plantilla ───────────────────────────────────────

@compras_required
@require_POST
def ajax_generar_semana(request):
    """
    POST /scheduling/api/generar-semana/
    Body: { template_id: int, lunes: 'YYYY-MM-DD' }

    Genera AppointmentSlots para la semana indicada usando la plantilla seleccionada.
    Responde con el resumen de slots creados/existentes.
    """
    try:
        data = json.loads(request.body)
        template_id = data.get('template_id')
        lunes_str = data.get('lunes')

        if not template_id or not lunes_str:
            return JsonResponse(
                {'status': 'error', 'msg': 'Debes seleccionar una plantilla y una semana.'},
                status=400
            )

        template = get_object_or_404(ScheduleTemplate, id=template_id, activa=True)
        lunes = date.fromisoformat(lunes_str)

        resumen = SchedulingService.generar_semana(template, lunes)

        return JsonResponse({
            'status': 'success',
            'msg': (
                f"Semana generada: {resumen['creados']} slot(s) nuevos, "
                f"{resumen['existentes']} ya existían."
            ),
            'resumen': resumen,
        })

    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': str(e)}, status=400)


# ─── AJAX: Duplicar semana anterior ─────────────────────────────────────────────

@compras_required
@require_POST
def ajax_duplicar_semana(request):
    """
    POST /scheduling/api/duplicar-semana/
    Body: { lunes_origen: 'YYYY-MM-DD' }

    Copia los AppointmentSlots de la semana origen a la semana siguiente.
    Es la acción de 'duplicar semana anterior'.
    """
    try:
        data = json.loads(request.body)
        lunes_str = data.get('lunes_origen')

        if not lunes_str:
            return JsonResponse(
                {'status': 'error', 'msg': 'Se requiere la fecha de la semana origen.'},
                status=400
            )

        lunes_origen = date.fromisoformat(lunes_str)
        resumen = SchedulingService.duplicar_semana(lunes_origen)

        return JsonResponse({
            'status': 'success',
            'msg': (
                f"{resumen['copiados']} slot(s) copiados a la semana del "
                f"{resumen['lunes_destino']} – {resumen['sabado_destino']}. "
                f"{resumen['omitidos']} ya existían."
            ),
            'resumen': resumen,
        })

    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': str(e)}, status=400)


# ─── AJAX: Bloquear / Desbloquear slot ──────────────────────────────────────────

@compras_required
@require_POST
def ajax_toggle_bloqueo(request):
    """
    POST /scheduling/api/toggle-bloqueo/
    Body: { slot_id: int, accion: 'bloquear' | 'desbloquear' }

    Cambia el estado is_full_override de un AppointmentSlot.
    """
    try:
        data = json.loads(request.body)
        slot_id = data.get('slot_id')
        accion = data.get('accion')  # 'bloquear' o 'desbloquear'

        if not slot_id or accion not in ('bloquear', 'desbloquear'):
            return JsonResponse(
                {'status': 'error', 'msg': 'Parámetros inválidos.'},
                status=400
            )

        if accion == 'bloquear':
            slot = SchedulingService.bloquear_slot(slot_id)
            msg = f"Slot del {slot.date.strftime('%d/%m')} a las {slot.start_time.strftime('%H:%M')} bloqueado."
        else:
            slot = SchedulingService.desbloquear_slot(slot_id)
            msg = f"Slot del {slot.date.strftime('%d/%m')} a las {slot.start_time.strftime('%H:%M')} desbloqueado."

        return JsonResponse({'status': 'success', 'msg': msg})

    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': str(e)}, status=400)


# ─── AJAX: Eliminar slot vacío ───────────────────────────────────────────────────

@compras_required
@require_POST
def ajax_eliminar_slot(request):
    """
    POST /scheduling/api/eliminar-slot/
    Body: { slot_id: int }

    Elimina un AppointmentSlot SOLO si no tiene citas activas.
    """
    try:
        data = json.loads(request.body)
        slot_id = data.get('slot_id')

        if not slot_id:
            return JsonResponse(
                {'status': 'error', 'msg': 'Se requiere el ID del slot.'},
                status=400
            )

        info = SchedulingService.eliminar_slot_vacio(slot_id)
        return JsonResponse({
            'status': 'success',
            'msg': f"Slot del {info['fecha']} a las {info['hora']} eliminado."
        })

    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': str(e)}, status=400)


# ─── AJAX: Añadir slot individual ────────────────────────────────────────────────

@compras_required
def ajax_agregar_slot(request):
    """
    POST /scheduling/api/agregar-slot/
    Body: { fecha: 'YYYY-MM-DD', hora: 'HH:MM', capacidad: int }

    Añade un AppointmentSlot individual a una fecha/hora específica.
    Útil para ajustes puntuales fuera de la plantilla.
    """
    try:
        data = json.loads(request.body)
        fecha_str = data.get('fecha')
        hora_str = data.get('hora')
        capacidad = int(data.get('capacidad', 1))

        if not fecha_str or not hora_str:
            return JsonResponse(
                {'status': 'error', 'msg': 'Fecha y hora son requeridas.'},
                status=400
            )

        from datetime import datetime
        fecha = date.fromisoformat(fecha_str)
        hora = datetime.strptime(hora_str, '%H:%M').time()

        from apps.appointments.models import AppointmentSlot
        slot, created = AppointmentSlot.objects.get_or_create(
            date=fecha,
            start_time=hora,
            defaults={
                'dock': 'ALMACEN',
                'max_capacity': capacidad,
                'is_full_override': False,
            }
        )

        if not created:
            return JsonResponse(
                {'status': 'error', 'msg': 'Ya existe un slot para esa fecha y hora.'},
                status=400
            )

        return JsonResponse({
            'status': 'success',
            'msg': f"Slot del {fecha.strftime('%d/%m/%Y')} a las {hora.strftime('%H:%M')} agregado.",
        })

    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': str(e)}, status=400)


# ─── AJAX: Citas de un slot (panel lateral de detalle) ────────────────────────────

@compras_required
@require_GET
def ajax_get_citas_slot(request, slot_id: int):
    """
    GET /scheduling/api/citas-slot/<slot_id>/

    Devuelve las citas (Appointment) asociadas a un AppointmentSlot, para el
    panel lateral que se abre al hacer click en un slot ocupado de la grilla
    (antes no hacía nada — ver verDetalleSlot en panel_horarios.js).

    Por cada cita expone el proveedor, el estado y, si ya existe Ticket
    (la cita fue confirmada por Compras), su id para enlazar directamente a
    operations:trazabilidad_ticket (Fase 5) — no se duplica esa vista.
    """
    from apps.appointments.models import AppointmentSlot

    slot = get_object_or_404(AppointmentSlot, id=slot_id)

    citas = []
    for appointment in slot.appointments.select_related('user').order_by('id'):
        ticket = getattr(appointment, 'ticket', None)
        citas.append({
            'appointment_id':  appointment.id,
            'proveedor_nombre': appointment.user.get_full_name() or appointment.user.username,
            'proveedor_codigo': appointment.user.username,
            'ticket_id':        ticket.id if ticket else None,
            'estado_display':   ticket.get_estado_display() if ticket else appointment.get_status_display(),
        })

    return JsonResponse({
        'status': 'success',
        'fecha': slot.date.strftime('%d/%m/%Y'),
        'hora':  slot.start_time.strftime('%H:%M'),
        'citas': citas,
    })


# ─── Helper interno ──────────────────────────────────────────────────────────────

def AppointmentSlot_existe_en_semana(lunes: date) -> bool:
    """
    Verifica si existen AppointmentSlots en la semana de 'lunes'.
    Usado para habilitar/deshabilitar el botón 'Duplicar semana anterior'.
    """
    from apps.appointments.models import AppointmentSlot
    sabado = lunes + timedelta(days=5)
    return AppointmentSlot.objects.filter(date__range=[lunes, sabado]).exists()
