"""
apps/appointments/urls.py
=========================
Rutas del portal del proveedor y gestión de citas.
"""
from django.urls import path
from .views import (
    PortalProveedorView,
    HistorialCitasView,
    solicitar_cita_ajax,
    subir_coa_ajax,
    subir_coa_linea_ajax,
    api_lineas_cita,          # ← nuevo endpoint para panel COA del proveedor
    registrar_datos_ingreso_ajax,
    exportar_historial_citas,
)

app_name = 'appointments'

urlpatterns = [
    # ── Portal del proveedor ─────────────────────────────────────────────
    path('portal/',
         PortalProveedorView.as_view(),
         name='portal_proveedor'),

    path('historial/',
         HistorialCitasView.as_view(),
         name='historial_entregas'),

    path('historial/exportar/<str:formato>/',
         exportar_historial_citas,
         name='exportar_historial_citas'),

    # ── AJAX: solicitar cita ─────────────────────────────────────────────
    path('api/solicitar-cita/',
         solicitar_cita_ajax,
         name='solicitar_cita'),

    # ── AJAX: líneas de una cita (para panel COA del proveedor) ──────────
    path('api/lineas-cita/<int:appointment_id>/',
         api_lineas_cita,
         name='api_lineas_cita'),

    # ── AJAX: cargar COA ─────────────────────────────────────────────────
    path('api/subir-coa/<int:appointment_id>/',
         subir_coa_ajax,
         name='subir_coa'),

    path('<int:appointment_id>/coa/<int:po_line_id>/subir/',
         subir_coa_linea_ajax,
         name='subir_coa_linea'),

    # ── AJAX: datos de ingreso (vehículo/conductor) ───────────────────────
    path('<int:appointment_id>/datos-ingreso/',
         registrar_datos_ingreso_ajax,
         name='registrar_datos_ingreso'),
]
