# apps/invoicing/urls.py
from django.urls import path

from . import views

app_name = 'invoicing'

urlpatterns = [
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
