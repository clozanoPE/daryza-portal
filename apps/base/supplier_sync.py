# apps/base/supplier_sync.py
"""
Upsert de SupplierProfile a partir de los datos de una OC ya sincronizada
desde SAP (card_code/card_name/e_mail — ya presentes en el payload real
del daemon, `PurchaseOrderSerializer`, confirmado antes de escribir esto).

Vive en apps.base junto al modelo (mismo criterio que Sede/filters.py/
reporting.py) — llamado desde apps.sap_sync (cada sync de OC) sin que esa
app tenga que conocer los detalles del modelo de perfil de proveedor.
"""
from django.core.exceptions import ValidationError

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


def resolver_perfil_de_usuario(user) -> SupplierProfile | None:
    """
    Resuelve el SupplierProfile del usuario logueado (apps.invoicing,
    "Copiar de OC(s)" — Factura.proveedor es FK a SupplierProfile, no a
    User directo). Devuelve None si no existe ningún perfil todavía (el
    proveedor nunca tuvo una OC sincronizada) — el llamador decide cómo
    mostrar ese estado vacío, no se crea un perfil "en blanco" aquí.

    Mismo criterio de matching ya usado en todo el proyecto (`apps/
    appointments/views.py::PortalProveedorView`: `card_code == username`,
    sin ninguna transformación) — primero busca por `user` ya vinculado
    (camino normal, rápido); si no hay ninguno, busca por `sap_card_code
    == user.username` y AUTO-VINCULA el perfil (`sincronizar_
    supplier_profile` lo crea sin ningún `user` — nace del sync de OC, ver
    docstring de `SupplierProfile`) — necesario para que `services_
    archivos.validar_permiso_edicion` reconozca al dueño real al crear/
    editar una Factura. Si el perfil encontrado por `sap_card_code` ya
    está vinculado a OTRA cuenta, se rechaza con un error claro en vez de
    robarle el vínculo en silencio (no debería ocurrir en la práctica —
    cada `sap_card_code` corresponde a un único RUC real — pero es una
    inconsistencia real de datos si pasara, no algo para ignorar).
    """
    perfil = SupplierProfile.objects.filter(user=user).first()
    if perfil is not None:
        return perfil

    perfil = SupplierProfile.objects.filter(sap_card_code=user.username).first()
    if perfil is None:
        return None
    if perfil.user_id is not None:
        raise ValidationError(
            "El perfil de proveedor asociado a su usuario ya está vinculado a otra cuenta de Portal."
        )
    perfil.user = user
    perfil.save(update_fields=['user', 'updated_at'])
    return perfil
