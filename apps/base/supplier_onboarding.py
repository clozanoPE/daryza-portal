# apps/base/supplier_onboarding.py
"""
Alta automática de proveedores (Etapa 2.2 del plan de integración
VB.NET<->Portal, sesión 87) — se invoca desde el endpoint `sync-
proveedores` (Etapa 2.3, todavía sin construir) una vez por cada
proveedor del lote que envía el demonio (`U_PORTAL='Y'` en SAP).

Distinto de `apps/base/supplier_sync.py::sincronizar_supplier_profile`
(el upsert incrustado en `sync-oc`, que solo actualiza el `SupplierProfile`
y nunca crea ningún `User`) — este módulo es el que da de alta la cuenta
real de Portal (`User`+grupo+contraseña temporal) la primera vez que un
proveedor llega por este flujo nuevo.

Diseño confirmado explícitamente por el usuario antes de escribir este
código (ver CLAUDE.md, sesión 86/87):

1. Cada proveedor se procesa en su PROPIO `transaction.atomic()` — nunca
   uno compartido para todo el lote. `onboard_proveedor()` es, por
   construcción, una unidad transaccional independiente: esto es lo que
   hace posible, en la Etapa 2.3, que un registro con datos inválidos no
   afecte a los demás del mismo lote (cada llamada abre y cierra su
   propia transacción, sin anidarse dentro de un `atomic()` más grande
   que envuelva el lote completo).
2. Alta de `User` SOLO si `SupplierProfile.user_id` es `None` — un
   proveedor ya vinculado a una cuenta (dada de alta manualmente, o por
   este mismo flujo en un ciclo anterior) nunca se toca: ni contraseña,
   ni membresía de grupo, ni correo. Esto evita que un reenvío desde SAP
   (por ejemplo si `U_PORTAL` sigue en `'Y'` por un reintento del
   demonio, o porque alguien lo reactivó sin querer) resetee una
   contraseña que el proveedor ya cambió por la suya.
3. El correo de bienvenida se dispara DESPUÉS de que el `atomic()` ya
   salió con éxito (commit real a la base de datos) — nunca dentro del
   bloque. Si el envío de correo falla, el alta en BD ya quedó
   confirmada de todas formas (`services_correo.enviar_correo` además
   nunca lanza, así que tampoco hay riesgo de que un fallo de Graph
   propague una excepción hacia el llamador). Si en cambio el `atomic()`
   falla (ej. una colisión real de `username`), nunca se llega siquiera
   a intentar el envío.
4. La contraseña temporal (el `LicTradNum`/RUC del proveedor) se asigna
   vía `user.set_password(...)` directo, nunca a través de un formulario
   que valide (`UserCreationForm`, `PasswordResetForm.save()`, etc.) —
   Django no tiene `AUTH_PASSWORD_VALIDATORS` sobreescrito en este
   proyecto (`core/settings.py`), así que sigue vigente su lista default,
   que incluye `NumericPasswordValidator` — un RUC de 11 dígitos, 100%
   numérico, sería rechazado si pasara por esa validación. `set_password()`
   no la invoca, así que el alta funciona sin fricción.
"""
from dataclasses import dataclass
from typing import Optional

from django.contrib.auth.models import Group, User
from django.db import transaction

from . import services_correo
from .models import SupplierProfile

GRUPO_PROVEEDORES = 'PROVEEDORES'

ESTADO_CREADO = 'creado'
ESTADO_ACTUALIZADO_SIN_ALTA = 'actualizado_sin_alta'


@dataclass
class ResultadoAltaProveedor:
    card_code: str
    estado: str  # ESTADO_CREADO | ESTADO_ACTUALIZADO_SIN_ALTA
    perfil: SupplierProfile
    email_enviado: bool = False
    email_error: Optional[str] = None


def _construir_correo_bienvenida(card_code: str, lic_trad_num: str) -> tuple[str, str]:
    """
    Asunto + cuerpo HTML del correo de bienvenida. La contraseña
    temporal viaja en texto plano a propósito — regla de negocio ya
    confirmada: la contraseña inicial ES el LicTradNum, un dato que el
    propio proveedor ya conoce, no un secreto generado que deba viajar
    solo por un link/token (eso es el flujo de recuperación, Etapa 2.5,
    distinto de este).
    """
    asunto = "Bienvenido al Portal de Proveedores Daryza"
    cuerpo = f"""
    <p>Se ha creado su cuenta de acceso al Portal de Proveedores Daryza.</p>
    <p>
        <strong>Usuario:</strong> {card_code}<br>
        <strong>Contraseña temporal:</strong> {lic_trad_num}
    </p>
    <p>Al ingresar por primera vez, el sistema le pedirá reemplazar esta
    contraseña temporal por una de su elección antes de continuar.</p>
    """
    return asunto, cuerpo


def onboard_proveedor(
    card_code: str,
    card_name: str,
    card_fname: str,
    e_mail: str,
    lic_trad_num: str,
) -> ResultadoAltaProveedor:
    """
    Alta o actualización de UN proveedor — unidad atómica independiente
    (punto 1 del docstring del módulo). No captura ninguna excepción
    propia: si algo falla dentro del `atomic()` (ej. una colisión real
    de `username`), se deja propagar tal cual — es responsabilidad del
    llamador (la Etapa 2.3, que procesa el lote registro por registro)
    decidir qué hacer con ese error sin afectar a los demás del lote.
    """
    es_alta_nueva = False

    with transaction.atomic():
        perfil, _creado = SupplierProfile.objects.get_or_create(
            sap_card_code=card_code,
            defaults={'estado': SupplierProfile.ESTADO_ACTIVO},
        )

        # Datos maestros de SAP: se refrescan siempre, sin importar si
        # el perfil ya existía o si ya tiene una cuenta vinculada (mismo
        # criterio ya usado por sincronizar_supplier_profile).
        perfil.razon_social = card_name
        perfil.razon_social_legal = card_fname
        perfil.correo_electronico = e_mail
        perfil.ruc = lic_trad_num
        campos_a_guardar = [
            'razon_social', 'razon_social_legal', 'correo_electronico', 'ruc', 'updated_at',
        ]

        if perfil.user_id is None:
            grupo, _ = Group.objects.get_or_create(name=GRUPO_PROVEEDORES)

            user = User.objects.create_user(username=card_code, email=e_mail)
            user.set_password(lic_trad_num)
            user.save()
            user.groups.add(grupo)

            perfil.user = user
            perfil.debe_cambiar_password = True
            campos_a_guardar += ['user', 'debe_cambiar_password']
            es_alta_nueva = True

        perfil.save(update_fields=campos_a_guardar)

    # --- fuera del atomic(): el commit de todo lo anterior ya ocurrió ---
    email_enviado = False
    email_error = None
    if es_alta_nueva:
        asunto, cuerpo = _construir_correo_bienvenida(card_code, lic_trad_num)
        resultado_correo = services_correo.enviar_correo(e_mail, asunto, cuerpo)
        email_enviado = resultado_correo.enviado
        email_error = resultado_correo.error

    return ResultadoAltaProveedor(
        card_code=card_code,
        estado=ESTADO_CREADO if es_alta_nueva else ESTADO_ACTUALIZADO_SIN_ALTA,
        perfil=perfil,
        email_enviado=email_enviado,
        email_error=email_error,
    )
