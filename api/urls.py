# api/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .sap_api import SAPIntegrationViewSet

router = DefaultRouter()
router.register(r'sync-oc', SAPIntegrationViewSet, basename='sync-oc')

urlpatterns = [
    path('', include(router.urls)),
]