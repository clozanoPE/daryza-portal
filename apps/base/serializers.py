# apps/base/serializers.py
"""
Serializers de apps.base consumidos por endpoints del daemon SAP. Mismo
criterio de ubicación ya usado por apps.sap_sync/apps.operations/
apps.invoicing: el serializer vive en la app dueña del modelo — acá,
`SupplierProfile`.
"""
from rest_framework import serializers


class ProveedorSyncSerializer(serializers.Serializer):
    """
    Payload de UN proveedor para `POST /api/v1/sync-proveedores/`
    (Etapa 2.3, sesión 88). No es un `ModelSerializer` — no mapea 1:1 a
    `SupplierProfile` (el alta real de `User`/grupo/contraseña la hace
    `onboard_proveedor`, `apps.base.supplier_onboarding`, Etapa 2.2, no
    este serializer) — este solo valida la FORMA del payload de entrada,
    registro por registro (nunca en lote — ver `api/proveedor_api.py`).

    `card_code`: identificador de SAP (CardCode), obligatorio — sin él
    no hay con qué hacer upsert de nada.

    `e_mail`/`lic_trad_num`: obligatorios y no vacíos — criterio de
    negocio confirmado explícitamente (sin `e_mail` no se puede
    notificar el alta; sin `lic_trad_num` no hay con qué construir la
    contraseña temporal). `allow_blank=False` (el default de DRF, dejado
    explícito a propósito) rechaza tanto la ausencia del campo como un
    string vacío `""` — ambos casos deben rechazar ese registro, según
    lo confirmado.

    `card_name`/`card_fname`: opcionales — su ausencia no impide el
    alta, solo deja esos 2 campos de `SupplierProfile` vacíos.
    """
    card_code = serializers.CharField(max_length=50, allow_blank=False)
    card_name = serializers.CharField(max_length=255, allow_blank=True, required=False, default='')
    card_fname = serializers.CharField(max_length=255, allow_blank=True, required=False, default='')
    e_mail = serializers.CharField(max_length=255, allow_blank=False)
    lic_trad_num = serializers.CharField(max_length=50, allow_blank=False)
