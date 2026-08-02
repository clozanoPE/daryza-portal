import os
import requests
import json
from dotenv import load_dotenv

# Token cargado desde core/.env (SAP_SYNC_TOKEN) o pedido por input manual —
# nunca hardcodeado en este archivo. Ver CLAUDE.md.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'core', '.env'))

# Configuración
URL = "http://127.0.0.1:8000/api/v1/sync-oc/"
TOKEN = os.getenv('SAP_SYNC_TOKEN') or input(
    "Token de autenticación (DRF authtoken) para /api/v1/sync-oc/: "
).strip()

if not TOKEN:
    raise SystemExit(
        "Se requiere un token para ejecutar este script. "
        "Defínelo en SAP_SYNC_TOKEN dentro de core/.env, o ingrésalo cuando se te pida."
    )

# Datos de prueba (Simulando una OC de SAP)
payload = {
    "doc_entry": 999,
    "doc_num": 1122557744,
    "card_code": "P20405060",
    "card_name": "PROVEEDOR DE PRUEBA SAC",
    "lines": [
        {
            "line_num": 0,
            "item_code": "ART-001",
            "description": "Producto de Prueba API",
            "quantity_sap": 10.5
        }
    ]
}

headers = {
    "Authorization": f"Token {TOKEN}",
    "Content-Type": "application/json"
}

print(f"Enviando OC {payload['doc_num']}...")
response = requests.post(URL, data=json.dumps(payload), headers=headers)

if response.status_code == 201:
    print("✅ ÉXITO: La API recibió la orden y respondió 201 Created.")
else:
    print(f"❌ ERROR {response.status_code}: {response.text}")
