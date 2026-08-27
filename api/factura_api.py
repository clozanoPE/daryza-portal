# api/factura_api.py
"""
Endpoints del daemon SAP para el ciclo de vida de Factura en SAP B1
(Sub-fase 3.6) — mismo patrón de autenticación (Token) y de upsert
retry-safe ya usado por SAPIntegrationViewSet (api/sap_api.py, sync de
OC) y EntradaMercaderiaViewSet (api/entrada_mercaderia_api.py, sesión
57-58). El demonio SIEMPRE inicia la conexión, nunca al revés.

3 ViewSets, uno por etapa del ciclo (cada uno con su propio filtro de
"pendientes" en `list`, mismo criterio ya usado en EntradaMercaderia
donde `get_queryset` distingue `self.action == 'list'` de las acciones
de detalle):

  FacturaPreliminarViewSet
    GET  /api/v1/facturas-pendientes-preliminar/                     -> 'L' pendientes (APROBADA_COMPRAS + estado_sap='L')
    POST .../<id>/confirmar-preliminar/                               -> 'L' -> 'B', guarda doc_entry_preliminar
    POST .../<id>/reportar-error/                                     -> guarda error_mensaje_sap, NO toca estado_sap

  FacturaReconciliacionViewSet (polling menos frecuente — Contabilidad
  crea el definitivo por su cuenta en SAP, el daemon solo confirma)
    GET  /api/v1/facturas-preliminares/                               -> estado_sap='B'
    POST .../<id>/confirmar-definitivo/                               -> 'B' -> 'Y', guarda doc_entry_definitivo

  FacturaCancelacionViewSet
    GET  /api/v1/facturas-pendientes-cancelacion/                     -> estado=CANCELADO + estado_sap='B'
    POST .../<id>/confirmar-cancelacion/                              -> 'B' -> 'C'

<id> es Factura.pk (identificador interno consumido solo por el daemon,
nunca mostrado a un usuario — no aplica el criterio de la sesión 56
sobre numeración visible).

INTERRUPTOR DE SEGURIDAD (pedido explícito, punto 6): `get_queryset` de
los 3 ViewSets devuelve un queryset VACÍO cuando `settings.
FACTURA_DRAFT_SAP_HABILITADO` es False (el valor por defecto y el actual
en producción) — sin importar la acción (`list` o una de detalle), no
solo el listado de "pendientes". Esto bloquea también los POST de
confirmación mientras el flag esté apagado (un intento de confirmar
sobre un `id` real responde 404, como si la Factura no existiera) —
interpretación deliberadamente conservadora de "TODOS estos endpoints
deben respetar" el interruptor, ya que el daemon nunca debería llegar a
confirmar algo que la lista de pendientes nunca le ofreció, pero no hay
motivo para dejar una vía de escape si el flag se apaga mientras el
daemon ya tiene un `id` en memoria de un ciclo anterior.
"""
from django.conf import settings
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.authentication import TokenAuthentication
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.invoicing.models import Factura
from apps.invoicing.serializers import (
    FacturaCancelacionSerializer,
    FacturaPreliminarSerializer,
    FacturaReconciliacionSerializer,
)


def _flag_habilitado():
    return settings.FACTURA_DRAFT_SAP_HABILITADO


class FacturaPreliminarViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = FacturaPreliminarSerializer

    def get_queryset(self):
        if not _flag_habilitado():
            return Factura.objects.none()

        qs = Factura.objects.select_related('proveedor', 'sede').prefetch_related(
            'lineas__po_line__purchase_order',
        ).order_by('created_at')
        if self.action == 'list':
            return qs.filter(estado='APROBADA_COMPRAS', estado_sap='L')
        return qs

    @action(detail=True, methods=['post'], url_path='confirmar-preliminar')
    def confirmar_preliminar(self, request, pk=None):
        """
        Marca estado_sap='B' y guarda doc_entry_preliminar. Upsert: un
        reintento con el mismo o distinto doc_entry_preliminar solo
        actualiza — mismo criterio que EntradaMercaderia.confirmar_
        borrador (sesión 57), no exige que el estado previo fuera
        exactamente 'L'.
        """
        factura = self.get_object()
        doc_entry = request.data.get('doc_entry_preliminar')
        if not doc_entry:
            return Response(
                {'status': 'error', 'errors': {'doc_entry_preliminar': ['Este campo es obligatorio.']}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        factura.doc_entry_preliminar = doc_entry
        factura.estado_sap = 'B'
        factura.fecha_preliminar_confirmado = timezone.now()
        factura.save(update_fields=[
            'doc_entry_preliminar', 'estado_sap', 'fecha_preliminar_confirmado', 'updated_at',
        ])
        return Response(
            {'status': 'success', 'message': 'Preliminar confirmado.'},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['post'], url_path='reportar-error')
    def reportar_error(self, request, pk=None):
        """
        Guarda error_mensaje_sap SIN tocar estado_sap (mismo patrón que
        EntradaMercaderia.reportar_error, sesión 58) — la Factura queda
        tal como estaba (normalmente 'L'), lista para que el daemon la
        reintente en el siguiente ciclo sin que el Portal la marque como
        procesada.
        """
        factura = self.get_object()
        mensaje = (request.data.get('error_mensaje') or '').strip()
        if not mensaje:
            return Response(
                {'status': 'error', 'errors': {'error_mensaje': ['Este campo es obligatorio.']}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        factura.error_mensaje_sap = mensaje
        factura.save(update_fields=['error_mensaje_sap', 'updated_at'])
        return Response(
            {'status': 'success', 'message': 'Error registrado.'},
            status=status.HTTP_200_OK,
        )


class FacturaReconciliacionViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    Reconciliación periódica (punto 3 del pedido, "menos frecuente" que
    la lista de pendientes) — Facturas con el Preliminar ya confirmado en
    SAP (estado_sap='B'), para que el daemon consulte si Contabilidad ya
    creó el documento definitivo directamente en SAP (fuera del Portal) y
    lo reporte de vuelta vía confirmar-definitivo.
    """
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = FacturaReconciliacionSerializer

    def get_queryset(self):
        if not _flag_habilitado():
            return Factura.objects.none()

        qs = Factura.objects.select_related('proveedor').order_by('fecha_preliminar_confirmado')
        if self.action == 'list':
            return qs.filter(estado_sap='B')
        return qs

    @action(detail=True, methods=['post'], url_path='confirmar-definitivo')
    def confirmar_definitivo(self, request, pk=None):
        """
        Marca estado_sap='Y' y guarda doc_entry_definitivo. Mismo
        criterio de upsert que el resto de este archivo. A partir de
        aquí, ningún endpoint de edición de la Factura acepta cambios —
        candado explícito en apps/invoicing/services_archivos.py::
        validar_permiso_edicion (punto 4 del pedido, verificado también
        con test dedicado).
        """
        factura = self.get_object()
        doc_entry = request.data.get('doc_entry_definitivo')
        if not doc_entry:
            return Response(
                {'status': 'error', 'errors': {'doc_entry_definitivo': ['Este campo es obligatorio.']}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        factura.doc_entry_definitivo = doc_entry
        factura.estado_sap = 'Y'
        factura.fecha_definitivo_confirmado = timezone.now()
        factura.save(update_fields=[
            'doc_entry_definitivo', 'estado_sap', 'fecha_definitivo_confirmado', 'updated_at',
        ])
        return Response(
            {'status': 'success', 'message': 'Documento definitivo confirmado.'},
            status=status.HTTP_200_OK,
        )


class FacturaCancelacionViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    Flujo de cancelación (punto 5 del pedido): una Factura anulada en el
    Portal (`estado='CANCELADO'`) que ya tenía un Preliminar creado en
    SAP (`estado_sap='B'`) necesita que el daemon anule también ese
    documento en SAP antes de considerarse cerrada (`estado_sap='C'`).
    Una Factura cancelada que NUNCA llegó a tener Preliminar en SAP
    (estado_sap sigue en ''/'L') no aparece aquí — no hay nada que
    cancelar del lado de SAP.
    """
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = FacturaCancelacionSerializer

    def get_queryset(self):
        if not _flag_habilitado():
            return Factura.objects.none()

        qs = Factura.objects.select_related('proveedor').order_by('updated_at')
        if self.action == 'list':
            return qs.filter(estado='CANCELADO', estado_sap='B')
        return qs

    @action(detail=True, methods=['post'], url_path='confirmar-cancelacion')
    def confirmar_cancelacion(self, request, pk=None):
        """
        Marca estado_sap='C'. Sin ningún campo obligatorio en el body —
        cancelar no genera un DocEntry nuevo en SAP, solo anula el
        Preliminar ya existente (doc_entry_preliminar). Upsert: un
        reintento sobre una Factura que ya está en 'C' vuelve a
        aplicarlo sin fallar (mismo criterio de retry-safety del resto
        de este archivo).
        """
        factura = self.get_object()
        factura.estado_sap = 'C'
        factura.fecha_cancelado_sap = timezone.now()
        factura.save(update_fields=['estado_sap', 'fecha_cancelado_sap', 'updated_at'])
        return Response(
            {'status': 'success', 'message': 'Cancelación confirmada.'},
            status=status.HTTP_200_OK,
        )
