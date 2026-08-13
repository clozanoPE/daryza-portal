"""
apps/appointments/views.py
==========================
Vistas del portal del proveedor:
  - PortalProveedorView: grilla semanal + historial
  - HistorialCitasView: historial paginado
  - solicitar_cita_ajax: POST — crear solicitud
  - api_lineas_cita: GET — líneas de OC de una cita (para cargar COA)
  - subir_coa_ajax: POST — cargar COA general (legacy)
  - subir_coa_linea_ajax: POST — cargar COA por línea (OneDrive)
"""
from django.views.generic import TemplateView, ListView
from django.http import JsonResponse, HttpResponseBadRequest
from django.utils import timezone
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.http import require_POST
from django.core.exceptions import ValidationError

from .models import Appointment
from .services import SlotService, AppointmentService
from apps.sap_sync.models import PurchaseOrder, PurchaseOrderLine
from apps.base.decorators import proveedor_required
from apps.base.filters import resolver_periodo, resolver_sede
from apps.base.reporting import cita_a_row, exportar_excel, exportar_pdf
from apps.base.models import Sede


def _json_ok(**kwargs):
    return JsonResponse({'status': 'success', **kwargs})


def _json_err(msg, status=400):
    return JsonResponse({'status': 'error', 'msg': msg}, status=status)


# ═══════════════════════════════════════════════════════════════════════════════
# PORTAL DEL PROVEEDOR
# ═══════════════════════════════════════════════════════════════════════════════

@method_decorator(proveedor_required, name='dispatch')
class PortalProveedorView(TemplateView):
    """
    Dashboard principal del proveedor.
    Muestra la grilla semanal de slots y el expediente de citas.
    """
    template_name = 'appointments/portal_proveedor.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        ruc_proveedor = self.request.user.username

        # Sede administrada por la Agenda de Cupos (sesión 48b): la grilla
        # ahora muestra los cupos de UNA sola sede a la vez — ya no un pool
        # único con slots de todas las sedes mezclados. ?sede=CODIGO en la
        # querystring; por defecto LURIN (ver apps.base.filters.resolver_sede).
        sede_actual = resolver_sede(self.request)
        context['sede_actual'] = sede_actual

        # Grilla de 6 días consecutivos desde hoy (ventana rodante, sesión
        # 39) — timezone.localtime() para obtener la fecha de HOY en hora
        # local de Lima, no en UTC crudo (timezone.now() con USE_TZ=True
        # siempre es UTC; usar su .date() directo desfasa la ventana hasta
        # 5 horas antes de medianoche en Lima, mostrando ya el día siguiente).
        if sede_actual:
            context['matrix'], context['dias_semana'] = SlotService.get_semana_matrix(
                timezone.localtime(timezone.now()).date(), sede_actual
            )
        else:
            context['matrix'], context['dias_semana'] = {}, []

        # Vista móvil (Parte C, sesión 45): reagrupa el mismo 'matrix' (sin
        # recalcular datos, mismo SlotService.get_semana_matrix de arriba)
        # por día calendario en vez de por hora, para la lista vertical con
        # selector de día en pantallas <992px. Se agrupa por slot['fecha']
        # real (no por posición en la lista de cada hora), para no heredar
        # el riesgo de desalineación ya documentado en la grilla de
        # escritorio cuando un día no tiene slot en alguna hora específica.
        for dia in context['dias_semana']:
            slots_dia = []
            for hora, columnas in context['matrix'].items():
                for slot in columnas:
                    if slot['fecha'] == dia['fecha']:
                        slots_dia.append({**slot, 'hora': hora})
            slots_dia.sort(key=lambda s: s['hora'])
            dia['slots'] = slots_dia

        # OCs pendientes del proveedor en SAP
        context['ocs_pendientes'] = PurchaseOrder.objects.filter(
            card_code=ruc_proveedor,
            status__in=['O', 'PENDIENTE']
        ).order_by('-created_at')

        # Historial de citas ("Expediente de Citas") con filtros opcionales.
        # Período (mes/año, Fase 10): antes cargaba TODAS las citas del
        # proveedor sin límite; ahora arranca acotado al mes actual, igual
        # que los historiales de Compras/Vigilancia/Calidad.
        q        = self.request.GET.get('q')
        f_estado = self.request.GET.get('estado')
        anio, mes, periodo = resolver_periodo(self.request)

        qs = Appointment.objects.filter(
            user=self.request.user
        ).select_related('slot').prefetch_related(
            'purchase_orders', 'ticket'
        ).filter(
            slot__date__year=anio,
            slot__date__month=mes,
        ).order_by('-created_at')

        if q:
            qs = qs.filter(
                Q(id__icontains=q) |
                Q(purchase_orders__doc_num__icontains=q)
            ).distinct()

        if f_estado:
            qs = qs.filter(status=f_estado)

        # Anotamos los datos de ingreso ya guardados (si existen) para
        # repoblar el formulario del proveedor sin una consulta extra por
        # fila en el template — mismo patrón que ticket.coa_completo en
        # panel_vigilancia (sesión 6).
        for cita in qs:
            ticket = getattr(cita, 'ticket', None)
            cita.datos_ingreso_actual = getattr(ticket, 'datos_ingreso', None) if ticket else None

        context['historial']      = qs
        context['periodo']        = periodo
        context['sedes_daryza']   = Sede.objects.filter(activa=True).order_by('nombre')
        return context


def _historial_citas_qs(request):
    """
    Queryset de citas del proveedor filtrado por q/status/sede — extraído de
    HistorialCitasView.get_queryset (Fase 15, sesión 21) para que
    exportar_historial_citas exporte EXACTAMENTE lo mismo que ve el
    proveedor en pantalla, sin duplicar la lógica de filtrado. Esta vista
    no tiene filtro de período (confirmado sesión 15: fuera del alcance de
    la Fase 10, ya tiene su propia paginación).
    """
    qs = Appointment.objects.filter(
        user=request.user
    ).select_related('slot', 'sede').prefetch_related(
        'purchase_orders', 'ticket'
    ).order_by('-created_at')

    q = request.GET.get('q')
    if q:
        qs = qs.filter(
            Q(id__icontains=q) |
            Q(purchase_orders__doc_num__icontains=q)
        ).distinct()

    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)

    sede = request.GET.get('sede')
    if sede:
        qs = qs.filter(sede__codigo=sede)

    return qs


@method_decorator(proveedor_required, name='dispatch')
class HistorialCitasView(ListView):
    """Historial paginado de citas del proveedor."""
    model                = Appointment
    template_name        = 'appointments/historial_listado.html'
    context_object_name  = 'citas'
    paginate_by          = 10

    def get_queryset(self):
        return _historial_citas_qs(self.request)

    def get_context_data(self, **kwargs):
        """
        Anota 'coa_completo' por cita (columna "Documentos" de la tabla):
        antes leía el campo legacy Appointment.coa_pdf, que la carga de COA
        por línea (Fase 3) nunca llena. Se reemplaza por
        OperationsService.calcular_coa_completo(ticket), la misma fuente
        (TicketLineCOA) ya usada en detalle_ticket/panel_vigilancia.
        None cuando la cita todavía no tiene Ticket (no confirmada).
        """
        context = super().get_context_data(**kwargs)
        from apps.operations.services import OperationsService
        for cita in context['citas']:
            cita.coa_completo = (
                OperationsService.calcular_coa_completo(cita.ticket)
                if hasattr(cita, 'ticket') else None
            )
        return context


@proveedor_required
def exportar_historial_citas(request, formato: str):
    """
    GET /appointments/historial/exportar/<excel|pdf>/  (Fase 15, sesión 21)
    Exporta EXACTAMENTE el mismo listado que ve el proveedor en
    /appointments/historial/ (mismos filtros q/status/sede ya aplicados vía
    querystring), restringido por diseño a sus propias citas
    (_historial_citas_qs ya filtra por user=request.user, igual que
    HistorialCitasView — no se duplica ninguna lógica de permisos nueva).
    """
    citas_qs = _historial_citas_qs(request)
    rows = [cita_a_row(c) for c in citas_qs]
    titulo = f'Historial de Entregas — {request.user.get_full_name() or request.user.username}'
    filename = f'historial_entregas_{request.user.username}'

    if formato == 'excel':
        return exportar_excel(rows, filename, titulo)
    if formato == 'pdf':
        return exportar_pdf(rows, filename, titulo)
    return HttpResponseBadRequest('Formato de reporte no soportado. Use "excel" o "pdf".')


# ═══════════════════════════════════════════════════════════════════════════════
# AJAX — SOLICITAR CITA
# ═══════════════════════════════════════════════════════════════════════════════

@proveedor_required
@require_POST
def solicitar_cita_ajax(request):
    """
    POST /appointments/api/solicitar-cita/
    Crea una solicitud de cita en estado SOLICITADO.
    """
    try:
        slot_id        = request.POST.get('slot_id')
        oc_ids         = request.POST.getlist('oc_ids')
        coa_file       = request.FILES.get('coa_pdf')

        if not slot_id or not oc_ids:
            return _json_err('Debe seleccionar un horario y al menos una Orden de Compra.')

        appointment = AppointmentService.solicitar_cita_borrador(
            user=request.user,
            slot_id=slot_id,
            oc_ids=oc_ids,
            coa_file=coa_file,
        )
        return _json_ok(msg=f'Solicitud #{appointment.id} registrada. Compras la revisará pronto.')

    except ValidationError as e:
        msg = e.messages[0] if hasattr(e, 'messages') else str(e).strip("['']")
        return _json_err(msg)
    except Exception as e:
        return _json_err(f'Error de sistema: {str(e)}', status=500)


# ═══════════════════════════════════════════════════════════════════════════════
# AJAX — LÍNEAS DE CITA (para panel de carga de COA del proveedor)
# ═══════════════════════════════════════════════════════════════════════════════

@proveedor_required
def api_lineas_cita(request, appointment_id: int):
    """
    GET /appointments/api/lineas-cita/<appointment_id>/
    Devuelve las líneas de OC de una cita, siempre a partir de las OCs de SAP
    vinculadas (purchase_orders__lines), con o sin Ticket generado.

    El estado del COA por línea se lee de TicketLineCOA (si ya existe Ticket);
    ya no depende de que Almacén/Vigilancia hayan procesado la cita, porque
    TicketLineInspection ya no se crea al confirmar la cita.
    """
    try:
        appointment = get_object_or_404(
            Appointment.objects.select_related('ticket').prefetch_related('purchase_orders__lines'),
            id=appointment_id,
            user=request.user,
        )

        coas_cargados = {}
        if hasattr(appointment, 'ticket'):
            from apps.operations.models import TicketLineCOA
            coas_cargados = {
                coa.po_line_id: coa.coa_url
                for coa in TicketLineCOA.objects.filter(ticket=appointment.ticket)
            }

        lineas = []
        for po in appointment.purchase_orders.all():
            for line in po.lines.all():
                lineas.append({
                    'po_line_id':   line.id,
                    'oc_num':       po.doc_num,
                    'item_code':    line.item_code,
                    'description':  line.description,
                    'quantity_sap': str(line.quantity_sap),
                    'und_medida':   line.und_medida,
                    'requiere_coa': line.requiere_coa,
                    'coa_url':      coas_cargados.get(line.id, ''),
                })

        return _json_ok(lineas=lineas)

    except Exception as e:
        return _json_err(str(e))
# ═══════════════════════════════════════════════════════════════════════════════
# AJAX — CARGAR COA
# ═══════════════════════════════════════════════════════════════════════════════

@proveedor_required
@require_POST
def subir_coa_ajax(request, appointment_id: int):
    """
    POST /appointments/api/subir-coa/<appointment_id>/
    Carga de COA general a nivel de cita (campo legacy appointment.coa_pdf).
    """
    try:
        appointment = get_object_or_404(
            Appointment, id=appointment_id, user=request.user, status='CONFIRMADA'
        )
        if hasattr(appointment, 'ticket') and appointment.ticket.estado != 'PROGRAMADO':
            return _json_err('No se puede modificar el COA: el proveedor ya ingresó a planta.')

        coa_file = request.FILES.get('coa_pdf')
        if not coa_file:
            return _json_err('Debe adjuntar el archivo COA (PDF).')
        if not coa_file.name.lower().endswith('.pdf'):
            return _json_err('Solo se aceptan archivos PDF.')

        appointment.coa_pdf = coa_file
        appointment.save(update_fields=['coa_pdf'])
        return _json_ok(msg='COA cargado correctamente.', coa_url=appointment.coa_pdf.url)

    except Exception as e:
        return _json_err(str(e))


@proveedor_required
@require_POST
def subir_coa_linea_ajax(request, appointment_id: int, po_line_id: int):
    """
    POST /appointments/<appointment_id>/coa/<po_line_id>/subir/
    Sube el COA de una línea específica a OneDrive y lo registra en BD.
    Máx 5 MB, solo PDF.
    """
    try:
        appointment = get_object_or_404(
            Appointment, id=appointment_id, user=request.user, status='CONFIRMADA'
        )
        if hasattr(appointment, 'ticket') and appointment.ticket.estado != 'PROGRAMADO':
            return _json_err('No se puede modificar el COA: el proveedor ya ingresó a planta.')

        coa_file = request.FILES.get('coa_pdf')
        if not coa_file:
            return _json_err('Debe adjuntar el archivo COA (PDF).')
        if not coa_file.name.lower().endswith('.pdf'):
            return _json_err('Solo se aceptan archivos PDF.')
        if coa_file.size > 5 * 1024 * 1024:
            return _json_err('El archivo supera el límite de 5 MB.')

        po_line = get_object_or_404(PurchaseOrderLine, id=po_line_id)

        # Subir a OneDrive — sede: apps/base/utils.py::OneDriveClient.upload_coa
        # (sesión 48d) separa el árbol de carpetas por sede desde ahora, solo
        # para COAs nuevos (los ya cargados quedan en la ruta vieja sin sede).
        from apps.base.utils import OneDriveClient
        onedrive = OneDriveClient()
        coa_url = onedrive.upload_coa(
            file_obj=coa_file,
            ruc=request.user.username,
            oc_num=str(po_line.purchase_order.doc_num),
            item_code=po_line.item_code,
            sede=appointment.sede.codigo,
        )

        # Registrar en BD
        from apps.operations.services import OperationsService
        OperationsService.registrar_coa_proveedor(
            ticket_id=appointment.ticket.id,
            po_line_id=po_line.id,
            coa_url=coa_url,
            usuario=request.user,
        )

        return _json_ok(
            msg='COA cargado correctamente.',
            coa_url=coa_url,
            po_line_id=po_line_id,
        )

    except Exception as e:
        return _json_err(str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# AJAX — DATOS DE INGRESO (vehículo/conductor, sesión 13)
# ═══════════════════════════════════════════════════════════════════════════════

@proveedor_required
@require_POST
def registrar_datos_ingreso_ajax(request, appointment_id: int):
    """
    POST /appointments/<appointment_id>/datos-ingreso/
    Body (form-encoded): placa_vehiculo, conductor_dni, conductor_nombre

    Registra/actualiza la placa del vehículo y los datos del conductor para
    la visita. Mismo criterio que subir_coa_linea_ajax: editable solo
    mientras el ticket sigue PROGRAMADO (antes de que Vigilancia autorice
    el ingreso). No bloqueante: estos datos son informativos para
    Vigilancia, el sistema no exige que estén completos para guardarlos ni
    para autorizar el ingreso (sesión 13).
    """
    try:
        appointment = get_object_or_404(
            Appointment, id=appointment_id, user=request.user, status='CONFIRMADA'
        )
        if not hasattr(appointment, 'ticket'):
            return _json_err('Esta cita todavía no tiene un ticket generado.')
        if appointment.ticket.estado != 'PROGRAMADO':
            return _json_err('No se pueden modificar los datos de ingreso: el proveedor ya ingresó a planta.')

        placa_vehiculo   = request.POST.get('placa_vehiculo', '').strip()
        conductor_dni    = request.POST.get('conductor_dni', '').strip()
        conductor_nombre = request.POST.get('conductor_nombre', '').strip()

        if not (placa_vehiculo or conductor_dni or conductor_nombre):
            return _json_err('Completa al menos un dato antes de guardar.')

        from apps.operations.services import OperationsService
        OperationsService.registrar_datos_ingreso(
            ticket_id=appointment.ticket.id,
            placa_vehiculo=placa_vehiculo,
            conductor_dni=conductor_dni,
            conductor_nombre=conductor_nombre,
            usuario=request.user,
        )

        return _json_ok(msg='Datos de ingreso guardados correctamente.')

    except Exception as e:
        return _json_err(str(e))
