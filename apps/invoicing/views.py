# apps/invoicing/views.py
"""
Vistas de apps.invoicing.

1. Carga de archivos para Factura/FacturaLinea (Sub-fase 3.2, sin cambios
   en esta sesión) — subir_archivo_factura_ajax/subir_archivo_factura_
   linea_ajax, 2 endpoints genéricos parametrizados por `tipo`.

2. Sub-fase 3.4: pantalla completa donde el proveedor arma una Factura
   nueva, patrón "Copiar de OC(s)" —
     nueva_factura_ocs   GET  /invoicing/nueva/            Paso 1: listado
     copiar_oc_view      GET  /invoicing/nueva/copiar/     Paso 2: copia
     crear_factura_ajax  POST /invoicing/nueva/crear/      Paso 3: crear
     mis_facturas        GET  /invoicing/mis-facturas/     listado propio
     factura_detalle     GET  /invoicing/factura/<id>/     detalle+carga
     editar_cabecera_factura_ajax  POST .../editar/
     editar_linea_factura_ajax     POST .../editar/
     enviar_a_revision_ajax        POST .../enviar-a-revision/

3. Sub-fase 3.5 (esta sesión): el lado de Compras —
     panel_facturas_compras    GET  /invoicing/compras/                listado
     factura_detalle_compras   GET  /invoicing/compras/factura/<id>/   detalle
     aprobar_factura_ajax      POST .../aprobar/
     observar_factura_ajax     POST .../observar/

   factura_detalle_compras reutiliza LA MISMA plantilla que factura_
   detalle (proveedor) — nunca se duplicó el layout (pedido explícito) —
   pasando `es_compras=True` y `puede_editar=False` siempre (Compras no
   edita contenido, solo aprueba/observa; los campos ya quedan de solo
   lectura reutilizando el mismo `puede_editar` que ya gatea inputs/
   botones de carga en el template, sin necesitar un segundo camino de
   renderizado).

   Toda la orquestación de negocio vive en services_borrador.py/
   services.py — estas vistas solo traducen request<->servicio, mismo
   principio "fat services, thin views" ya establecido en todo el proyecto.

Ambas vistas de archivo/edición del proveedor (puntos 1-2) filtran el
objeto por dueño en el propio queryset (`proveedor__user=request.user`,
404 si no coincide) ADEMÁS del candado que ya vive en el servicio
(services_archivos.validar_permiso_edicion) — defensa en profundidad,
mismo patrón ya usado en subir_coa_linea_ajax (apps/appointments/views.py).
Las 4 vistas de Compras (punto 3) están protegidas por @compras_required
(redirect a login si el rol no corresponde — mismo patrón ya usado por
panel_compras/ajax_confirmar_cita_compras, no el patrón "403 explícito"
reservado para endpoints compartidos entre varios roles internos) Y el
candado de InvoicingService._validar_permiso_compras dentro del servicio
— defensa en profundidad, pedido explícito.
"""
import json
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db.models import Prefetch
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.base.decorators import compras_required, proveedor_required
from apps.base.filters import resolver_periodo
from apps.base.supplier_sync import resolver_perfil_de_usuario
from apps.sap_sync.models import PurchaseOrderLine

from . import services_archivos as sa
from . import services_borrador as sb
from .models import Factura, FacturaLinea
from .services import InvoicingService


def _json_ok(data: dict = None, **kwargs) -> JsonResponse:
    payload = {'status': 'success', **(data or {}), **kwargs}
    return JsonResponse(payload)


def _json_err(msg: str, status: int = 400) -> JsonResponse:
    return JsonResponse({'status': 'error', 'msg': msg}, status=status)


def _mensaje_de(e: ValidationError) -> str:
    return '; '.join(e.messages) if hasattr(e, 'messages') else str(e)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Carga de archivos (Sub-fase 3.2, sin cambios de esta sesión)
# ═══════════════════════════════════════════════════════════════════════════

@proveedor_required
@require_POST
def subir_archivo_factura_ajax(request, factura_id: int, tipo: str):
    """
    POST /invoicing/factura/<factura_id>/archivo/<tipo>/
    Body: multipart/form-data, campo 'archivo'.
    tipo ∈ {'xml', 'pdf', 'cdr'} — ver services_archivos.CONFIG_FACTURA.
    """
    factura = get_object_or_404(Factura, id=factura_id, proveedor__user=request.user)

    archivo = request.FILES.get('archivo')
    if not archivo:
        return _json_err('Debe adjuntar un archivo.')

    try:
        sa.cargar_archivo_factura(factura, tipo, archivo, request.user)
    except ValidationError as e:
        return _json_err(_mensaje_de(e))

    return _json_ok(msg='Archivo cargado correctamente.', tipo=tipo, factura_id=factura.id)


@proveedor_required
@require_POST
def subir_archivo_factura_linea_ajax(request, linea_id: int, tipo: str):
    """
    POST /invoicing/factura-linea/<linea_id>/archivo/<tipo>/
    Body: multipart/form-data, campo 'archivo'.
    tipo ∈ {'retencion', 'detraccion'} — ver services_archivos.CONFIG_FACTURA_LINEA.
    """
    linea = get_object_or_404(
        FacturaLinea, id=linea_id, factura__proveedor__user=request.user,
    )

    archivo = request.FILES.get('archivo')
    if not archivo:
        return _json_err('Debe adjuntar un archivo.')

    try:
        sa.cargar_archivo_factura_linea(linea, tipo, archivo, request.user)
    except ValidationError as e:
        return _json_err(_mensaje_de(e))

    return _json_ok(msg='Archivo cargado correctamente.', tipo=tipo, linea_id=linea.id)


# ═══════════════════════════════════════════════════════════════════════════
# 2. Sub-fase 3.4 — "Copiar de OC(s)"
# ═══════════════════════════════════════════════════════════════════════════

def _parse_int_list(valores):
    resultado = []
    for v in valores:
        try:
            resultado.append(int(v))
        except (TypeError, ValueError):
            continue
    return resultado


def _parsear_cabecera(data: dict) -> dict:
    """
    Normaliza los 8 campos de cabecera editables de Factura desde un dict
    plano — usado tanto por crear_factura_ajax (request.POST, multipart)
    como por editar_cabecera_factura_ajax (json.loads(request.body)):
    ambos son dict-like con .get(), la misma función sirve para los 2.
    Lanza ValueError (no ValidationError — es un error de PARSEO, antes
    de llegar a ninguna regla de negocio) si algún valor no se puede
    interpretar.
    """
    def _texto(clave, maxlen=None):
        val = (data.get(clave) or '').strip()
        if maxlen:
            val = val[:maxlen]
        return val or None

    tipo_operacion = data.get('tipo_operacion') or '02'
    if tipo_operacion not in dict(Factura.TIPO_OPERACION_CHOICES):
        raise ValueError('Tipo de Operación inválido.')

    clasificacion_raw = data.get('clasificacion_bienes_servicios')
    clasificacion = None
    if clasificacion_raw not in (None, ''):
        try:
            clasificacion = int(clasificacion_raw)
        except (TypeError, ValueError):
            raise ValueError('Clasificación de Bienes/Servicios inválida.')
        if clasificacion not in dict(Factura.CLASIFICACION_BIENES_SERVICIOS_CHOICES):
            raise ValueError('Clasificación de Bienes/Servicios inválida.')

    return {
        'doc_cur': _texto('doc_cur', 50),
        'tax_date': _texto('tax_date'),
        'doc_due_date': _texto('doc_due_date'),
        'num_at_card': _texto('num_at_card', 100),
        'serie_comprobante': _texto('serie_comprobante', 10),
        'numero_comprobante': _texto('numero_comprobante', 20),
        'tipo_operacion': tipo_operacion,
        'clasificacion_bienes_servicios': clasificacion,
    }


@proveedor_required
@require_GET
def nueva_factura_ocs(request):
    """Paso 1: listado de OC elegibles del proveedor logueado, con checkbox múltiple."""
    try:
        perfil = resolver_perfil_de_usuario(request.user)
    except ValidationError as e:
        perfil = None
        error_perfil = _mensaje_de(e)
    else:
        error_perfil = None

    ocs_elegibles = sb.listar_ocs_elegibles(perfil) if perfil else []

    return render(request, 'invoicing/nueva_factura_ocs.html', {
        'ocs_elegibles': ocs_elegibles,
        'sin_perfil': perfil is None,
        'error_perfil': error_perfil,
        'error_oc_no_disponible': request.GET.get('error') == 'oc_no_disponible',
    })


@proveedor_required
@require_GET
def copiar_oc_view(request):
    """Paso 2: pantalla de copia — SIN ningún guardado en BD todavía."""
    try:
        perfil = resolver_perfil_de_usuario(request.user)
    except ValidationError:
        perfil = None
    if perfil is None:
        return redirect('invoicing:nueva_factura_ocs')

    oc_ids_raw = request.GET.getlist('oc_ids')
    elegibles = {e['purchase_order'].id: e for e in sb.listar_ocs_elegibles(perfil)}

    seleccionadas = []
    omitidas_count = 0
    for oc_id in _parse_int_list(oc_ids_raw):
        entry = elegibles.get(oc_id)
        if entry:
            seleccionadas.append(entry)
        else:
            omitidas_count += 1

    if not seleccionadas:
        return redirect(f"{reverse('invoicing:nueva_factura_ocs')}?error=oc_no_disponible")

    sedes_distintas = {e['sede'].id for e in seleccionadas}
    error_sede = None
    grupos = []
    if len(sedes_distintas) > 1:
        error_sede = (
            "Las OC seleccionadas pertenecen a sedes distintas — no se pueden "
            "combinar en una misma Factura. Vuelva y seleccione solo OC de la misma sede."
        )
    else:
        purchase_orders = [e['purchase_order'] for e in seleccionadas]
        grupos = sb.lineas_para_copiar(purchase_orders)

    return render(request, 'invoicing/nueva_factura_copiar.html', {
        'grupos': grupos,
        'sede': seleccionadas[0]['sede'] if not error_sede else None,
        'omitidas_count': omitidas_count,
        'error_sede': error_sede,
        'tipos_operacion': Factura.TIPO_OPERACION_CHOICES,
        'clasificaciones': Factura.CLASIFICACION_BIENES_SERVICIOS_CHOICES,
    })


@proveedor_required
@require_POST
def crear_factura_ajax(request):
    """Paso 3: POST /invoicing/nueva/crear/ — el primer guardado real en BD."""
    try:
        perfil = resolver_perfil_de_usuario(request.user)
    except ValidationError as e:
        return _json_err(_mensaje_de(e))
    if perfil is None:
        return _json_err('No existe un perfil de proveedor vinculado a su cuenta.')

    oc_ids = _parse_int_list(request.POST.getlist('oc_ids'))
    if not oc_ids:
        return _json_err('Debe seleccionar al menos una Orden de Compra.')

    try:
        cabecera = _parsear_cabecera(request.POST)
    except ValueError as e:
        return _json_err(str(e))

    po_lines_por_id = {
        pl.id: pl for pl in PurchaseOrderLine.objects.filter(
            purchase_order_id__in=oc_ids, activa=True,
        ).select_related('purchase_order')
    }

    lineas_payload = []
    for po_line_id, po_line in po_lines_por_id.items():
        cantidad_raw = request.POST.get(f'cantidad_{po_line_id}')
        if not cantidad_raw:
            continue
        try:
            cantidad = Decimal(cantidad_raw)
        except InvalidOperation:
            return _json_err(f'Cantidad inválida para la línea {po_line.item_code}.')
        if cantidad <= 0:
            continue

        precio_raw = request.POST.get(f'precio_{po_line_id}')
        try:
            precio = Decimal(precio_raw) if precio_raw else Decimal('0')
        except InvalidOperation:
            return _json_err(f'Precio inválido para la línea {po_line.item_code}.')

        lineas_payload.append({
            'po_line': po_line,
            'cantidad': cantidad,
            'precio': precio,
            'aplica_retencion': request.POST.get(f'aplica_retencion_{po_line_id}') == 'on',
            'aplica_detraccion': request.POST.get(f'aplica_detraccion_{po_line_id}') == 'on',
            'archivo_retencion': request.FILES.get(f'archivo_retencion_{po_line_id}'),
            'archivo_detraccion': request.FILES.get(f'archivo_detraccion_{po_line_id}'),
        })

    try:
        factura = sb.crear_factura_desde_ocs(
            proveedor=perfil, purchase_order_ids=oc_ids,
            cabecera=cabecera, lineas_payload=lineas_payload, usuario=request.user,
        )
    except ValidationError as e:
        return _json_err(_mensaje_de(e))

    return _json_ok(msg='Factura creada correctamente.', factura_id=factura.id)


@proveedor_required
@require_GET
def mis_facturas(request):
    facturas = Factura.objects.filter(
        proveedor__user=request.user,
    ).select_related('sede').prefetch_related(
        'ordenes_compra__purchase_order', 'observaciones',
    ).order_by('-created_at')
    return render(request, 'invoicing/mis_facturas.html', {'facturas': facturas})


@proveedor_required
@require_GET
def factura_detalle(request, factura_id: int):
    factura = get_object_or_404(
        Factura.objects.select_related('proveedor', 'sede').prefetch_related(
            Prefetch(
                'lineas',
                queryset=FacturaLinea.objects.select_related(
                    'po_line', 'po_line__purchase_order',
                ).order_by('po_line__purchase_order_id', 'po_line__line_num'),
            ),
            'ordenes_compra__purchase_order',
            'observaciones',
        ),
        id=factura_id, proveedor__user=request.user,
    )
    puede_editar = factura.estado in sa.ESTADOS_CARGA_PERMITIDA

    return render(request, 'invoicing/factura_detalle.html', {
        'factura': factura,
        'puede_editar': puede_editar,
        'es_compras': False,
        'puede_actuar_compras': False,
        'archivos': [
            ('xml', 'XML de la Factura', factura.xml_file),
            ('pdf', 'PDF de la Factura', factura.pdf_file),
            ('cdr', 'CDR de SUNAT', factura.cdr_xml_file),
        ],
        'tipos_operacion': Factura.TIPO_OPERACION_CHOICES,
        'clasificaciones': Factura.CLASIFICACION_BIENES_SERVICIOS_CHOICES,
        'volver_url': reverse('invoicing:mis_facturas'),
        'volver_label': 'Mis Facturas',
    })


@proveedor_required
@require_POST
def editar_cabecera_factura_ajax(request, factura_id: int):
    factura = get_object_or_404(Factura, id=factura_id, proveedor__user=request.user)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _json_err('JSON inválido.')

    try:
        cabecera = _parsear_cabecera(data)
    except ValueError as e:
        return _json_err(str(e))

    try:
        sb.editar_cabecera_factura(factura, request.user, cabecera)
    except ValidationError as e:
        return _json_err(_mensaje_de(e))

    return _json_ok(msg='Cabecera actualizada correctamente.')


@proveedor_required
@require_POST
def editar_linea_factura_ajax(request, linea_id: int):
    linea = get_object_or_404(
        FacturaLinea.objects.select_related('po_line', 'factura'),
        id=linea_id, factura__proveedor__user=request.user,
    )

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _json_err('JSON inválido.')

    kwargs = {}
    if 'cantidad' in data:
        try:
            kwargs['cantidad'] = Decimal(str(data['cantidad']))
        except InvalidOperation:
            return _json_err('Cantidad inválida.')
    if 'precio' in data:
        try:
            kwargs['precio'] = Decimal(str(data['precio']))
        except InvalidOperation:
            return _json_err('Precio inválido.')
    if 'aplica_retencion' in data:
        kwargs['aplica_retencion'] = bool(data['aplica_retencion'])
    if 'aplica_detraccion' in data:
        kwargs['aplica_detraccion'] = bool(data['aplica_detraccion'])

    try:
        sb.editar_linea_factura(linea, request.user, **kwargs)
    except ValidationError as e:
        return _json_err(_mensaje_de(e))

    return _json_ok(msg='Línea actualizada correctamente.')


@proveedor_required
@require_POST
def enviar_a_revision_ajax(request, factura_id: int):
    """
    POST /invoicing/factura/<factura_id>/enviar-a-revision/ — punto 4 del
    pedido: llama a InvoicingService.enviar_a_revision tal cual (candado
    ya construido en la Sub-fase 3.3); si falla, el mensaje devuelto por
    el servicio se propaga sin modificar ni ocultar el motivo.
    """
    factura = get_object_or_404(Factura, id=factura_id, proveedor__user=request.user)
    try:
        InvoicingService.enviar_a_revision(factura)
    except ValidationError as e:
        return _json_err(_mensaje_de(e))
    return _json_ok(msg='Factura enviada a revisión correctamente.')


# ═══════════════════════════════════════════════════════════════════════════
# 3. Sub-fase 3.5 — lado de Compras
# ═══════════════════════════════════════════════════════════════════════════

ESTADOS_FILTRO_COMPRAS = [
    ('EN_REVISION_COMPRAS', 'En Revisión (Compras)'),
    ('OBSERVADA', 'Observada'),
    ('APROBADA_COMPRAS', 'Aprobada (Compras)'),
    ('CANCELADO', 'Cancelado'),
]


@compras_required
@require_GET
def panel_facturas_compras(request):
    """
    Listado de Facturas para Compras (punto 1 del pedido): EN_REVISION_
    COMPRAS por defecto, con opción de ver OBSERVADA/APROBADA_COMPRAS/
    CANCELADO vía el parámetro `estado`. Filtro de período (mes actual
    por defecto) sobre `created_at` — mismo patrón ya usado por
    apps.base.filters.resolver_periodo en el Panel de Consulta de OC y
    los Historiales de citas; Factura no tiene una única "fecha de cita"
    propia (puede vincular varias OC, cada una con su Ticket/Appointment
    distinto), así que created_at (cuándo se creó/envió la Factura misma)
    es el único campo de fecha que TODA Factura tiene sin ambigüedad.
    """
    anio, mes, periodo = resolver_periodo(request)

    estado_filtro = request.GET.get('estado') or 'EN_REVISION_COMPRAS'
    if estado_filtro not in dict(ESTADOS_FILTRO_COMPRAS):
        estado_filtro = 'EN_REVISION_COMPRAS'

    facturas = Factura.objects.filter(
        estado=estado_filtro, created_at__year=anio, created_at__month=mes,
    ).select_related('proveedor', 'sede').prefetch_related(
        'ordenes_compra__purchase_order',
    ).order_by('-created_at')

    return render(request, 'invoicing/panel_facturas_compras.html', {
        'facturas': facturas,
        'periodo': periodo,
        'estado_filtro': estado_filtro,
        'estados_filtro': ESTADOS_FILTRO_COMPRAS,
    })


@compras_required
@require_GET
def factura_detalle_compras(request, factura_id: int):
    """
    Detalle de Factura para Compras — MISMA plantilla que factura_detalle
    (proveedor), ver docstring del módulo. Sin filtro de dueño (Compras
    revisa Facturas de cualquier proveedor) — a diferencia de
    factura_detalle, que sí filtra por `proveedor__user=request.user`.
    """
    factura = get_object_or_404(
        Factura.objects.select_related('proveedor', 'sede').prefetch_related(
            Prefetch(
                'lineas',
                queryset=FacturaLinea.objects.select_related(
                    'po_line', 'po_line__purchase_order',
                ).order_by('po_line__purchase_order_id', 'po_line__line_num'),
            ),
            'ordenes_compra__purchase_order',
            'observaciones',
        ),
        id=factura_id,
    )

    return render(request, 'invoicing/factura_detalle.html', {
        'factura': factura,
        'puede_editar': False,
        'es_compras': True,
        'puede_actuar_compras': factura.estado == 'EN_REVISION_COMPRAS',
        'archivos': [
            ('xml', 'XML de la Factura', factura.xml_file),
            ('pdf', 'PDF de la Factura', factura.pdf_file),
            ('cdr', 'CDR de SUNAT', factura.cdr_xml_file),
        ],
        'tipos_operacion': Factura.TIPO_OPERACION_CHOICES,
        'clasificaciones': Factura.CLASIFICACION_BIENES_SERVICIOS_CHOICES,
        'volver_url': reverse('invoicing:panel_facturas_compras'),
        'volver_label': 'Facturas Pendientes',
    })


@compras_required
@require_POST
def aprobar_factura_ajax(request, factura_id: int):
    """
    POST /invoicing/compras/factura/<factura_id>/aprobar/ — punto 3 del
    pedido. Sin filtro de dueño (a propósito, ver factura_detalle_compras).
    InvoicingService.aprobar_factura re-valida firma/CDR/importe por su
    cuenta (defensa en profundidad) y el propio candado de rol — no se
    confía en que @compras_required ya lo haya garantizado.
    """
    factura = get_object_or_404(Factura, id=factura_id)
    try:
        InvoicingService.aprobar_factura(factura, request.user)
    except ValidationError as e:
        return _json_err(_mensaje_de(e))
    return _json_ok(msg='Factura aprobada correctamente. Queda lista para sincronizar con SAP.')


@compras_required
@require_POST
def observar_factura_ajax(request, factura_id: int):
    """
    POST /invoicing/compras/factura/<factura_id>/observar/ — punto 3 del
    pedido. Exige 'texto' en el body JSON (candado real vive en
    InvoicingService.observar_factura, no aquí — esto es solo parseo).
    Si el correo de notificación al proveedor falla, se incluye
    'email_error' en la respuesta (sin bloquear la operación — la
    Factura ya quedó OBSERVADA) para que el chat/toast de Compras lo deje
    visible, mismo criterio ya usado con Ticket.email_notificacion_error
    (apps.appointments.views, sesión 73).
    """
    factura = get_object_or_404(Factura, id=factura_id)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _json_err('JSON inválido.')

    try:
        InvoicingService.observar_factura(factura, request.user, data.get('texto', ''))
    except ValidationError as e:
        return _json_err(_mensaje_de(e))

    resultado = {'msg': 'Factura observada correctamente.'}
    email_error = getattr(factura, '_email_observacion_error', None)
    if email_error:
        resultado['email_error'] = email_error
    return _json_ok(**resultado)
