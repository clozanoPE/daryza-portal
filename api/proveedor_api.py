# api/proveedor_api.py
"""
Endpoint del daemon SAP para el alta/actualización de Proveedores
(Etapa 2.3 del plan de integración VB.NET<->Portal, sesión 88) — mismo
patrón de autenticación (Token de `daemon_sap`) ya usado por
`SAPIntegrationViewSet` (sync-oc) / `EntradaMercaderiaViewSet` /
`Factura*ViewSet`.

    POST /api/v1/sync-proveedores/

Distinto de `sync-oc` en un punto de diseño importante y confirmado
explícitamente para esta sub-etapa: NO usa el mecanismo `many=True` de
DRF para lotes (que valida TODO el lote junto y, si un solo ítem es
inválido, invalida el lote entero sin guardar nada). Acá cada
proveedor del lote se valida y procesa de forma INDEPENDIENTE,
reutilizando `onboard_proveedor()` (`apps.base.supplier_onboarding`,
Etapa 2.2) tal cual — ya está diseñado como su propia unidad
transaccional (`atomic()` por registro, nunca uno compartido). Es
justamente ESE diseño previo el que hace posible, acá, que un registro
con datos inválidos no bloquee ni afecte a los demás del mismo POST.

Payload: un objeto único, o una lista de objetos — mismo criterio de
soporte individual/lote ya usado en `sync-oc`
(`many=isinstance(request.data, list)`), aunque acá no se delega al
`many=True` de DRF por el motivo de arriba.

Respuesta: siempre `200` con `{'resultados': [...]}` — un resultado por
`card_code`, nunca un único éxito/error global del request completo
(el demonio necesita saber qué pasó con CADA proveedor del lote, para
decidir a cuáles marcar `U_PORTAL='M'` en SAP y a cuáles reintentar en
el siguiente ciclo). Cada resultado trae `status` en
`{ESTADO_CREADO, ESTADO_ACTUALIZADO_SIN_ALTA, 'error'}` — los primeros
2 son las constantes reales de `supplier_onboarding`; `'error'` cubre
tanto un rechazo de validación del serializer como una excepción real
propagada por `onboard_proveedor` (ver docstring de esa función), con
el detalle en `errors`.
"""
from rest_framework import status, viewsets
from rest_framework.authentication import TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.base.serializers import ProveedorSyncSerializer
from apps.base.supplier_onboarding import onboard_proveedor


class ProveedorSyncViewSet(viewsets.GenericViewSet):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = ProveedorSyncSerializer

    def create(self, request):
        payload = request.data if isinstance(request.data, list) else [request.data]

        resultados = [self._procesar_uno(item) for item in payload]

        return Response({'resultados': resultados}, status=status.HTTP_200_OK)

    def _procesar_uno(self, item):
        """
        Un registro, de principio a fin: valida su forma, y si es
        válido llama a onboard_proveedor() dentro de su propio try —
        una excepción real de ahí (ej. la colisión de username ya
        probada en la Etapa 2.2) tampoco debe interrumpir el loop del
        resto del lote.
        """
        card_code_crudo = item.get('card_code', '') if isinstance(item, dict) else ''

        serializer = self.get_serializer(data=item)
        if not serializer.is_valid():
            return {
                'card_code': card_code_crudo,
                'status': 'error',
                'errors': serializer.errors,
            }

        datos = serializer.validated_data
        try:
            resultado = onboard_proveedor(
                card_code=datos['card_code'],
                card_name=datos.get('card_name', ''),
                card_fname=datos.get('card_fname', ''),
                e_mail=datos['e_mail'],
                lic_trad_num=datos['lic_trad_num'],
            )
        except Exception as exc:
            return {
                'card_code': datos['card_code'],
                'status': 'error',
                'errors': {'detail': str(exc)},
            }

        return {
            'card_code': resultado.card_code,
            'status': resultado.estado,
            'email_enviado': resultado.email_enviado,
            'email_error': resultado.email_error,
        }
