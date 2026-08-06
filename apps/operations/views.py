"""
apps/operations/views.py
========================
Vistas operativas del ciclo de vida del Ticket de Inspección.

Flujo completo:
  PROVEEDOR solicita → COMPRAS revisa/aprueba → QR generado
  → VIGILANCIA escanea QR → EN_PLANTA
  → [si CON_CALIDAD]: ALMACEN (iniciar) → CALIDAD (inspección) → ALMACEN (recepción)
  → [si SOLO_ALMACEN]: ALMACEN (inspección + recepción)
  → VIGILANCIA (salida) → FINALIZADO

Cada vista tiene un solo decorador de rol, sin lógica duplicada.
"""
import json
from io import BytesIO

from django.core.exceptions import ValidationError
from django.db.models import Prefetch, Q
from django.http import JsonResponse, HttpResponse, HttpResponseBadRequest
from django.shortcuts import render, get_object_or_404, redirect
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST
from django.utils import timezone
from xhtml2pdf import pisa

from apps.sap_sync.models import PurchaseOrder, PurchaseOrderLine
from apps.appointments.models import Appointment
from apps.appointments.services import AppointmentService
from .models import Ticket, TicketStage, TicketLineInspection, TicketLineCOA
from .services import OperationsService
from apps.base.decorators import (
    almacen_required, vigilancia_required, calidad_required,
    compras_required, staff_interno_required, staff_o_proveedor_required,
    materia_prima_required,
)
from apps.base.filters import resolver_periodo
from apps.base.reporting import ticket_a_row, exportar_excel, exportar_pdf


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS INTERNOS
# ═══════════════════════════════════════════════════════════════════════════════

def _json_ok(data: dict = None, **kwargs) -> JsonResponse:
    """Respuesta JSON de éxito estándar."""
    payload = {'status': 'success', **(data or {}), **kwargs}
    return JsonResponse(payload)


def _json_err(msg: str, status: int = 400) -> JsonResponse:
    """Respuesta JSON de error estándar."""
    return JsonResponse({'status': 'error', 'msg': msg}, status=status)


def _safe_post(request, func):
    """
    Ejecuta func() dentro de try/except y devuelve JSON.
    Simplifica el patrón repetido en todas las vistas AJAX.
    """
    try:
        data = json.loads(request.body)
        return func(data)
    except ValidationError as e:
        msg = e.messages[0] if hasattr(e, 'messages') else str(e)
        return _json_err(msg)
    except Exception as e:
        return _json_err(str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# PANEL COMPRAS — Revisión, configuración COA, aprobación
# ═══════════════════════════════════════════════════════════════════════════════

def _historial_compras_qs(request):
    """
    Queryset de tickets_historial de panel_compras, ya filtrado (q/fecha/
    periodo) — extraído a función propia (Fase 15, sesión 21) para que
    exportar_historial_compras exporte EXACTAMENTE lo mismo que ve el
    usuario en pantalla, sin duplicar la lógica de filtrado.
    Devuelve (queryset, periodo) — periodo se usa para el nombre del archivo.
    """
    q     = request.GET.get('q', '').strip()
    fecha = request.GET.get('fecha', '')
    anio, mes, periodo = resolver_periodo(request)

    tickets_qs = Ticket.objects.select_related(
        'appointment__slot', 'appointment__user'
    ).prefetch_related(
        'appointment__purchase_orders'
    ).order_by('-fecha_creacion')

    if q:
        tickets_qs = tickets_qs.filter(
            Q(id__icontains=q) |
            Q(appointment__purchase_orders__doc_num__icontains=q) |
            Q(appointment__user__username__icontains=q)
        ).distinct()

    if fecha:
        tickets_qs = tickets_qs.filter(appointment__slot__date=fecha)

    tickets_qs = tickets_qs.filter(
        appointment__slot__date__year=anio,
        appointment__slot__date__month=mes,
    )
    return tickets_qs, periodo


def _exportar_tickets(tickets_qs, formato, filename, titulo):
    """
    Despacho compartido Excel/PDF (Fase 15) para los 3 paneles internos —
    evita repetir el if/elif de formato en cada vista de exportación.
    """
    rows = [ticket_a_row(t) for t in tickets_qs]
    if formato == 'excel':
        return exportar_excel(rows, filename, titulo)
    if formato == 'pdf':
        return exportar_pdf(rows, filename, titulo)
    return HttpResponseBadRequest('Formato de reporte no soportado. Use "excel" o "pdf".')


@compras_required
def panel_compras(request):
    """
    Panel de Compras:
      - Pestaña "Pendientes": citas SOLICITADAS para validar OCs y
        configurar qué líneas requieren COA antes de aprobar o rechazar.
      - Pestaña "Historial" (Fase 6): TODOS los Tickets del sistema (no
        solo los que confirmó este usuario), cada uno enlazando a
        operations:trazabilidad_ticket (Fase 5) para su detalle de solo
        lectura. No se modifica ninguna lógica de etapa_actual ni de
        permisos — solo se agrega un punto de entrada de consulta.
    Los filtros (q, fecha) ya existentes aplican a ambas pestañas. El
    filtro de período (mes/año, Fase 10) solo aplica al Historial —
    "Pendientes" es la cola activa, no tiene sentido acotarla por mes.
    """
    q     = request.GET.get('q', '').strip()
    fecha = request.GET.get('fecha', '')

    qs = Appointment.objects.filter(
        status='SOLICITADO'
    ).select_related('slot', 'user').prefetch_related(
        'purchase_orders__lines'
    ).order_by('-created_at')

    if q:
        qs = qs.filter(
            Q(id__icontains=q) |
            Q(purchase_orders__doc_num__icontains=q) |
            Q(user__username__icontains=q)
        ).distinct()

    if fecha:
        qs = qs.filter(slot__date=fecha)

    # ── Historial (Fase 6): todos los tickets, sin filtrar por confirmante ──
    tickets_qs, periodo = _historial_compras_qs(request)

    context = {
        'solicitudes':       qs,
        'tickets_historial': tickets_qs,
        'q':                 q,
        'fecha_filtro':      fecha,
        'periodo':           periodo,
    }
    return render(request, 'operations/panel_compras.html', context)


@compras_required
def exportar_historial_compras(request, formato: str):
    """
    GET /operations/compras/historial/exportar/<excel|pdf>/  (Fase 15)
    Exporta EXACTAMENTE el mismo tickets_historial que ve Compras en pantalla
    (mismos filtros q/fecha/periodo ya aplicados vía querystring).
    """
    tickets_qs, periodo = _historial_compras_qs(request)
    return _exportar_tickets(
        tickets_qs, formato,
        filename=f'historial_compras_{periodo}',
        titulo=f'Historial de Tickets — Panel Compras ({periodo})',
    )


@compras_required
def ajax_get_lineas_oc(request, appointment_id: int):
    """
    GET /operations/api/compras/lineas/<appointment_id>/
    Devuelve las líneas de OC de una cita con su flag requiere_coa actual.
    """
    try:
        appointment = get_object_or_404(
            Appointment.objects.prefetch_related('purchase_orders__lines'),
            id=appointment_id
        )
        lineas = []
        for po in appointment.purchase_orders.all():
            for line in po.lines.all():
                lineas.append({
                    'po_line_id':   line.id,
                    'doc_num':      po.doc_num,
                    'tipo_oc':      po.u_mss_tdb or 'COMERCIAL',
                    'es_mp':        po.es_materia_prima,
                    'item_code':    line.item_code,
                    'description':  line.description,
                    'quantity_sap': str(line.quantity_sap),
                    'und_medida':   line.und_medida,
                    'requiere_coa': line.requiere_coa,
                })
        return _json_ok(lineas=lineas)
    except Exception as e:
        return _json_err(str(e))


@compras_required
@require_POST
def ajax_configurar_items_coa(request):
    """
    POST /operations/api/configurar-items-coa/
    Body: { appointment_id: int, lineas: [{po_line_id, requiere_coa}, …] }
    Compras ajusta manualmente qué líneas requieren COA.
    """
    def handle(data):
        appointment_id = data.get('appointment_id')
        lineas         = data.get('lineas', [])
        if not appointment_id:
            return _json_err('Se requiere appointment_id.')

        appointment = get_object_or_404(Appointment, id=appointment_id, status='SOLICITADO')
        oc_ids = set(appointment.purchase_orders.values_list('id', flat=True))

        actualizadas = 0
        for item in lineas:
            updated = PurchaseOrderLine.objects.filter(
                id=item.get('po_line_id'),
                purchase_order__id__in=oc_ids
            ).update(requiere_coa=bool(item.get('requiere_coa', False)))
            actualizadas += updated

        return _json_ok(msg=f'{actualizadas} línea(s) actualizadas.', actualizadas=actualizadas)

    return _safe_post(request, handle)


@compras_required
@require_POST
def ajax_confirmar_cita_compras(request):
    """
    POST /operations/api/compras/confirmar-cita/
    Body: { appointment_id: int, observaciones: str }
    Compras aprueba la cita. Crea el Ticket y genera QR.
    """
    def handle(data):
        appointment_id = data.get('appointment_id')
        observaciones  = data.get('observaciones', '').strip()
        if not appointment_id:
            return _json_err('Se requiere appointment_id.')

        if observaciones:
            Appointment.objects.filter(id=appointment_id).update(
                observaciones_admin=observaciones
            )

        ticket = AppointmentService.confirmar_cita(
            appointment_id=appointment_id,
            usuario_almacen=request.user
        )
        return _json_ok(
            msg=f'Cita #{appointment_id} aprobada. Ticket #{ticket.id} generado.',
            ticket_id=ticket.id,
            codigo_qr=ticket.codigo_qr,
            tipo_flujo=ticket.tipo_flujo,
        )

    return _safe_post(request, handle)


@compras_required
@require_POST
def ajax_rechazar_cita_compras(request):
    """
    POST /operations/api/compras/rechazar-cita/
    Body: { appointment_id: int, observaciones: str }
    """
    def handle(data):
        appointment_id = data.get('appointment_id')
        observaciones  = data.get('observaciones', '').strip()

        appointment = get_object_or_404(Appointment, id=appointment_id)
        if appointment.status != 'SOLICITADO':
            return _json_err(f'No se puede rechazar una cita en estado {appointment.status}.')

        appointment.status               = 'RECHAZADO'
        appointment.observaciones_admin  = observaciones
        appointment.fecha_respuesta_admin = timezone.now()
        appointment.save(update_fields=['status', 'observaciones_admin', 'fecha_respuesta_admin'])
        return _json_ok(msg=f'Cita #{appointment_id} rechazada.')

    return _safe_post(request, handle)


# ═══════════════════════════════════════════════════════════════════════════════
# PANEL ALMACÉN — Confirmación legacy (si se usa panel_almacen en vez de compras)
# ═══════════════════════════════════════════════════════════════════════════════

@almacen_required
def panel_almacen(request):
    """
    Panel Almacén: vista Kanban con solicitudes pendientes, confirmadas y rechazadas.
    """
    q      = request.GET.get('q', '').strip()
    estado = request.GET.get('estado', '')
    fecha  = request.GET.get('fecha', '')

    qs = Appointment.objects.select_related(
        'slot', 'user'
    ).prefetch_related('purchase_orders').order_by('-created_at')

    if q:
        qs = qs.filter(
            Q(id__icontains=q) |
            Q(purchase_orders__doc_num__icontains=q) |
            Q(user__username__icontains=q)
        ).distinct()

    if estado:
        qs = qs.filter(status=estado)
    if fecha:
        qs = qs.filter(slot__date=fecha)

    context = {
        'solicitadas':     qs.filter(status='SOLICITADO'),
        'confirmadas':     qs.filter(status='CONFIRMADA'),
        'rechazadas':      qs.filter(status__in=['RECHAZADO', 'CANCELADA']),
        'q':               q,
        'estado_filtro':   estado,
        'fecha_filtro':    fecha,
        'estados_choices': Appointment.ESTADOS,
    }
    return render(request, 'operations/panel_almacen.html', context)


@almacen_required
@require_POST
def ajax_confirmar_cita(request):
    """
    POST /operations/api/confirmar-cita/
    Almacén confirma una cita (legacy/alternativo al endpoint de Compras).
    """
    def handle(data):
        appointment_id = data.get('appointment_id')
        observaciones  = data.get('observaciones', '').strip()
        if not appointment_id:
            return _json_err('Se requiere appointment_id.')
        if observaciones:
            Appointment.objects.filter(id=appointment_id).update(
                observaciones_admin=observaciones
            )
        ticket = AppointmentService.confirmar_cita(
            appointment_id=appointment_id,
            usuario_almacen=request.user
        )
        return _json_ok(
            msg=f'Cita #{appointment_id} confirmada. Ticket #{ticket.id} generado.',
            ticket_id=ticket.id,
            codigo_qr=ticket.codigo_qr,
        )

    return _safe_post(request, handle)


@almacen_required
@require_POST
def ajax_rechazar_cita(request):
    """POST /operations/api/rechazar-cita/"""
    def handle(data):
        appointment_id = data.get('appointment_id')
        observaciones  = data.get('observaciones', '').strip()
        appointment = get_object_or_404(Appointment, id=appointment_id)
        if appointment.status != 'SOLICITADO':
            return _json_err(f'No se puede rechazar una cita en estado {appointment.status}.')
        appointment.status               = 'RECHAZADO'
        appointment.observaciones_admin  = observaciones
        appointment.fecha_respuesta_admin = timezone.now()
        appointment.save(update_fields=['status', 'observaciones_admin', 'fecha_respuesta_admin'])
        return _json_ok(msg=f'Cita #{appointment_id} rechazada.')

    return _safe_post(request, handle)


@staff_interno_required
@require_POST
def ajax_autorizar_almacen(request):
    """
    POST /operations/api/autorizar-almacen/ ("Iniciar Recepción")

    Sesión 30 (rediseño Materia Prima, Fase 3+4): este endpoint ahora sirve
    tanto a Almacén como a Materia Prima — @staff_interno_required por sí
    solo permite cualquier grupo interno, sin distinguir cuál corresponde
    al ticket concreto. Igual que ajax_registrar_inspeccion, se valida
    aquí que el grupo del usuario coincida con
    OperationsService.grupo_requerido_por_etapa(ticket) (que ya bifurca
    ALMACEN/MATERIA_PRIMA según el tipo de OC). Los superusuarios siempre
    pueden.

    Se reutiliza el mismo endpoint/URL/nombre de siempre (no se creó uno
    nuevo) para que el botón "Iniciar Recepción" existente (Almacén,
    Fase 5 pendiente de actualizar su JS/template) siga funcionando sin
    cambios: requiere_calidad/confirmado son opcionales, con default de
    negocio resuelto en el servicio si no se envían.
    """
    def handle(data):
        ticket_id = data.get('ticket_id')
        muelle    = data.get('muelle', '').strip()
        if not ticket_id:
            return _json_err('Se requiere ticket_id.')

        ticket = get_object_or_404(Ticket, id=ticket_id)

        if not request.user.is_superuser:
            grupo_requerido = OperationsService.grupo_requerido_por_etapa(ticket)
            if not grupo_requerido or not request.user.groups.filter(name=grupo_requerido).exists():
                return _json_err(
                    f'No tienes permiso para iniciar la recepción del Ticket #{ticket.id}. '
                    f'Se requiere pertenecer al grupo {grupo_requerido or "correspondiente"}.',
                    status=403
                )

        requiere_calidad = data.get('requiere_calidad')
        confirmado = bool(data.get('confirmado', False))

        stage = OperationsService.autorizar_almacen(
            ticket_id=ticket_id,
            usuario=request.user,
            muelle=muelle,
            requiere_calidad=requiere_calidad,
            confirmado=confirmado,
        )
        return _json_ok(msg=f'Recepción registrada. Etapa {stage.get_etapa_display()} iniciada.')

    return _safe_post(request, handle)


def _historial_por_periodo_qs(request):
    """
    Queryset de tickets_historial compartido por panel_vigilancia y
    panel_calidad: TODOS los tickets, acotados solo por el filtro de
    período (mes/año, Fase 10) — ninguno de los 2 paneles tiene filtro
    q/fecha propio (a diferencia de Compras, ver _historial_compras_qs).
    Extraído (Fase 15, sesión 21) para que exportar_historial_vigilancia y
    exportar_historial_calidad exporten exactamente lo que ve cada panel.
    Devuelve (queryset, periodo).
    """
    anio, mes, periodo = resolver_periodo(request)
    tickets_qs = Ticket.objects.select_related(
        'appointment__slot', 'appointment__user'
    ).prefetch_related(
        'appointment__purchase_orders'
    ).filter(
        appointment__slot__date__year=anio,
        appointment__slot__date__month=mes,
    ).order_by('-fecha_creacion')
    return tickets_qs, periodo


# ═══════════════════════════════════════════════════════════════════════════════
# PANEL MATERIA PRIMA — Fase 2 del rediseño de flujo Materia Prima (sesión 29)
# ═══════════════════════════════════════════════════════════════════════════════

@materia_prima_required
def panel_materia_prima(request):
    """
    Panel del grupo MATERIA_PRIMA — actor nuevo, paralelo a ALMACEN, que
    ejecutará "Iniciar Recepción" para tickets cuyas OCs sean tipo Materia
    Prima (según PurchaseOrder.es_materia_prima), una vez que la Fase 4
    del rediseño bifurque grupo_requerido_por_etapa y autorizar_almacen.

    Esta fase (2) es solo el aterrizaje: login → panel propio. Sin
    lógica de recepción, sin queryset de tickets pendientes todavía —
    eso depende de la bifurcación de la Fase 4, que no se toca aquí.
    """
    return render(request, 'operations/panel_materia_prima.html', {})


# ═══════════════════════════════════════════════════════════════════════════════
# PANEL VIGILANCIA — Ingreso y salida por QR
# ═══════════════════════════════════════════════════════════════════════════════

@vigilancia_required
def panel_vigilancia(request):
    """
    Panel de Vigilancia:
      - Pestaña "Hoy": Kanban de tickets del día (Programados/En Planta/
        Finalizados), sin cambios respecto a antes.
      - Pestaña "Historial" (Fase 11, mismo patrón de la Fase 6): TODOS los
        Tickets del sistema, de cualquier fecha, cada uno enlazando a
        operations:trazabilidad_ticket para consulta de solo lectura — antes
        Vigilancia solo podía ver tickets del día actual.
    Incluye buscador QR para escaneo en puerta (sin cambios). El filtro de
    período (mes/año, Fase 10) solo aplica al Historial — "Hoy" ya está
    acotado por definición al día actual.

    Sesión 25 — mismo fix del cronómetro ya aplicado en panel_calidad
    (sesión 23): se anota `ticket.fecha_ingreso_planta` (etapa
    VIGILANCIA_ENTRADA explícita) para el cronómetro "En Planta ahora".
    El de "Finalizados hoy" usa `ticket.tiempo_total_planta` (timedelta
    ya calculado en Python, sin JS) y no se toca — nunca tuvo este bug.
    """
    hoy = timezone.now().date()

    base_qs = Ticket.objects.select_related(
        'appointment__slot',
        'appointment__user',
    ).prefetch_related(
        'stages',
        'inspections',
        'appointment__purchase_orders__lines',
        'coas',
    ).filter(
        appointment__slot__date=hoy
    ).exclude(estado='CANCELADO').order_by('appointment__slot__start_time')

    tickets_prog = base_qs.filter(estado='PROGRAMADO')
    tickets_planta = base_qs.filter(estado='EN_PLANTA')
    tickets_fin = base_qs.filter(estado='FINALIZADO')

    # Anotamos coa_completo (vía TicketLineCOA) en cada ticket programado, para
    # que el badge del Kanban muestre el estado real sin entrar al detalle.
    for ticket in tickets_prog:
        ticket.coa_completo = OperationsService.calcular_coa_completo(ticket)

    # Anotamos fecha_ingreso_planta (etapa VIGILANCIA_ENTRADA explícita) para
    # el cronómetro "En Planta ahora" — mismo fix de la sesión 23.
    for ticket in tickets_planta:
        entrada = next(
            (s for s in ticket.stages.all() if s.etapa == 'VIGILANCIA_ENTRADA'),
            None
        )
        ticket.fecha_ingreso_planta = entrada.fecha_inicio if entrada else None

    # ── Historial (Fase 11): todos los tickets, acotados al período (Fase 10) ──
    tickets_historial, periodo = _historial_por_periodo_qs(request)

    context = {
        'tickets_programados': tickets_prog,
        'tickets_en_planta':   tickets_planta,
        'tickets_finalizados': tickets_fin,
        'tickets_historial':   tickets_historial,
        'periodo':             periodo,
        'hoy': hoy,
    }
    return render(request, 'operations/panel_vigilancia.html', context)


@vigilancia_required
def exportar_historial_vigilancia(request, formato: str):
    """
    GET /operations/vigilancia/historial/exportar/<excel|pdf>/  (Fase 15)
    Exporta EXACTAMENTE el mismo tickets_historial que ve Vigilancia en
    pantalla (mismo filtro de período ya aplicado vía querystring).
    """
    tickets_qs, periodo = _historial_por_periodo_qs(request)
    return _exportar_tickets(
        tickets_qs, formato,
        filename=f'historial_vigilancia_{periodo}',
        titulo=f'Historial de Tickets — Panel Vigilancia ({periodo})',
    )


@staff_o_proveedor_required
def scan_qr(request):
    """
    GET /operations/vigilancia/scan/?codigo=<qr>

    Busca el Ticket por su código QR y redirige según si el usuario tiene una
    acción operativa pendiente sobre él, usando el mismo cálculo de turno que
    ya protege ajax_registrar_inspeccion (OperationsService.grupo_requerido_por_etapa):
      - El grupo del usuario coincide con el turno pendiente del ticket (o es
        superusuario) -> detalle_ticket (ficha con acciones).
      - Cualquier otro caso -rol de solo consulta, el ticket ya no tiene
        acción pendiente (p.ej. FINALIZADO), o el usuario es el proveedor
        dueño de la cita- -> trazabilidad_ticket (solo lectura).
    La verificación de que un proveedor solo pueda ver SU propio ticket queda
    delegada a las vistas de destino (mismo criterio que ya aplican
    detalle_ticket y trazabilidad_ticket), no se duplica aquí.
    """
    from django.contrib import messages

    codigo = request.GET.get('codigo', '').strip()
    es_proveedor = request.user.groups.filter(name='PROVEEDORES').exists()
    fallback_url = 'appointments:portal_proveedor' if es_proveedor else 'operations:panel_vigilancia'

    if not codigo:
        messages.error(request, 'Ingresa un código QR para buscar.')
        return redirect(fallback_url)

    ticket = Ticket.objects.filter(codigo_qr=codigo).select_related(
        'appointment__slot', 'appointment__user'
    ).first()

    if not ticket:
        messages.error(request, f'No se encontró ningún ticket con el código "{codigo}".')
        return redirect(fallback_url)

    grupo_pendiente = (
        OperationsService.grupo_requerido_por_etapa(ticket)
        if ticket.estado in ('PROGRAMADO', 'EN_PLANTA') else None
    )
    tiene_accion_pendiente = (
        grupo_pendiente is not None and
        (request.user.is_superuser or request.user.groups.filter(name=grupo_pendiente).exists())
    )

    if tiene_accion_pendiente:
        return redirect('operations:detalle_ticket', pk=ticket.id)
    return redirect('operations:trazabilidad_ticket', pk=ticket.id)


@vigilancia_required
@require_POST
def ajax_autorizar_ingreso(request):
    """POST /operations/api/autorizar-ingreso/"""
    def handle(data):
        ticket_id = data.get('ticket_id')
        if not ticket_id:
            return _json_err('Se requiere ticket_id.')
        ticket = OperationsService.iniciar_ingreso_planta(
            ticket_id=ticket_id,
            usuario_vigilancia=request.user
        )
        return _json_ok(msg=f'Ingreso autorizado. Ticket #{ticket.id} EN_PLANTA.')

    return _safe_post(request, handle)


@vigilancia_required
@require_POST
def ajax_registrar_salida(request):
    """POST /operations/api/registrar-salida/"""
    def handle(data):
        ticket_id = data.get('ticket_id')
        ticket = OperationsService.registrar_salida(
            ticket_id=ticket_id,
            usuario_vigilancia=request.user
        )
        tiempo = '—'
        if ticket.tiempo_total_planta:
            mins = int(ticket.tiempo_total_planta.total_seconds() // 60)
            h, m = divmod(mins, 60)
            tiempo = f'{h}h {m}min' if h else f'{m}min'
        return _json_ok(
            msg=f'Salida registrada. Ticket #{ticket.id} FINALIZADO.',
            tiempo_total=tiempo,
        )

    return _safe_post(request, handle)


# ═══════════════════════════════════════════════════════════════════════════════
# PANEL CALIDAD — Inspección de Materia Prima
# ═══════════════════════════════════════════════════════════════════════════════

@calidad_required
def panel_calidad(request):
    """
    Panel de Calidad:
      - Pestaña "Pendientes": tickets EN_PLANTA con tipo_flujo=CON_CALIDAD
        donde la etapa VIGILANCIA_ENTRADA está completa y la recepción de
        Almacén ha iniciado (ALMACEN_RECEPCION existe) pero Calidad no.
      - Pestaña "Historial" (Fase 11, mismo patrón de la Fase 6): TODOS los
        Tickets del sistema, cada uno enlazando a operations:trazabilidad_ticket
        para consulta de solo lectura — antes, en cuanto Calidad terminaba su
        inspección, el ticket desaparecía de este panel sin dejar rastro
        consultable.

    Flujo correcto (pestaña Pendientes):
      VIGILANCIA_ENTRADA (cierra) → ALMACEN_RECEPCION (abre)
      → Calidad inspecciona → CALIDAD_INSPECCION (crea)
      → ALMACEN hace recepción final

    El filtro de período (mes/año, Fase 10) solo aplica al Historial —
    "Pendientes" es la cola activa, no tiene sentido acotarla por mes.

    Sesión 23 — 2 bugs corregidos en esta vista (ver CLAUDE.md para el
    diagnóstico completo):
      - El Prefetch de 'inspections' ahora fuerza un orden explícito
        (por OC y luego por línea). TicketLineInspection no tiene Meta.
        ordering, así que sin esto el orden de `ticket.inspections.all`
        no está garantizado; el {% regroup %} del template EXIGE que el
        queryset ya venga ordenado por la clave de agrupación, si no,
        una misma OC con filas de etapas distintas intercaladas (p.ej.
        VIGILANCIA y ALMACEN) se parte en grupos duplicados.
      - Se anota `ticket.fecha_ingreso_planta` (la fecha_inicio de la
        etapa VIGILANCIA_ENTRADA, explícita — no "la primera fila que
        haya, sea la que sea") para el cronómetro JS del template.

    Sesión 24 — bug adicional corregido: la columna "COA" de esta tabla
    leía TicketLineInspection.coa_url (fila etapa='ALMACEN'), que
    autorizar_almacen() nunca puebla — siempre None, mostraba "Falta"
    aunque el proveedor sí hubiera cargado el COA (verificado en
    TicketLineCOA). Se anota `insp.coa_url_real` por línea, cruzado por
    po_line_id contra TicketLineCOA (fuente única desde la Fase 1),
    sobre los objetos ya prefetched (sin queries extra por ticket).
    """
    tickets_para_calidad = Ticket.objects.filter(
        estado='EN_PLANTA',
        tipo_flujo='CON_CALIDAD',
        stages__etapa='ALMACEN_RECEPCION',
    ).exclude(
        stages__etapa='CALIDAD_INSPECCION'
    ).select_related(
        'appointment__slot', 'appointment__user'
    ).prefetch_related(
        Prefetch(
            'inspections',
            queryset=TicketLineInspection.objects.select_related(
                'po_line__purchase_order'
            ).order_by('po_line__purchase_order_id', 'po_line__line_num'),
        ),
        'stages',
    ).distinct()

    for ticket in tickets_para_calidad:
        entrada = next(
            (s for s in ticket.stages.all() if s.etapa == 'VIGILANCIA_ENTRADA'),
            None
        )
        ticket.fecha_ingreso_planta = entrada.fecha_inicio if entrada else None

        coas_por_linea = {
            coa.po_line_id: coa.coa_url
            for coa in TicketLineCOA.objects.filter(ticket=ticket)
        }
        for insp in ticket.inspections.all():
            insp.coa_url_real = coas_por_linea.get(insp.po_line_id, '')

    # ── Historial (Fase 11): todos los tickets, acotados al período (Fase 10) ──
    tickets_historial, periodo = _historial_por_periodo_qs(request)

    context = {
        'tickets':            tickets_para_calidad,
        'tickets_historial':  tickets_historial,
        'periodo':            periodo,
    }
    return render(request, 'operations/panel_calidad.html', context)


@calidad_required
def exportar_historial_calidad(request, formato: str):
    """
    GET /operations/calidad/historial/exportar/<excel|pdf>/  (Fase 15)
    Exporta EXACTAMENTE el mismo tickets_historial que ve Calidad en
    pantalla (mismo filtro de período ya aplicado vía querystring).
    """
    tickets_qs, periodo = _historial_por_periodo_qs(request)
    return _exportar_tickets(
        tickets_qs, formato,
        filename=f'historial_calidad_{periodo}',
        titulo=f'Historial de Tickets — Panel Calidad ({periodo})',
    )


@staff_interno_required
@require_POST
def ajax_registrar_inspeccion(request):
    """
    POST /operations/api/registrar-inspeccion/
    Body: { ticket_id: int, resultados: [{inspeccion_id, estado, cantidad_modificada, comentario}] }

    Este endpoint sirve tanto a Calidad (flujo CON_CALIDAD) como al cierre de
    Almacén (flujo SOLO_ALMACEN) — @staff_interno_required por sí solo permite
    cualquier grupo interno (ALMACEN/CALIDAD/VIGILANCIA/COMPRAS), sin distinguir
    cuál corresponde a la etapa activa del ticket. Por eso, además del login,
    validamos aquí que el grupo del usuario coincida con
    OperationsService.grupo_requerido_por_etapa(ticket) — el candado de orden
    (Ticket.etapa_actual) ya lo aplica OperationsService.registrar_calidad.
    Los superusuarios/administradores siempre pueden.
    """
    def handle(data):
        ticket_id  = data.get('ticket_id')
        resultados = data.get('resultados', [])
        if not ticket_id or not resultados:
            return _json_err('Se requiere ticket_id y al menos un resultado.')

        ticket = get_object_or_404(Ticket, id=ticket_id)

        if not request.user.is_superuser:
            grupo_requerido = OperationsService.grupo_requerido_por_etapa(ticket)
            if not grupo_requerido or not request.user.groups.filter(name=grupo_requerido).exists():
                return _json_err(
                    f'No tienes permiso para registrar esta etapa del Ticket #{ticket.id}. '
                    f'Se requiere pertenecer al grupo {grupo_requerido or "correspondiente"}.',
                    status=403
                )

        stage = OperationsService.registrar_calidad(
            ticket_id=ticket_id,
            usuario_calidad=request.user,
            resultados=resultados
        )
        return _json_ok(msg=f'Inspección registrada. Etapa {stage.get_etapa_display()} iniciada.')

    return _safe_post(request, handle)


# ═══════════════════════════════════════════════════════════════════════════════
# DETALLE DEL TICKET — Trazabilidad completa + acciones contextuales
# ═══════════════════════════════════════════════════════════════════════════════

@staff_o_proveedor_required
def detalle_ticket(request, pk: int):
    """
    Vista de trazabilidad completa de un Ticket con lógica híbrida (Borrador vs Generado).
    """
    # 1. Query optimizada con select_related y prefetch para toda la cadena de datos
    ticket = get_object_or_404(
        Ticket.objects.select_related(
            'appointment__slot',
            'appointment__user',
        ).prefetch_related(
            'stages__usuario',
            'inspections__po_line__purchase_order',
            'inspections__usuario',
            'appointment__purchase_orders__lines', # Líneas originales de SAP
        ),
        pk=pk
    )

    # ── Validación de privacidad ──
    es_proveedor = request.user.groups.filter(name='PROVEEDORES').exists()
    if es_proveedor and ticket.appointment.user != request.user:
        return redirect('appointments:portal_proveedor')
    


    es_staff_interno = (
        request.user.groups.filter(
            name__in=['ALMACEN', 'CALIDAD', 'VIGILANCIA', 'COMPRAS']
        ).exists() or request.user.is_superuser
    )

    etapas_completadas = set(ticket.stages.values_list('etapa', flat=True))
    es_flujo_calidad   = (ticket.tipo_flujo == 'CON_CALIDAD')

    # ── Estado del COA (independiente de las etapas, vía TicketLineCOA) ──
    coa_completo = OperationsService.calcular_coa_completo(ticket)

    es_vigilancia = request.user.groups.filter(name='VIGILANCIA').exists() or request.user.is_superuser
    es_almacen = request.user.groups.filter(name='ALMACEN').exists()
    es_calidad = request.user.groups.filter(name='CALIDAD').exists()

    # ── Gate de edición de "mi sesión" (vía Ticket.etapa_actual) ──
    # Reutiliza el mismo cálculo que ya usa ajax_registrar_inspeccion para
    # decidir a quién le toca actuar (OperationsService.grupo_requerido_por_etapa),
    # así la UI y el backend nunca quedan desincronizados sobre "de quién es el turno".
    # Antes, este bloque se activaba con 'es_flujo_calidad' exigido en AMBAS ramas
    # (Calidad y Almacén), lo que impedía por completo que el flujo SOLO_ALMACEN
    # mostrara el formulario de cierre de Almacén; ese bug queda corregido al
    # depender ahora exclusivamente de la etapa activa + el grupo correspondiente.
    grupo_etapa_activa = OperationsService.grupo_requerido_por_etapa(ticket)
    es_su_turno = (
        request.user.is_superuser or
        (grupo_etapa_activa is not None and request.user.groups.filter(name=grupo_etapa_activa).exists())
    )
    puede_editar_mi_sesion = (
        ticket.estado == 'EN_PLANTA' and
        ticket.etapa_actual == Ticket.ETAPA_ALMACEN and
        es_su_turno
    )

    # ── Mapa de acciones ──
    acciones = {
        'puede_autorizar_ingreso': (es_vigilancia and ticket.estado == 'PROGRAMADO'),
        'puede_autorizar_almacen': (
            es_almacen and ticket.estado == 'EN_PLANTA' and
            'VIGILANCIA_ENTRADA' in etapas_completadas and
            'ALMACEN_RECEPCION' not in etapas_completadas
        ),
        'puede_editar_mi_sesion': puede_editar_mi_sesion,
        'puede_registrar_calidad': puede_editar_mi_sesion and grupo_etapa_activa == 'CALIDAD',
        'puede_registrar_almacen': puede_editar_mi_sesion and grupo_etapa_activa == 'ALMACEN',
        'puede_registrar_salida': (
            es_vigilancia and # <--- Agregamos esta condición obligatoria quito es_staff_interno and
             ticket.estado == 'EN_PLANTA' and
            'VIGILANCIA_SALIDA' not in etapas_completadas and
            (
                (es_flujo_calidad and 'CALIDAD_INSPECCION' in etapas_completadas) or
                (not es_flujo_calidad and 'ALMACEN_RECEPCION' in etapas_completadas)
            )
        ),
        'es_flujo_calidad': es_flujo_calidad,
        'coa_completo': coa_completo,
    }

    # ── Agrupación de datos para UI ──
    # 'mi_sesion_agrupada': filas editables de la etapa activa (solo si el gate
    #   de arriba pasa; ver OperationsService.get_mi_sesion) — punto 4.
    # 'estado_actual_agrupada': fila más reciente por línea entre TODAS las
    #   etapas ya ejecutadas, siempre de solo lectura — corrige el bug de
    #   visibilidad de los resultados reales de Calidad (punto 3).
    context = {
        'ticket': ticket,
        'acciones': acciones,
        'etapas_completadas': etapas_completadas,
        'coa_status_agrupado': OperationsService.get_coa_status_por_oc(ticket),
        'mi_sesion_agrupada': OperationsService.get_mi_sesion(ticket) if puede_editar_mi_sesion else {},
        'estado_actual_agrupada': OperationsService.get_estado_actual_por_oc(pk),
    }
    return render(request, 'operations/detalle_ticket.html', context)


@staff_o_proveedor_required
def trazabilidad_ticket(request, pk: int):
    """
    Vista de trazabilidad de solo lectura (Fase 5): TicketStage (tiempos por
    etapa) + el detalle "estado actual" ya construido en la Fase 4
    (OperationsService.get_estado_actual_por_oc — la fila más reciente por
    línea, sin importar la etapa que la generó). Sin acciones ni formularios.

    Destino de scan_qr para roles de solo consulta (incluido el proveedor
    dueño de la cita) y para cualquier rol cuando el ticket ya no tiene una
    acción pendiente para ese usuario (p.ej. FINALIZADO).
    """
    ticket = get_object_or_404(
        Ticket.objects.select_related(
            'appointment__slot',
            'appointment__user',
            'datos_ingreso',
        ).prefetch_related(
            'stages__usuario',
            'appointment__purchase_orders__lines',
        ),
        pk=pk
    )

    # ── Validación de privacidad: mismo criterio que detalle_ticket ──
    es_proveedor = request.user.groups.filter(name='PROVEEDORES').exists()
    if es_proveedor and ticket.appointment.user != request.user:
        return redirect('appointments:portal_proveedor')

    context = {
        'ticket': ticket,
        'estado_actual_agrupada': OperationsService.get_estado_actual_por_oc(pk),
    }
    return render(request, 'operations/trazabilidad_ticket.html', context)


@staff_o_proveedor_required
def imprimir_cargo_pdf(request, pk: int):
    """
    GET /operations/ticket/<pk>/cargo/  (Fase 12)

    Genera el "Cargo de Entrega" en PDF: mismos datos que trazabilidad_ticket
    (ID de ticket, OC(s), proveedor, sede, fecha/hora de la cita, resumen de
    Trazabilidad de Etapas), en un documento simple apto para imprimir o
    enviar. Mismo criterio de propiedad que trazabilidad_ticket — el
    proveedor solo puede generar el cargo de sus propios tickets.

    Se abre inline (Content-Disposition: inline), no como adjunto forzado,
    para que el navegador lo muestre listo para imprimir en una pestaña
    nueva (el link en el Historial de Entregas usa target="_blank").
    """
    ticket = get_object_or_404(
        Ticket.objects.select_related(
            'appointment__slot',
            'appointment__user',
        ).prefetch_related(
            'stages',
            'appointment__purchase_orders',
        ),
        pk=pk
    )

    # ── Validación de privacidad: mismo criterio que detalle_ticket/trazabilidad_ticket ──
    es_proveedor = request.user.groups.filter(name='PROVEEDORES').exists()
    if es_proveedor and ticket.appointment.user != request.user:
        return redirect('appointments:portal_proveedor')

    html = render_to_string('operations/cargo_ticket_pdf.html', {
        'ticket': ticket,
        'etapas': ticket.stages.order_by('fecha_inicio'),
        'fecha_generacion': timezone.now(),
    })

    buffer = BytesIO()
    resultado = pisa.CreatePDF(html, dest=buffer)
    if resultado.err:
        return HttpResponse('No se pudo generar el PDF del cargo.', status=500)

    response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="cargo_ticket_{ticket.id}.pdf"'
    return response
