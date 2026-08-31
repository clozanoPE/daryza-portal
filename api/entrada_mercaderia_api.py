# api/entrada_mercaderia_api.py
"""
Endpoints del daemon SAP para el ciclo de vida de EntradaMercaderia
(Entrada de Mercancía, sesión 57) — mismo patrón de autenticación
(Token) y de upsert (nunca falla en un reintento) ya usado por
SAPIntegrationViewSet (api/sap_api.py) para la sincronización de OC.

GET  /api/v1/entradas-pendientes/                             -> estado_sap='L' pendientes
POST /api/v1/entradas-pendientes/<id>/confirmar-borrador/     -> estado_sap 'L' -> 'B'
POST /api/v1/entradas-pendientes/<id>/confirmar-definitivo/   -> estado_sap 'B' -> 'Y', estado -> CREADO_SAP
POST /api/v1/entradas-pendientes/<id>/reportar-error/         -> guarda error_mensaje,
                                                                   NO toca estado/estado_sap
                                                                   (sesión 58)

Sesión 99: confirmar-borrador/confirmar-definitivo aceptan además un
`doc_num` opcional (el DocNum visible del GRPO en SAP B1) que se guarda en
`doc_num_sap`. El daemon lo obtiene síncronamente de la misma respuesta
del POST /PurchaseDeliveryNotes (no hace falta buscarlo después, a
diferencia de Factura). Si el daemon no lo envía, no falla — el campo
queda como estaba. Además, `estado` (de negocio) avanza junto con
`estado_sap`: confirmar-definitivo lo lleva a CREADO_SAP.

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
        # Defensivo (upsert): confirmar-borrador solo llega sobre una
        # entrada que ya fue enviada, así que estado ya debería ser
        # ENVIADO — se fija explícito por si el daemon confirmara sobre
        # una entrada en un estado inesperado.
        entrada.estado = EntradaMercaderia.ESTADO_ENVIADO
        entrada.fecha_borrador_confirmado = timezone.now()
        update_fields = [
            'doc_entry_borrador', 'estado', 'estado_sap', 'fecha_borrador_confirmado',
        ]
        doc_num = request.data.get('doc_num')
        if doc_num not in (None, ''):
            entrada.doc_num_sap = str(doc_num)
            update_fields.append('doc_num_sap')
        entrada.save(update_fields=update_fields)
        return Response(
            {'status': 'success', 'message': 'Borrador confirmado.'},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['post'], url_path='confirmar-definitivo')
    def confirmar_definitivo(self, request, pk=None):
        """
        Marca estado_sap='Y', estado='CREADO_SAP' y guarda
        doc_entry_definitivo (+ doc_num opcional -> doc_num_sap). Mismo
        criterio de upsert que el resto de este archivo.
        """
        entrada = self.get_object()
        doc_entry = request.data.get('doc_entry_definitivo')
        if not doc_entry:
            return Response(
                {'status': 'error', 'errors': {'doc_entry_definitivo': ['Este campo es obligatorio.']}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        entrada.doc_entry_definitivo = doc_entry
        entrada.estado_sap = 'Y'
        entrada.estado = EntradaMercaderia.ESTADO_CREADO_SAP
        entrada.fecha_definitivo_confirmado = timezone.now()
        update_fields = [
            'doc_entry_definitivo', 'estado', 'estado_sap', 'fecha_definitivo_confirmado',
        ]
        doc_num = request.data.get('doc_num')
        if doc_num not in (None, ''):
            entrada.doc_num_sap = str(doc_num)
            update_fields.append('doc_num_sap')
        entrada.save(update_fields=update_fields)
        return Response(
            {'status': 'success', 'message': 'Documento definitivo confirmado.'},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['post'], url_path='reportar-error')
    def reportar_error(self, request, pk=None):
        """
        Guarda error_mensaje SIN tocar estado_sap (sesión 58) — la entrada
        queda tal como estaba (normalmente 'L'), lista para que el daemon
        la reintente en el siguiente ciclo sin que el Portal la marque como
        procesada. No es un estado nuevo del ciclo (''/'L'/'B'/'Y' se
        mantiene igual); error_mensaje es solo diagnóstico.
        """
        entrada = self.get_object()
        mensaje = (request.data.get('error_mensaje') or '').strip()
        if not mensaje:
            return Response(
                {'status': 'error', 'errors': {'error_mensaje': ['Este campo es obligatorio.']}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        entrada.error_mensaje = mensaje
        entrada.save(update_fields=['error_mensaje'])
        return Response(
            {'status': 'success', 'message': 'Error registrado.'},
            status=status.HTTP_200_OK,
        )
