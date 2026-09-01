# Checklist — Prueba E2E del flujo LOTE → GRPO → Facturación (sesión 99 / 99b / 99c)

OCs objetivo: **79001197**, **79001198**, **79001199**.
Entorno: **Portal local** (`http://127.0.0.1:8000`) + **SAP `BK_1808261700`** (HANA `192.168.10.30:30015` + Service Layer).

---

## Estado actual (ya hecho por Claude)

| ✅ | Acción |
|---|---|
| ✅ | `gestionado_por_lote` en el modelo + migración `sap_sync/0005` aplicada. `operations/0010` (estado/lote de `EntradaMercaderia`) y `operations/0011` (`EntradaMercaderiaEvento`, rastro de reaperturas) también aplicadas. |
| ✅ | **Grant `SELECT ON OITM` dado** → el sync de OC ya trae `gestionado_por_lote`. |
| ✅ | **Daemon ejecutado — sync EXITOSO** de las 3 OCs (`79001197`, `79001198`, `79001199`). `gestionado_por_lote` verificado por línea (ver tabla ↓). |
| ✅ | Password de los 3 proveedores (`P20102021836`, `P20612579751`, `P20615319059`) → `Prueba2026!`. |
| ✅ | 32 `AppointmentSlot` futuros (LURIN + PUNTA_NEGRA). |
| ✅ | **BD local limpia (sesión 99c).** Se borraron TODAS las `Factura`, `Appointment` (+ cascada Ticket / EntradaMercadería / eventos / inspecciones / COA) y todas las `PurchaseOrder` salvo las 3 objetivo. Se arranca el ciclo completo desde 0 — sin histórico, para trazabilidad limpia. **No hizo falta re-sincronizar las OCs** (siguen en el Portal con el `gestionado_por_lote` correcto). |

### Las 3 OCs en el Portal local

| OC (`doc_num`) | Tipo | Moneda | Proveedor (`card_code`) | Líneas | Casos que cubre |
|---|---|---|---|---|---|
| **79001197** | MP | USD | `P20102021836` | 1 · `EB00000455` (200) — **`gestionado_por_lote=True`, `requiere_coa=True`** | Flujo completo: COA + Calidad + LOTE obligatorio + `BatchNumbers` + facturación. |
| **79001199** | MP | USD | `P20612579751` | 1 · `EB00000373` (12000) — **`gestionado_por_lote=True`, `requiere_coa=False`** | LOTE obligatorio sin COA. |
| **79001198** | CDL (comercial) | SOL | `P20615319059` | 20 · **todas `gestionado_por_lote=False`** | Ninguna línea pide lote: el campo LOTE queda deshabilitado, el daemon **no** manda `BatchNumbers`. (`LineNum=7` no vino — cerrada en SAP, normal.) |

> Contrastar cada `gestionado_por_lote` contra SAP: Datos maestros de artículo → Inventario → "Gestionar artículo por: Lotes" (`OITM.ManBtchNum='Y'`).

---

## Paso 1 — Flujo E2E por cada OC (cita → recepción → GRPO)

Repetir para **79001197** (`P20102021836`), **79001199** (`P20612579751`) y **79001198** (`P20615319059`). Contraseña de todas las cuentas: **`Prueba2026!`**.

| # | Rol / cuenta | Acción |
|---|---|---|
| 1.1 | Proveedor | `/appointments/portal/` → "Agenda de Cupos" → slot futuro → seleccionar la OC → **Solicitar cita**. |
| 1.2 | **`ucompras`** | `/operations/compras/` → Pendientes → la solicitud → para `79001197` usar "Configurar COA" → **Confirmar** (genera Ticket + QR). |
| 1.3 | Proveedor | **Solo `79001197`**: subir el COA de la línea `EB00000455`. Opcional: "Datos del Vehículo / Conductor". |
| 1.4 | **`uvigilancia`** | `/operations/vigilancia/` → buscar el QR → **Autorizar Ingreso a Planta**. En `79001197` **bloquea** si falta el COA. |
| 1.5 | Actor de recepción | `79001197` / `79001199` = MP → **`umateriaprima`**. `79001198` = comercial → **`ualmacen`**. Detalle del Ticket → **Iniciar Recepción** (muelle + checkbox "Solicitar Inspección Calidad"). Luego "Mi Sesión" → CANT. REAL / ESTADO / OBSERVACIÓN → **Guardar**. |
| 1.6 | **`ucalidad`** | Solo si se marcó "Solicitar Inspección Calidad". `/operations/calidad/` → el Ticket → **Finalizar Inspección de Calidad**. |
| 1.7 | **`uvigilancia`** | Detalle del Ticket → **Registrar Salida** → Ticket **FINALIZADO**. El Portal genera la **EntradaMercadería en PENDIENTE** (`estado_sap=''`). |
| 1.8 | Actor de recepción | `/operations/materia-prima/` o `/operations/almacen/` → sección **"Entradas de Mercadería por generar"** → **"Completar lote"** → `/operations/entrada-mercaderia/<id>/`. |
| 1.9 | mismo | La columna **LOTE** solo pide dato en las líneas `gestionado_por_lote=True`. <br>• `79001197` / `79001199`: cargar nº de lote (opcional venc. / fab.). <br>• `79001198`: todas "No gestionado por lote" (deshabilitado). <br>Ajustar Cantidad Real si hace falta. |
| 1.10 | mismo | **"Enviar a SAP B1"**. Valida que toda línea de lote tenga nº. Transición: `PENDIENTE → ENVIADO`, `estado_sap '' → 'L'`. |
| 1.11 | Daemon | `cd daemon-sap/app/app/bin/Debug && ./app.exe --sync-grpo`. Log esperado: `[GRPO-ÉXITO] Entrada #<id>: ... DocEntry=..., DocNum=...` → `[GRPO-CONFIRMADO]`. |
| 1.12 | Verificar | **Portal:** `EntradaMercaderia` queda `estado='CREADO_SAP'`, `estado_sap='Y'`, `doc_num_sap` poblado (badge "Entrada Mercadería: Creado en SAP B1 · GRPO <nro>" en `/operations/ticket/<id>/trazabilidad/`). **SAP:** buscar el `OPDN` por ese DocNum → cantidad real; `79001197`/`79001199` con `BatchNumber`; `79001198` sin lote. |

---

## Paso 1b — Camino de error: SAP rechaza la Entrada (nuevo, sesión 99c)

Si el daemon reporta un error de SAP (`reportar-error` → `EntradaMercaderia.error_mensaje` poblado, sigue en `estado='ENVIADO'`):

| # | Rol / cuenta | Acción |
|---|---|---|
| E.1 | Verificar | En el panel del actor (`/operations/materia-prima/` o `/operations/almacen/`) aparece la sección **"Entradas de Mercadería rechazadas por SAP"** con el texto del error y un botón **"Corregir"**. También `/operations/entrada-mercaderia/<id>/` muestra una alerta amarilla + botón **"Reabrir para corregir"** (solo si `ENVIADO` + hay error). |
| E.2 | Actor de recepción (o superusuario) | Botón **"Reabrir para corregir"** → confirma. La Entrada vuelve a `PENDIENTE` / `estado_sap=''`, se limpia `error_mensaje`, y queda un **evento de reapertura** (quién, cuándo, qué decía el error). |
| E.3 | mismo | Corregir el lote / la cantidad en el detalle → **"Enviar a SAP B1"** de nuevo → repetir 1.11. |
| E.4 | Verificar | `/operations/entrada-mercaderia/<id>/` muestra **"Historial de reaperturas"** con el/los evento(s). `/operations/ticket/<id>/trazabilidad/` muestra "Entrada reabierta N vez(ces) tras rechazo de SAP". El rastro **persiste** aunque el envío siguiente sea exitoso. |

> `reabrir_para_correccion` rechaza si la Entrada ya está `CREADO_SAP` ("el GRPO ya existe en SAP") o si está `ENVIADO` sin error (nada que corregir).

---

## Paso 2 — Facturación (requiere GRPO real en SAP)

**Una OC solo aparece en "Copiar de OC(s)" cuando su `EntradaMercaderia` está en `estado='CREADO_SAP'`** (el daemon confirmó el GRPO). Una Entrada en `ENVIADO` — enviada pero aún no creada, o rechazada por SAP — **no** habilita facturación (cambio sesión 99c: antes bastaba `ENVIADO`).

| # | Rol / cuenta | Acción |
|---|---|---|
| 2.1 | Proveedor | `/invoicing/facturas/` → **"Nueva factura desde OC(s)"** → la OC debe aparecer en la lista (solo si Paso 1.12 completó con `CREADO_SAP`). Seleccionar → **"Copiar de OC(s)"**. |
| 2.2 | Proveedor | En "Copiar de OC(s)": por línea se ve **Artículo · Cantidad (editable) · Unidad de Medida · Precio Unitario · Indicador de Impuesto ("IGV" / "IGV Exonerado") · Total Línea** (recalcula en vivo con la cantidad). Ajustar cantidades → **Crear factura**. |
| 2.3 | Proveedor | En el detalle de la factura: cargar XML, PDF, CDR (y retención/detracción si aplica). Verificar que el detalle de línea muestra las mismas columnas (Unidad de Medida, Indicador de Impuesto, Total Línea neto). → **Enviar a revisión**. |
| 2.4 | **`ucompras`** | `/invoicing/compras/` → la factura → revisar → **Aprobar** (o Observar). |
| 2.5 | Daemon (si `FACTURA_DRAFT_SAP_HABILITADO=True`) | `./app.exe --sync-factura-preliminar` → crea el Preliminar en SAP copiando del GRPO. Luego `--sync-factura-reconciliar` para el definitivo. |

---

## Casos a validar explícitamente

- **`79001197` / `79001199` — línea de lote sin nº** → "Enviar a SAP B1" rechaza nombrando el `item_code`.
- **`79001198` — líneas NO gestionadas por lote** → campo LOTE deshabilitado; el daemon **no** manda `BatchNumbers`.
- **`DocNum` de vuelta** → `EntradaMercaderia.doc_num_sap` con el número visible del GRPO en SAP.
- **Cantidad real ≠ cantidad OC** → `EntradaMercaderiaLinea.cantidad` y el GRPO reflejan lo inspeccionado.
- **Reabrir tras rechazo** → la Entrada vuelve a editable, queda el evento, el rastro persiste tras un reenvío exitoso.
- **Facturación bloqueada sin `CREADO_SAP`** → una OC cuya Entrada está en `ENVIADO` (o rechazada) **no** aparece en "Copiar de OC(s)".
- **Columnas de línea en Factura** → Unidad de Medida (`po_line.und_medida`), Indicador de Impuesto como etiqueta, Total Línea neto (`cantidad × precio`) — tanto en "Copiar de OC(s)" como en el detalle.

### Verificación rápida por shell

```
python manage.py shell -c "
from apps.operations.models import EntradaMercaderia
for em in EntradaMercaderia.objects.select_related('ticket').order_by('-fecha_generada')[:5]:
    print(em.id, 'ticket', em.ticket_id, em.estado, em.estado_sap, 'doc_num_sap=', em.doc_num_sap, 'err=', (em.error_mensaje or '')[:80])
    for ev in em.eventos.all():
        print('   evento', ev.tipo, ev.fecha, ev.usuario_id, repr(ev.mensaje[:60]))
    for l in em.lineas.all():
        print('   ', l.po_line.item_code, 'cant=', l.cantidad, 'lote=', repr(l.numero_lote), 'venc=', l.fecha_vencimiento_lote)
"
```

---

## Datos de acceso (Portal local)

| Cuenta | Password | Uso |
|---|---|---|
| `P20102021836` | `Prueba2026!` | Proveedor de la OC **79001197** (MP) |
| `P20612579751` | `Prueba2026!` | Proveedor de la OC **79001199** (MP) |
| `P20615319059` | `Prueba2026!` | Proveedor de la OC **79001198** (comercial) |
| `ucompras` / `ualmacen` / `ucalidad` / `uvigilancia` / `umateriaprima` | `Prueba2026!` | Roles operativos |
| `daemon_sap` (token) | coincide con `App.config::DaryzaApiToken` | Endpoints `/api/v1/` del daemon |

---

## Recordatorios

- **Daemon `App.config`**: HANA → `BK_1808261700`, Portal → `http://127.0.0.1:8000`. Verificar antes de cada corrida.
- Modo consola **sin flag** → ciclo real de sync de OC (~90s, se detiene solo). **`--sync-grpo`** → solo GRPO. **`--sync-factura-preliminar` / `--sync-factura-reconciliar` / `--sync-factura-cancelar`** → Etapa 5 (solo con `FACTURA_DRAFT_SAP_HABILITADO=True`).
- `gestionado_por_lote` es `required=True` en el serializer → un POST sin ese campo → `400`.
- Si vuelve `insufficient privilege` sobre otra tabla, aplicar `GRANT SELECT`.
- El daemon-sap **no** está instalado como servicio de Windows — solo modo consola.
- Para re-probar desde cero una OC ya usada: borrar en el Portal la cita (cascadea Ticket + EntradaMercadería + eventos) y re-marcar `U_DRYZ_PC='1'` en SAP.
