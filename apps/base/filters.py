# apps/base/filters.py
"""
Filtro de período (mes/año) reutilizado por los historiales de solo lectura
de Compras, Vigilancia, Calidad y el Portal del Proveedor (Fase 10).

Antes de esta fase, esos historiales cargaban todos los registros sin
límite. resolver_periodo() centraliza la lectura/normalización del
parámetro GET 'periodo' (formato HTML5 <input type="month">, 'YYYY-MM')
y el valor por defecto (mes/año actual), para no repetir esta lógica en
cada vista.

resolver_sede() (sesión 48b) centraliza el mismo patrón para el parámetro
GET 'sede' — usado tanto por el Panel de Administración de Horarios
(apps.scheduling, agenda por sede para Compras/Almacén) como por la Agenda
de Cupos del Portal del Proveedor (apps.appointments) — mismo criterio ya
usado para resolver_periodo: vive en apps.base porque más de una app de
negocio lo necesita.
"""
from django.utils import timezone

from apps.base.models import Sede


def resolver_periodo(request):
    """
    Lee request.GET['periodo'] ('YYYY-MM'). Si falta o es inválido, usa el
    mes/año actual como valor por defecto — así el historial arranca
    acotado sin que el usuario tenga que filtrar manualmente la primera vez.

    Devuelve (anio: int, mes: int, periodo: str), donde periodo siempre
    queda normalizado 'YYYY-MM' (para repoblar el <input type="month">
    del filtro, incluso cuando se usó el valor por defecto).
    """
    hoy = timezone.now().date()
    crudo = request.GET.get('periodo', '').strip()

    if crudo:
        try:
            anio_str, mes_str = crudo.split('-')
            anio, mes = int(anio_str), int(mes_str)
            if 1 <= mes <= 12 and anio >= 2000:
                return anio, mes, f'{anio:04d}-{mes:02d}'
        except (ValueError, TypeError):
            pass

    return hoy.year, hoy.month, hoy.strftime('%Y-%m')


def resolver_sede(request):
    """
    Lee request.GET['sede'] (el `codigo` de una Sede activa). Si falta, no
    coincide con ninguna Sede activa, no hay ninguna Sede activa en
    absoluto (caso extremo, no se produce en la práctica), devuelve LURIN
    si existe y está activa; si no, la primera Sede activa por nombre.

    Devuelve una instancia de Sede o None (solo si no existe ninguna Sede
    activa en el sistema — caller decide cómo manejar ese caso extremo).
    """
    activas = Sede.objects.filter(activa=True)
    codigo = (request.GET.get('sede') or '').strip()

    if codigo:
        sede = activas.filter(codigo=codigo).first()
        if sede:
            return sede

    return activas.filter(codigo='LURIN').first() or activas.order_by('nombre').first()
