# apps/base/site_utils.py
"""
Construcción de URLs absolutas para links reales enviados por correo —
una ruta relativa no resuelve a nada útil en un cliente de correo, a
diferencia de una página ya cargada en el navegador.

Extraído de `AppointmentService._notificar_proveedor_qr` (sesión 74) —
la Etapa 2.5 (recuperación de contraseña, sesión 90) necesita
exactamente la misma lógica para el link del correo de recuperación,
así que se centraliza acá en vez de duplicarla una segunda vez.
"""
from django.conf import settings


def construir_url_absoluta(path: str) -> str:
    """
    `path` debe empezar con `/`. Se arma desde `ALLOWED_HOSTS` (ya
    configurado, sin variable de entorno nueva) en vez de asumir un
    dominio fijo.

    Preferencia de dominio custom sobre el subdominio de Railway (ya
    documentada desde la sesión 74): en producción `ALLOWED_HOSTS` trae
    `"web-production-ac867.up.railway.app,nexo.daryza.pe"`, en ese
    orden — sin esta preferencia, `ALLOWED_HOSTS[0]` armaría un link
    funcional pero con el dominio feo de Railway, no el branded.
    """
    hosts = settings.ALLOWED_HOSTS or ['localhost:8000']
    dominio = next((h for h in hosts if 'railway.app' not in h), hosts[0])
    esquema = 'http' if dominio.startswith('localhost') or dominio.startswith('127.') else 'https'
    return f"{esquema}://{dominio}{path}"
