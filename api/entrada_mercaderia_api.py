# api/entrada_mercaderia_api.py
"""
Endpoints del daemon SAP para el ciclo de vida de EntradaMercaderia
(Entrada de Mercancía, sesión 57) — mismo patrón de autenticación
(Token) y de upsert (nunca falla en un reintento) ya usado por
SAPIntegrationViewSet (api/sap_api.py) para la sincronización de OC.

GET  /api/v1/entradas-pendientes/                             -> 'L' pendientes
POST /api/v1/entradas-pendientes/<id>/confirmar-borrador/     -> 'L' -> 'B'
POST /api/v1/entradas-pendientes/<id>/confirmar-definitivo/   -> 'B' -> 'Y'

<id> es EntradaMercaderia.pk (identificador interno consumido solo por el
daemon, nunca mostrado a un usuario — no aplica el criterio de la sesión
56 sobre numeración visible).
"""
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.authentication import TokenAuthentication
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.operations.models import EntradaMercaderia
from apps.operations.serializers import EntradaMercaderiaSerializer


class EntradaMercaderiaViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = EntradaMercaderiaSerializer

    def get_queryset(self):
        qs = EntradaMercaderia.objects.select_related(
            'ticket'
        ).prefetch_related('lineas__po_line__purchase_order').order_by('fecha_generada')
        if self.action == 'list':
            return qs.filter(estado_sap='L')
        return qs

    @action(detail=True, methods=['post'], url_path='confirmar-borrador')
    def confirmar_borrador(self, request, pk=None):
        """
        Marca estado_sap='B' y guarda doc_entry_borrador. Upsert: reintentar
        con el mismo o distinto doc_entry_borrador simplemente lo
        actualiza, sin exigir que el estado previo fuera exactamente 'L'
        (mismo criterio que el sync de OC — el daemon es la fuente de
        verdad de SAP, no hace falta que el Portal valide la secuencia).
        """
        entrada = self.get_object()
        doc_entry = request.data.get('doc_entry_borrador')
        if not doc_entry:
            return Response(
                {'status': 'error', 'errors': {'doc_entry_borrador': ['Este campo es obligatorio.']}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        entrada.doc_entry_borrador = doc_entry
        entrada.estado_sap = 'B'
        entrada.fecha_borrador_confirmado = timezone.now()
        entrada.save(update_fields=[
            'doc_entry_borrador', 'estado_sap', 'fecha_borrador_confirmado',
        ])
        return Response(
            {'status': 'success', 'message': 'Borrador confirmado.'},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['post'], url_path='confirmar-definitivo')
    def confirmar_definitivo(self, request, pk=None):
        """Marca estado_sap='Y' y guarda doc_entry_definitivo. Mismo criterio de upsert."""
        entrada = self.get_object()
        doc_entry = request.data.get('doc_entry_definitivo')
        if not doc_entry:
            return Response(
                {'status': 'error', 'errors': {'doc_entry_definitivo': ['Este campo es obligatorio.']}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        entrada.doc_entry_definitivo = doc_entry
        entrada.estado_sap = 'Y'
        entrada.fecha_definitivo_confirmado = timezone.now()
        entrada.save(update_fields=[
            'doc_entry_definitivo', 'estado_sap', 'fecha_definitivo_confirmado',
        ])
        return Response(
            {'status': 'success', 'message': 'Documento definitivo confirmado.'},
            status=status.HTTP_200_OK,
        )
