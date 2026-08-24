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

    def _subir_y_compartir(self, remote_full_path: str, contenido: bytes, content_type: str) -> str:
        """
        PUT del contenido a `remote_full_path` + creación del link
        compartido de solo lectura — lógica común de upload_coa/
        upload_documento_factura, extraída para no duplicarla (mismo
        principio ya aplicado a la autenticación, apps/base/graph_auth.py).
        """
        token = self._get_token()
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': content_type}

        upload_url = f"{self.GRAPH_BASE}/drives/{self.drive_id}/root:{remote_full_path}:/content"
        # PUT simple para archivos < 4MB; usar upload session para >4MB
        # (no aplica hoy: los límites de tamaño de esta app son de 5-10MB).
        resp = requests.put(upload_url, headers=headers, data=contenido, timeout=30)
        resp.raise_for_status()

        item_id = resp.json().get('id')
        if not item_id:
            raise ValueError("OneDrive no retornó el ID del archivo subido.")

        share_url = f"{self.GRAPH_BASE}/drives/{self.drive_id}/items/{item_id}/createLink"
        share_resp = requests.post(
            share_url,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            json={'type': 'view', 'scope': 'organization'},
            timeout=15,
        )
        share_resp.raise_for_status()
        return share_resp.json()['link']['webUrl']

    def _descargar(self, remote_full_path: str) -> bytes:
        """
        GET del contenido crudo de `remote_full_path` (path-addressing de
        Graph, mismo mecanismo que ya usa `_subir_y_compartir` para el
        PUT) — deliberadamente NO se usa el webUrl del link compartido
        guardado en BD para esto: ese link es de solo-lectura/preview
        para un navegador humano, no un endpoint de descarga de bytes sin
        resolución adicional. Reconstruir la ruta determinística (Sub-
        fase 3.3, `descargar_documento_factura`) evita depender de un
        `item_id` que `_subir_y_compartir` no retorna hoy.
        """
        token = self._get_token()
        headers = {'Authorization': f'Bearer {token}'}
        url = f"{self.GRAPH_BASE}/drives/{self.drive_id}/root:{remote_full_path}:/content"
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.content

    def descargar_documento_factura(
        self, sede: str, ruc: str, identificador: str, nombre_archivo: str,
    ) -> bytes:
        """
        Descarga el contenido real (bytes) de un archivo de Factura ya
        subido con `upload_documento_factura` — misma ruta determinística
        que ese método usó para subirlo, ver su docstring.
        """
        remote_full_path = f"/VBS/{sede}/FACTURAS/{ruc}/{identificador}/{nombre_archivo}"
        return self._descargar(remote_full_path)

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
        folder_path = f"/VBS/{sede}/PROVEEDORES/{ruc}/{oc_num}"
        self._ensure_folder_path(folder_path)
        remote_full_path = f"{folder_path}/{item_code}.pdf"
        contenido = file_obj.read()
        return self._subir_y_compartir(remote_full_path, contenido, 'application/pdf')

    def upload_documento_factura(
        self, contenido: bytes, sede: str, ruc: str, identificador: str,
        nombre_archivo: str, content_type: str,
    ) -> str:
        """
        Sube un archivo de Factura/FacturaLinea a OneDrive en la ruta:
            /VBS/{SEDE}/FACTURAS/{RUC}/{IDENTIFICADOR}/{NOMBRE_ARCHIVO}

        `identificador` puede incluir subcarpetas (ej. "42/L1" para la
        línea 1 de la Factura 42) — se usa Factura.pk, no
        numero_comprobante: ese campo puede seguir vacío mientras la
        Factura está en BORRADOR (se completa recién en una fase
        posterior a partir del propio XML), y el pk siempre existe.

        A diferencia de upload_coa, recibe `contenido` (bytes) ya leído
        en vez de un file_obj — quien llama (services_archivos.py) ya
        necesita los bytes en memoria para el hash SHA-256 y la
        validación de contenido real, así que se leen una sola vez.
        """
        folder_path = f"/VBS/{sede}/FACTURAS/{ruc}/{identificador}"
        self._ensure_folder_path(folder_path)
        remote_full_path = f"{folder_path}/{nombre_archivo}"
        return self._subir_y_compartir(remote_full_path, contenido, content_type)