# apps/base/graph_auth.py
"""
Autenticación compartida contra Microsoft Graph (OAuth2 Client
Credentials Flow, no interactivo) — extraída de OneDriveClient
(apps/base/utils.py) para que el nuevo servicio de correo
(apps/base/services_correo.py) la reutilice sin duplicar la lógica.

Misma app de Azure AD para ambos usos (confirmado explícitamente por el
usuario: Mail.Send ya está concedido en la misma app registrada que ya
usa OneDriveClient) — reutiliza las mismas 3 variables de entorno
(ONEDRIVE_CLIENT_ID/TENANT_ID/CLIENT_SECRET), sin ninguna credencial
nueva. El nombre "ONEDRIVE_*" quedó desactualizado por esto (la app ya
no es solo para OneDrive), pero renombrar esas 3 variables en Railway es
una migración de infraestructura aparte, fuera de alcance de esta
sesión — se documenta aquí para quien lo revise más adelante.
"""
import requests
from django.conf import settings

TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"


def obtener_token_graph() -> str:
    """
    Client Credentials Flow — sin caché entre llamadas (cada invocación
    pide un token nuevo; el volumen de este proyecto, subida de COAs +
    envío de notificaciones, no justifica la complejidad de un caché
    compartido con expiración). Lanza requests.HTTPError si Azure AD
    rechaza las credenciales — el llamador decide cómo manejarlo.
    """
    url = TOKEN_URL.format(tenant_id=settings.ONEDRIVE_TENANT_ID)
    resp = requests.post(url, data={
        'grant_type': 'client_credentials',
        'client_id': settings.ONEDRIVE_CLIENT_ID,
        'client_secret': settings.ONEDRIVE_CLIENT_SECRET,
        'scope': 'https://graph.microsoft.com/.default',
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()['access_token']
