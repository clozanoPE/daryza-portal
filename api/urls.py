# api/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .sap_api import SAPIntegrationViewSet
from .entrada_mercaderia_api import EntradaMercaderiaViewSet
from .factura_api import (
    FacturaCancelacionViewSet,
    FacturaPreliminarViewSet,
    FacturaReconciliacionViewSet,
)

router = DefaultRouter()
router.register(r'sync-oc', SAPIntegrationViewSet, basename='sync-oc')
router.register(r'entradas-pendientes', EntradaMercaderiaViewSet, basename='entradas-pendientes')
router.register(r'facturas-pendientes-preliminar', FacturaPreliminarViewSet, basename='facturas-pendientes-preliminar')
router.register(r'facturas-preliminares', FacturaReconciliacionViewSet, basename='facturas-preliminares')
router.register(r'facturas-pendientes-cancelacion', FacturaCancelacionViewSet, basename='facturas-pendientes-cancelacion')

urlpatterns = [
    path('', include(router.urls)),
]