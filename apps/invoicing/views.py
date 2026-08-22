# apps/invoicing/views.py
"""
Endpoints de carga de archivos para Factura/FacturaLinea.

2 endpoints genéricos parametrizados por `tipo` (uno por nivel: Factura
para xml/pdf/cdr, FacturaLinea para retencion/detraccion) en vez de 5
vistas casi idénticas — la única diferencia real entre los "tipos" de un
mismo nivel ya vive centralizada en services_archivos.CONFIG_FACTURA/
CONFIG_FACTURA_LINEA; una vista por tipo solo duplicaría el mismo cuerpo
3 (o 2) veces sin ganar nada.

Ambas vistas filtran el objeto por dueño en el propio queryset
(`proveedor__user=request.user`, 404 si no coincide — no revela si la
Factura existe a alguien que no es su dueño) ADEMÁS del candado que ya
vive en el servicio (services_archivos._validar_permiso_carga) — defensa
en profundidad, mismo patrón ya usado en subir_coa_linea_ajax
(apps/appointments/views.py).
"""
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from apps.base.decorators import proveedor_required

from . import services_archivos as sa
from .models import Factura, FacturaLinea


def _json_ok(data: dict = None, **kwargs) -> JsonResponse:
    payload = {'status': 'success', **(data or {}), **kwargs}
    return JsonResponse(payload)


def _json_err(msg: str, status: int = 400) -> JsonResponse:
    return JsonResponse({'status': 'error', 'msg': msg}, status=status)


def _mensaje_de(e: ValidationError) -> str:
    return '; '.join(e.messages) if hasattr(e, 'messages') else str(e)


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
