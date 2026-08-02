# apps/base/filters.py
"""
Filtro de período (mes/año) reutilizado por los historiales de solo lectura
de Compras, Vigilancia, Calidad y el Portal del Proveedor (Fase 10).

Antes de esta fase, esos historiales cargaban todos los registros sin
límite. resolver_periodo() centraliza la lectura/normalización del
parámetro GET 'periodo' (formato HTML5 <input type="month">, 'YYYY-MM')
y el valor por defecto (mes/año actual), para no repetir esta lógica en
cada vista.
"""
from django.utils import timezone


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
