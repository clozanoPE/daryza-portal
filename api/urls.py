# api/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .sap_api import SAPIntegrationViewSet
from .entrada_mercaderia_api import EntradaMercaderiaViewSet

router = DefaultRouter()
router.register(r'sync-oc', SAPIntegrationViewSet, basename='sync-oc')
router.register(r'entradas-pendientes', EntradaMercaderiaViewSet, basename='entradas-pendientes')

urlpatterns = [
    path('', include(router.urls)),
]