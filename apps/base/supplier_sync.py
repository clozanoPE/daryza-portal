# apps/base/supplier_sync.py
"""
Upsert de SupplierProfile a partir de los datos de una OC ya sincronizada
desde SAP (card_code/card_name/e_mail — ya presentes en el payload real
del daemon, `PurchaseOrderSerializer`, confirmado antes de escribir esto).

Vive en apps.base junto al modelo (mismo criterio que Sede/filters.py/
reporting.py) — llamado desde apps.sap_sync (cada sync de OC) sin que esa
app tenga que conocer los detalles del modelo de perfil de proveedor.
"""
from .models import SupplierProfile


def sincronizar_supplier_profile(card_code: str, card_name: str, e_mail: str) -> SupplierProfile:
    """
    get_or_create por sap_card_code — nunca pisa `estado` ni `user` en un
    perfil que ya existe (un `SupplierProfile` puede llevar meses
    suspendido o ya vinculado a una cuenta real; un resync de OC no debe
    revertir ninguna de esas 2 decisiones). razon_social/correo_
    electronico sí se refrescan en cada sync — son datos maestros de SAP,
    no algo que un humano edite todavía (sin panel de administración en
    esta fase), así que mantenerlos al día con la fuente real es correcto.
    """
    perfil, creado = SupplierProfile.objects.get_or_create(
        sap_card_code=card_code,
        defaults={
            'ruc': card_code,
            'razon_social': card_name,
            'correo_electronico': e_mail,
            'estado': SupplierProfile.ESTADO_ACTIVO,
        },
    )
    if not creado:
        perfil.razon_social = card_name
        perfil.correo_electronico = e_mail
        perfil.save(update_fields=['razon_social', 'correo_electronico', 'updated_at'])
    return perfil
