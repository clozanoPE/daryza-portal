# apps/scheduling/urls.py
#
# RUTAS DE LA APP scheduling — Fase 3
#
# CAPA AFECTADA: core/urls.py debe incluir este archivo bajo el prefijo /scheduling/
#
# Añadir en core/urls.py:
#     path('scheduling/', include('apps.scheduling.urls')),
#
# Todas las rutas requieren is_staff (guard aplicado en cada vista).

from django.urls import path
from . import views

app_name = 'scheduling'

urlpatterns = [
    # Vista principal del panel de horarios
    path(
        'panel/',
        views.panel_horarios,
        name='panel_horarios'
    ),

    # ── Endpoints AJAX (consumidos por panel_horarios.js) ──
    path(
        'api/generar-semana/',
        views.ajax_generar_semana,
        name='ajax_generar_semana'
    ),
    path(
        'api/duplicar-semana/',
        views.ajax_duplicar_semana,
        name='ajax_duplicar_semana'
    ),
    path(
        'api/toggle-bloqueo/',
        views.ajax_toggle_bloqueo,
        name='ajax_toggle_bloqueo'
    ),
    path(
        'api/eliminar-slot/',
        views.ajax_eliminar_slot,
        name='ajax_eliminar_slot'
    ),
    path(
        'api/agregar-slot/',
        views.ajax_agregar_slot,
        name='ajax_agregar_slot'
    ),
    path(
        'api/citas-slot/<int:slot_id>/',
        views.ajax_get_citas_slot,
        name='ajax_get_citas_slot'
    ),
]
