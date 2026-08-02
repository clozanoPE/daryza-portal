# apps/sap_sync/sap_api.py
from rest_framework import status, viewsets
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.authentication import TokenAuthentication
from apps.sap_sync.serializers import PurchaseOrderSerializer
from apps.sap_sync.models import PurchaseOrder

class SAPIntegrationViewSet(viewsets.GenericViewSet):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = PurchaseOrderSerializer

    def create(self, request):
        """
        Endpoint PRO: Recibe OC desde el demonio VB.NET.
        Soporta carga masiva o individual.
        """
        serializer = self.get_serializer(data=request.data, many=isinstance(request.data, list))
        
        if serializer.is_valid():
            serializer.save()
            return Response(
                {"status": "success", "message": "Sincronización SAP exitosa"},
                status=status.HTTP_201_CREATED
            )
        
        return Response(
            {"status": "error", "errors": serializer.errors},
            status=status.HTTP_400_BAD_REQUEST
        )