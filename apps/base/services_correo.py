# apps/base/services_correo.py
"""
Envío de correo real vía Microsoft Graph — POST /users/{buzón}/sendMail.

Endpoint elegido, y por qué: con permiso de tipo APLICACIÓN (Client
Credentials Flow, sin usuario logueado — el mismo mecanismo que ya usa
OneDriveClient), `/me/sendMail` no existe: `/me` solo resuelve cuando hay
un usuario delegado autenticado interactivamente. Graph exige el buzón
explícito (`/users/{id o userPrincipalName}/sendMail`) precisamente
porque, con un token de aplicación, no hay ningún "yo" implícito — la
app actúa en nombre de una identidad que hay que nombrar. El buzón real
(GRAPH_SENDER_EMAIL) es el que tiene el permiso Mail.Send concedido
sobre la misma app registrada que ya usa OneDriveClient.

Variable de entorno NUEVA (confirmada explícitamente con el usuario —
no había ningún buzón real de Daryza documentado en el código para usar
como remitente; ONEDRIVE_CLIENT_ID/TENANT_ID/CLIENT_SECRET son
credenciales de la app, no identifican ningún buzón):
    GRAPH_SENDER_EMAIL → UPN del buzón que envía las notificaciones.

Nunca lanza excepción hacia el llamador: un correo que falla (Graph
caído, buzón mal configurado, red) NO debe romper el flujo principal
(confirmar cita, etc. — pedido explícito). El resultado (éxito/error) se
devuelve siempre en ResultadoEnvioCorreo, para que el llamador decida
dónde dejarlo visible — no hay logging persistente en este proyecto
(hallazgo de la sesión 49), así que perder el error en un `except: pass`
lo dejaría invisible por completo.
"""
from dataclasses import dataclass
from typing import Optional

import requests
from django.conf import settings

from .graph_auth import obtener_token_graph

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


@dataclass
class ResultadoEnvioCorreo:
    enviado: bool
    error: Optional[str] = None


def enviar_correo(destinatario: str, asunto: str, cuerpo_html: str) -> ResultadoEnvioCorreo:
    """
    Envía un correo HTML real a `destinatario` desde GRAPH_SENDER_EMAIL.
    Siempre devuelve un ResultadoEnvioCorreo — nunca lanza.
    """
    if not settings.GRAPH_SENDER_EMAIL:
        return ResultadoEnvioCorreo(
            enviado=False, error="GRAPH_SENDER_EMAIL no está configurado."
        )
    if not destinatario:
        return ResultadoEnvioCorreo(enviado=False, error="Sin destinatario.")

    try:
        token = obtener_token_graph()
    except requests.RequestException as e:
        return ResultadoEnvioCorreo(
            enviado=False, error=f"No se pudo obtener el token de Graph: {e}"
        )

    url = f"{GRAPH_BASE}/users/{settings.GRAPH_SENDER_EMAIL}/sendMail"
    payload = {
        "message": {
            "subject": asunto,
            "body": {"contentType": "HTML", "content": cuerpo_html},
            "toRecipients": [{"emailAddress": {"address": destinatario}}],
        },
        "saveToSentItems": "true",
    }

    try:
        resp = requests.post(
            url,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            json=payload,
            timeout=15,
        )
    except requests.RequestException as e:
        return ResultadoEnvioCorreo(enviado=False, error=f"Error de red al llamar a Graph: {e}")

    # sendMail responde 202 Accepted sin body si Graph aceptó encolarlo.
    if resp.status_code == 202:
        return ResultadoEnvioCorreo(enviado=True)

    return ResultadoEnvioCorreo(
        enviado=False, error=f"Graph respondió {resp.status_code}: {resp.text[:500]}"
    )
