"""
apps/operations/urls.py
=======================
Rutas de la app operations.

Incluir en core/urls.py:
    path('operations/', include('apps.operations.urls', namespace='operations')),

Convención de nombres:
  - Paneles: panel_<area>
  - AJAX GET: ajax_get_<recurso>
  - AJAX POST: ajax_<verbo>_<recurso>
"""
from django.urls import path
from . import views

app_name = 'operations'

urlpatterns = [

    # ── Panel Compras ────────────────────────────────────────────────────
    path('compras/',
         views.panel_compras,
         name='panel_compras'),

    path('api/compras/lineas/<int:appointment_id>/',
         views.ajax_get_lineas_oc,
         name='ajax_get_lineas_oc'),

    path('api/configurar-items-coa/',
         views.ajax_configurar_items_coa,
         name='ajax_configurar_items_coa'),

    path('api/compras/confirmar-cita/',
         views.ajax_confirmar_cita_compras,
         name='ajax_confirmar_cita_compras'),

    path('api/compras/rechazar-cita/',
         views.ajax_rechazar_cita_compras,
         name='ajax_rechazar_cita_compras'),

    # ── Panel Almacén ────────────────────────────────────────────────────
    path('almacen/',
         views.panel_almacen,
         name='panel_almacen'),

    path('api/confirmar-cita/',
         views.ajax_confirmar_cita,
         name='ajax_confirmar_cita'),

    path('api/rechazar-cita/',
         views.ajax_rechazar_cita,
         name='ajax_rechazar_cita'),

    path('api/autorizar-almacen/',
         views.ajax_autorizar_almacen,
         name='ajax_autorizar_almacen'),

    # ── Panel Vigilancia ─────────────────────────────────────────────────
    path('vigilancia/',
         views.panel_vigilancia,
         name='panel_vigilancia'),

    path('vigilancia/scan/',
         views.scan_qr,
         name='scan_qr'),

    path('api/autorizar-ingreso/',
         views.ajax_autorizar_ingreso,
         name='ajax_autorizar_ingreso'),

    path('api/registrar-salida/',
         views.ajax_registrar_salida,
         name='ajax_registrar_salida'),

    # ── Panel Calidad ────────────────────────────────────────────────────
    path('calidad/',
         views.panel_calidad,
         name='panel_calidad'),

    path('api/registrar-inspeccion/',
         views.ajax_registrar_inspeccion,
         name='ajax_registrar_inspeccion'),

    # ── Detalle del Ticket ───────────────────────────────────────────────
    path('ticket/<int:pk>/',
         views.detalle_ticket,
         name='detalle_ticket'),

    path('ticket/<int:pk>/trazabilidad/',
         views.trazabilidad_ticket,
         name='trazabilidad_ticket'),

    path('ticket/<int:pk>/cargo/',
         views.imprimir_cargo_pdf,
         name='imprimir_cargo'),
]
