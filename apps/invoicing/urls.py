# apps/invoicing/urls.py
from django.urls import path

from . import views

app_name = 'invoicing'

urlpatterns = [
    # ── "Copiar de OC(s)" (Sub-fase 3.4) ────────────────────────────────
    path('nueva/', views.nueva_factura_ocs, name='nueva_factura_ocs'),
    path('nueva/copiar/', views.copiar_oc_view, name='copiar_oc'),
    path('nueva/crear/', views.crear_factura_ajax, name='crear_factura'),
    path('mis-facturas/', views.mis_facturas, name='mis_facturas'),
    path('factura/<int:factura_id>/', views.factura_detalle, name='factura_detalle'),
    path(
        'factura/<int:factura_id>/editar/',
        views.editar_cabecera_factura_ajax,
        name='editar_cabecera_factura',
    ),
    path(
        'factura-linea/<int:linea_id>/editar/',
        views.editar_linea_factura_ajax,
        name='editar_linea_factura',
    ),
    path(
        'factura/<int:factura_id>/enviar-a-revision/',
        views.enviar_a_revision_ajax,
        name='enviar_a_revision_factura',
    ),
    path(
        'factura/<int:factura_id>/eliminar/',
        views.eliminar_borrador_factura_ajax,
        name='eliminar_borrador_factura',
    ),

    # ── Lado de Compras (Sub-fase 3.5) ──────────────────────────────────
    path('compras/', views.panel_facturas_compras, name='panel_facturas_compras'),
    path(
        'compras/factura/<int:factura_id>/',
        views.factura_detalle_compras,
        name='factura_detalle_compras',
    ),
    path(
        'compras/factura/<int:factura_id>/aprobar/',
        views.aprobar_factura_ajax,
        name='aprobar_factura',
    ),
    path(
        'compras/factura/<int:factura_id>/observar/',
        views.observar_factura_ajax,
        name='observar_factura',
    ),

    # ── Carga de archivos (Sub-fase 3.2) ────────────────────────────────
    path(
        'factura/<int:factura_id>/archivo/<str:tipo>/',
        views.subir_archivo_factura_ajax,
        name='subir_archivo_factura',
    ),
    path(
        'factura-linea/<int:linea_id>/archivo/<str:tipo>/',
        views.subir_archivo_factura_linea_ajax,
        name='subir_archivo_factura_linea',
    ),
]
