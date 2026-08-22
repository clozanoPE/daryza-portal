# apps/base/utils.py
"""
OneDriveClient — Integración con Microsoft Graph API para almacenamiento de COAs.
Usa las credenciales de la aplicación "Daryza-VBS-OneDrive-Service" registrada en Azure AD.

Variables de entorno requeridas (en core/.env):
    ONEDRIVE_CLIENT_ID     → Id. de aplicación (cliente)
    ONEDRIVE_TENANT_ID     → Id. de directorio (inquilino)
    ONEDRIVE_CLIENT_SECRET → Secreto del cliente (generado en Azure AD)
    ONEDRIVE_DRIVE_ID      → ID del Drive de OneDrive (obtener con Graph Explorer)
"""

import io
import requests
from django.conf import settings

from .graph_auth import obtener_token_graph


class OneDriveClient:
    """
    Cliente para operaciones en OneDrive usando OAuth2 Client Credentials Flow.
    No requiere login interactivo; usa credenciales de la app registrada.
    """

    TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    GRAPH_BASE = "https://graph.microsoft.com/v1.0"

    def __init__(self):
        self.client_id     = settings.ONEDRIVE_CLIENT_ID
        self.tenant_id     = settings.ONEDRIVE_TENANT_ID
        self.client_secret = settings.ONEDRIVE_CLIENT_SECRET
        self.drive_id      = settings.ONEDRIVE_DRIVE_ID
        self._token        = None

        # Este print te confirmará en consola que se están usando estos valores
        print(f"--- PRUEBA DE CONEXIÓN DIRECTA ---")
        print(f"URL de Token: https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token")
       
        #url = "https://login.microsoftonline.com/ebff58aa-53a4-436c-8bf3-50af180fcf72/oauth2/v2.0/token"
        #print(f"DEBUG - PROBANDO URL FINAL: {url}")

    def _get_token(self) -> str:
        """
        Obtiene un access token usando Client Credentials (no interactivo).
        Delega la llamada real a apps.base.graph_auth.obtener_token_graph
        (compartida con el servicio de correo) — no duplica la lógica de
        autenticación contra Graph.
        """
        if self._token:
            return self._token

        self._token = obtener_token_graph()

        # --- BLOQUE PARA TRAER EL ID DEL DRIVE AUTOMÁTICAMENTE ---
        if self._token:
            print("--- TOKEN OBTENIDO: BUSCANDO ID DEL DRIVE ---")
            # Intentamos obtener el ID del Drive principal de la cuenta
            try:
                # Opción A: Drive de la cuenta (me/drive)
                # Nota: En flujos de aplicación, a veces 'me' no existe, usamos 'drives'
                test_resp = requests.get(
                    "https://graph.microsoft.com/v1.0/drives", 
                    headers={'Authorization': f'Bearer {self._token}'},
                    timeout=10
                )
                drives_data = test_resp.json().get('value', [])
                if drives_data:
                    print("**************************************************")
                    for d in drives_data:
                        print(f"DRIVE ENCONTRADO: {d.get('name')} | ID: {d.get('id')}")
                    print("**************************************************")
                else:
                    print("No se encontraron drives accesibles para esta App.")
            except Exception as e:
                print(f"No se pudo autodetectar el Drive ID: {e}")
        # -------------------------------------------------------


        return self._token

    def _ensure_folder_path(self, path: str):
        """Crea las carpetas de forma recursiva si no existen."""
        token = self._get_token()
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        
        # Limpiamos la ruta y la dividimos en partes
        parts = [p for p in path.split('/') if p]
        current_path = ""
        
        for part in parts:
            parent_path = current_path if current_path else "root"
            if parent_path != "root":
                url = f"{self.GRAPH_BASE}/drives/{self.drive_id}/root:{current_path}:/children"
            else:
                url = f"{self.GRAPH_BASE}/drives/{self.drive_id}/root/children"
            
            # Intentamos crear la carpeta. Si ya existe, Microsoft devolverá un error 409, que ignoraremos.
            data = {
                "name": part,
                "folder": {},
                "@microsoft.graph.conflictBehavior": "fail" 
            }
            requests.post(url, headers=headers, json=data, timeout=10)
            current_path += f"/{part}"

    def upload_coa(self, file_obj, ruc: str, oc_num: str, item_code: str, sede: str) -> str:
        """
        Sube un archivo COA a OneDrive en la ruta:
            /VBS/{SEDE}/PROVEEDORES/{RUC}/{OC_NUM}/{ITEM_CODE}.pdf

        Retorna la URL compartida (link de descarga directa) del archivo.

        Parámetros:
            file_obj   → objeto de archivo (InMemoryUploadedFile o similar)
            ruc        → RUC del proveedor (username en el sistema)
            oc_num     → Número de OC de SAP
            item_code  → Código de artículo de la línea
            sede       → Código de la Sede de la cita (Appointment.sede.codigo,
                         ej. 'LURIN'/'PUNTA_NEGRA') — separa el árbol de
                         carpetas por sede desde esta sesión (48d). Los COAs
                         ya cargados ANTES de este cambio quedan en la ruta
                         vieja (sin sede) — no se migran retroactivamente,
                         para no mover archivos reales en OneDrive sin
                         necesidad; sus URLs ya guardadas en TicketLineCOA
                         siguen apuntando ahí y siguen funcionando igual.
        """
        token = self._get_token()
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/pdf',
        }

        # 1. Definir la ruta y asegurar que las carpetas existan
        folder_path = f"/VBS/{sede}/PROVEEDORES/{ruc}/{oc_num}"
        self._ensure_folder_path(folder_path)
        
        # 2. Subir el archivo
        remote_full_path = f"{folder_path}/{item_code}.pdf"
        upload_url = f"{self.GRAPH_BASE}/drives/{self.drive_id}/root:{remote_full_path}:/content"



        # Leer contenido del archivo en memoria
        file_content = file_obj.read()

        # Subir (PUT simple para archivos < 4MB; usar upload session para >4MB)
        resp = requests.put(upload_url, headers=headers, data=file_content, timeout=30)
        resp.raise_for_status()

        item_id = resp.json().get('id')
        if not item_id:
            raise ValueError("OneDrive no retornó el ID del archivo subido.")

        # Crear link compartido de solo lectura
        share_url = (
            f"{self.GRAPH_BASE}/drives/{self.drive_id}/items/{item_id}/createLink"
        )
        share_resp = requests.post(
            share_url,
            headers={**headers, 'Content-Type': 'application/json'},
            json={'type': 'view', 'scope': 'organization'},
            timeout=15
        )
        share_resp.raise_for_status()
        link = share_resp.json()['link']['webUrl']
        return link