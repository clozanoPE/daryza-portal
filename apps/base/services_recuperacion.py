# apps/base/services_recuperacion.py
"""
Recuperación de contraseña (Etapa 2.5 del plan de integración VB.NET
<->Portal, sesión 90).

Criterios confirmados explícitamente antes de escribir este código:
2. Usa `default_token_generator` de Django (`PasswordResetTokenGenerator`
   estándar) — sin inventar un esquema de token propio. El token está
   atado al hash de contraseña + `last_login` del usuario (mecanismo
   interno de Django, no reimplementado acá) — esto es lo que hace que
   un token ya usado quede automáticamente inválido: al cambiar la
   contraseña con éxito, el hash contra el que el token se valida ya no
   coincide, así que un segundo intento con el mismo token falla solo,
   sin necesitar ningún registro de "tokens ya usados" del lado del
   Portal.
3. Reutiliza `services_correo.enviar_correo` (sesión 73) para el envío
   — mismo motor ya usado por el resto del Portal (correo de bienvenida
   de la Etapa 2.2, notificación de cita confirmada), no un mecanismo
   paralelo.
6. `solicitar_recuperacion` NUNCA revela si `username` existe o no —
   no lanza, no devuelve nada que distinga ambos casos; es la vista
   (`apps.base.views.solicitar_recuperacion`) la que muestra siempre el
   mismo mensaje neutro, sin importar el resultado interno de esta
   función.
7. La expiración del token es el comportamiento default de Django
   (`PASSWORD_RESET_TIMEOUT`, no sobreescrito en `core/settings.py` —
   3 días por defecto) — no se reimplementa acá tampoco.
"""
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from . import services_correo
from .site_utils import construir_url_absoluta


def solicitar_recuperacion(username: str) -> None:
    """
    Envía el correo de recuperación si `username` corresponde a una
    cuenta real y activa — no hace nada (sin lanzar, sin ningún efecto
    observable desde afuera) en cualquier otro caso. El llamador nunca
    debe inferir el resultado interno de esta función.
    """
    user = User.objects.filter(username=username, is_active=True).first()
    if user is None:
        return

    uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    link = construir_url_absoluta(f"/cuenta/recuperar/{uidb64}/{token}/")

    asunto = "Recuperación de contraseña — Portal de Proveedores Daryza"
    cuerpo = f"""
    <p>Recibimos una solicitud para restablecer la contraseña de su
    cuenta en el Portal de Proveedores Daryza.</p>
    <p><a href="{link}">Haga clic acá para establecer una nueva
    contraseña</a>.</p>
    <p>Si usted no solicitó este cambio, puede ignorar este correo — su
    contraseña actual sigue siendo válida.</p>
    """
    services_correo.enviar_correo(user.email, asunto, cuerpo)


def resolver_usuario_desde_token(uidb64: str, token: str):
    """
    Devuelve el `User` si `uidb64`/`token` son válidos y el token no
    está vencido ni ya usado (ver punto 2 del docstring del módulo) —
    `None` en cualquier otro caso (uid mal formado, usuario inexistente
    o inactivo, token inválido/vencido/reutilizado). El llamador
    (`confirmar_recuperacion`) decide qué mensaje mostrar — acá no se
    distingue el motivo exacto del rechazo, a propósito (mismo criterio
    de no revelar más información de la necesaria, punto 7).
    """
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid, is_active=True)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        return None

    if not default_token_generator.check_token(user, token):
        return None

    return user
