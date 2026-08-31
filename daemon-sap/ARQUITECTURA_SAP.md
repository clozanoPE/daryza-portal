# Arquitectura de la integración con SAP Business One — `daemon-sap`

Documento de referencia técnica, no narrativo — para eso está `CLAUDE.md` (historial de sesiones). Este archivo describe el estado de la arquitectura **al cierre de la sesión 99**: SQL directo contra HANA para lectura masiva de OC (Etapa 1, en producción), un cliente de sesión de Service Layer ya funcional (Etapa 3.1/3.2, todavía solo accesible vía un flag manual de prueba `--test-service-layer`), la **creación real de GRPO** (Etapa 4, sesión 97 — **rediseñada en la sesión 99**, ver abajo), y la **creación/reconciliación/cancelación del Preliminar de Factura** (Etapa 5, sesión 98) — código completo, compilado, y ejercitado contra el Portal real (sin datos calificados todavía del lado de SAP — ver sección 3.4). Ninguna de las 2 etapas de escritura está **conectada al ciclo automático** de `WorkerThread` — se disparan solo a mano (`--sync-grpo`, `--sync-factura-preliminar`, `--sync-factura-reconciliar`, `--sync-factura-cancelar`), mismo criterio deliberado que `--test-service-layer` (ver sección 3.3).

**Sesión 99 — el flujo de GRPO cambió: ahora hay un paso HUMANO intermedio en el Portal antes de que el daemon actúe.** Los artículos de Daryza se gestionan por LOTE, y el lote se ingresa al recibir la mercadería. Antes, la `EntradaMercaderia` se generaba automáticamente al finalizar el Ticket (Vigilancia registra la salida) y quedaba de inmediato disponible para el daemon (`estado_sap='L'`). Ahora nace en un estado nuevo `PENDIENTE` (`estado_sap=''`): Almacén / Materia Prima abre una pantalla de detalle en el Portal (mismo layout que "Copiar de OC" de Factura), completa el **número de lote** por línea (obligatorio) más fechas de vencimiento/fabricación (opcionales), y recién entonces la **envía** (`estado` → `ENVIADO`, `estado_sap='' → 'L'`). Ahí el daemon la ve. **El GRPO sigue siendo de UN SOLO PASO hacia SAP** (`POST /PurchaseDeliveryNotes` directo, sin Draft) — lo único que cambió del lado del daemon es que cada `DocumentLine` ahora lleva `BatchNumbers` cuando la línea trae lote, y que se parsea también el `DocNum` visible de la respuesta (se guarda en `EntradaMercaderia.doc_num_sap` vía `confirmar-borrador`/`confirmar-definitivo`).

Léase junto con `CLAUDE.md` → sección "Estructura del repositorio" → "Plan de integración VB.NET ↔ Portal" (la tabla de 6 etapas y las decisiones de diseño ya confirmadas).

---

## 1. Diagrama de capas

Este proyecto habla con SAP B1 por **2 mecanismos completamente distintos, que nunca deben mezclarse**:

- **SQL directo contra HANA** (`Sap.Data.Hana`, ADO.NET) — lectura masiva rápida de datos ya existentes en SAP (cabeceras/líneas de OC), y una única escritura de control (marcar un UDF de flag, `U_DRYZ_PC`). **Nunca se usa, y nunca debe usarse, para crear documentos de negocio nuevos** (Entradas de Mercancía, Facturas) — un `INSERT`/lógica SQL directa no replica la numeración de series, los asientos contables, los movimientos de inventario ni las validaciones que SAP B1 aplica internamente al crear un documento real; eso es exactamente el trabajo que hace la capa de negocio de SAP, a la que solo se accede vía Service Layer o DI API — nunca escribiendo filas a mano en las tablas base.
- **SAP Service Layer** (REST/OData sobre HTTPS, login con sesión) — el mecanismo oficial de SAP para *crear* documentos de negocio nuevos (GRPO, Factura Preliminar — Etapas 4/5, ambas ya implementadas) respetando toda esa lógica interna. Hoy también se usa para una lectura de prueba (`BusinessPartners`) y para consultas OData de reconciliación (`$filter` sobre `PurchaseInvoices`, Etapa 5.3), pero su razón de ser en este proyecto es la escritura.

```
┌──────────────────────────────────────────────────────────────────────┐
│  CAPA DE ORQUESTACIÓN (proyecto "app")                                │
│                                                                        │
│  Service1.Designer.vb :: Main()                                       │
│    - Servicio Windows real  → ServiceBase.Run(New Service1)           │
│    - Modo consola (manual)  → svc.OnStart() / svc.OnStop()            │
│    - Flag --test-service-layer → SOLO llama a ServiceLayerSmokeTest,  │
│      nunca toca HANA ni el ciclo real (ver sección 3)                 │
│    - Flag --sync-grpo → SOLO llama a GrpoSyncService.ExecuteGrpoSync, │
│      manual también (Etapa 4, todavía NO wireado al loop automático)  │
│    - Flag --sync-factura-preliminar → FacturaSyncService.             │
│      ExecutePreliminarSync (Etapa 5.1/5.2, manual)                    │
│    - Flag --sync-factura-reconciliar → FacturaSyncService.            │
│      ExecuteReconciliacionSync (Etapa 5.3, manual, gate de intervalo) │
│    - Flag --sync-factura-cancelar → FacturaSyncService.               │
│      ExecuteCancelacionSync (Etapa 5.4, manual)                       │
│                                                                        │
│  Service1.vb :: OnStart() → arranca WorkerThread (hilo dedicado)      │
│  Service1.vb :: WorkerThread → loop: ExecuteSync() ; Sleep(17s)       │
│    (Grpo/FacturaSyncService NO forman parte de este loop — ver 3.3)   │
└───────────────────────────────┬────────────────────────────────────┘
                                 │ decide CUÁNDO llamar a cada capa de abajo
                                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│  CAPA DE LÓGICA / ORQUESTACIÓN DE NEGOCIO (proyecto "daemon_logical")  │
│                                                                        │
│  SyncService.vb :: ExecuteSync()  — el ciclo real, cada 17s:          │
│    1. Lee OC pendientes de HANA          (SyncSAPB1)                  │
│    2. Arma el DTO                         (daemon_entity)             │
│    3. POST al Portal                      (HttpClient propio)         │
│    4. Si el Portal confirmó, marca en HANA (SyncSAPB1.MarkAsSynced)   │
│                                                                        │
│  ServiceLayerSmokeTest.vb — SOLO manual, vía --test-service-layer:    │
│    Login + 1 GET contra Service Layer, resultado a Logger + consola   │
│                                                                        │
│  GrpoSyncService.vb — SOLO manual, vía --sync-grpo (Etapa 4):         │
│    1. GET entradas-pendientes/ del Portal    (HttpClient propio)      │
│    2. Mapea DTO del Portal → GrpoLineaEntrada (único cruce de mundos) │
│    3. Delega la creación real a GrpoService   (daemon_data)           │
│    4. Si SAP acepta: POST confirmar-borrador Y confirmar-definitivo   │
│       al Portal, mismo DocEntry (flujo de un solo paso, sesión 86)    │
│    5. Si SAP rechaza: POST reportar-error al Portal, sigue con la     │
│       siguiente entrada (un fallo no interrumpe el ciclo)             │
│                                                                        │
│  FacturaSyncService.vb — SOLO manual, 3 flags independientes          │
│  (Etapa 5, sesión 98), un método público por sub-etapa:               │
│    ExecutePreliminarSync (5.1/5.2): GET facturas-pendientes-          │
│      preliminar/, salta líneas sin GRPO confirmado (espera, no        │
│      error), delega en FacturaService, confirma/reporta error         │
│    ExecuteReconciliacionSync (5.3): gateado por intervalo mínimo      │
│      configurable, GET facturas-preliminares/, busca en SAP por       │
│      U_MSSL_PORTAL_ID, confirma el definitivo si aparece              │
│    ExecuteCancelacionSync (5.4): GET facturas-pendientes-             │
│      cancelacion/, delega la anulación (DELETE) en FacturaService,    │
│      confirma la cancelación                                          │
│                                                                        │
│  Logger.vb — Logs/Log_YYYY-MM-DD.txt, usado por los 4 de arriba       │
└──────┬───────────────────────────────────────────┬───────────────────┘
       │                                            │
       ▼ SQL directo (lectura masiva + 1 flag)      ▼ Service Layer (sesión HTTP)
┌──────────────────────────┐              ┌──────────────────────────────┐
│  daemon_data               │              │  daemon_data                  │
│  SyncSAPB1.vb               │              │  SapServiceLayerClient.vb      │
│  HanaConnectionManager.vb   │              │  (login manual de cookies,     │
│                             │              │   re-auth automático en 401,   │
│                             │              │   GetAsync/PostAsync/          │
│                             │              │   DeleteAsync comparten un     │
│                             │              │   mismo helper de reintento)   │
│                             │              │  GrpoService.vb (Etapa 4):     │
│                             │              │  arma el payload "Copy From"   │
│                             │              │  (BaseType=22, origen=OC) y    │
│                             │              │  llama PostAsync("Purchase-    │
│                             │              │  DeliveryNotes", ...)          │
│                             │              │  FacturaService.vb (Etapa 5):  │
│                             │              │  arma el Draft "Copy From"     │
│                             │              │  (BaseType=20, origen=GRPO) →  │
│                             │              │  PostAsync("Drafts", ...);     │
│                             │              │  busca el definitivo por UDF   │
│                             │              │  (GetAsync $filter) y lo       │
│                             │              │  anula (DeleteAsync)           │
│                             │              │  Ambas por composición sobre   │
│                             │              │  SapServiceLayerClient, nunca  │
│                             │              │  lo extienden                  │
└──────────────┬─────────────┘              └───────────────┬────────────────┘
               │ ADO.NET / Sap.Data.Hana                     │ HTTPS / JSON (OData)
               ▼                                              ▼
      ┌─────────────────┐                            ┌──────────────────────┐
      │   SAP HANA DB     │                            │   SAP Service Layer   │
      │  (conexión directa,│                           │  (balanceado, 4 nodos │
      │   puerto SQL)      │                            │   detrás de Apache —  │
      └─────────────────┘                            │   ver sección 3)      │
                                                        └──────────────────────┘
```

**Regla dura, sin excepción**: cualquier código futuro que necesite **crear o modificar** un documento en SAP va por Service Layer (`SapServiceLayerClient`), nunca por `SyncSAPB1`/HANA directo. `SyncSAPB1` queda reservado para lectura masiva (el patrón ya probado desde la Etapa 1) y para el único flag de control (`U_DRYZ_PC`) que ya tenía antes de este rediseño — no se le agregan más responsabilidades de escritura.

---

## 2. Tabla de archivos ↔ responsabilidad

| Archivo | Qué hace | Contra qué habla | Endpoint(s) del Portal |
|---|---|---|---|
| `app/Service1.Designer.vb` | Punto de entrada (`Main`) — decide servicio Windows real / modo consola manual / prueba de humo de Service Layer, según cómo se lanzó el proceso | — (despacha a las capas de abajo) | — |
| `app/Service1.vb` | `OnStart`/`OnStop`, hilo `WorkerThread` — el loop real de sincronización (cada 17s) | — (orquesta `SyncService`) | — |
| `app/ProjectInstaller.vb` / `.Designer.vb` / `.resx` | Boilerplate estándar de instalación como servicio Windows (`installutil`) — infraestructura generada por Visual Studio, sin lógica de negocio | — | — |
| `daemon_logical/SyncService.vb` | Orquesta el ciclo real: lee OC pendientes de HANA, arma el DTO, hace `POST` al Portal, marca el flag en HANA solo si el Portal confirmó éxito (`201`); reintentos con backoff solo ante fallas transitorias | HANA (vía `SyncSAPB1`) **y** API del Portal | `POST /api/v1/sync-oc/` |
| `daemon_logical/ServiceLayerSmokeTest.vb` | Orquesta la prueba de humo de Service Layer (login + 1 `GET` de solo lectura) + logging de resultados. **Solo se dispara manualmente** (`--test-service-layer`) — no forma parte del ciclo real de `WorkerThread` | Service Layer (vía `SapServiceLayerClient`) | — (no llama al Portal) |
| `daemon_logical/GrpoSyncService.vb` | **Etapa 4 (sesión 97; sesión 99: LOTE + DocNum).** Orquesta el ciclo de creación de GRPO: `GET entradas-pendientes/` (el Portal solo devuelve las que Almacén/MP ya envió — `estado_sap='L'` —, con el lote ya completo), mapea el DTO del Portal a `GrpoLineaEntrada` **incluyendo `NumeroLote`/`FechaVencimientoLote`/`FechaFabricacionLote`** (único punto del proyecto donde se cruzan el vocabulario del Portal y el de SAP — solo asignación de campos, sin lógica), delega la creación real a `GrpoService` (`daemon_data`), y confirma de vuelta al Portal enviando **también el `doc_num`** (sesión 99) en `confirmar-borrador`/`confirmar-definitivo`. Reintento con backoff propio (`EnviarAlPortalConReintentoAsync`, mismo criterio que `AwakeApiPost`), un fallo por entrada no interrumpe el resto del ciclo. **Solo se dispara manualmente** (`--sync-grpo`) — no forma parte de `WorkerThread` todavía (ver sección 3.3) | Portal (HttpClient propio) — nunca habla con SAP directamente, delega en `GrpoService` | `GET /api/v1/entradas-pendientes/`, `POST .../confirmar-borrador/`, `POST .../confirmar-definitivo/`, `POST .../reportar-error/` |
| `daemon_logical/FacturaSyncService.vb` | **Etapa 5 (sesión 98).** 3 métodos públicos independientes, un ciclo por sub-etapa: `ExecutePreliminarSync` (GET pendientes, salta líneas sin GRPO confirmado — espera, no error —, mapea a `FacturaPreliminarCabecera`/`FacturaPreliminarLinea`, delega en `FacturaService`, confirma/reporta error); `ExecuteReconciliacionSync` (gateado por `FacturaReconciliacionIntervaloMinutos`, GET pendientes de reconciliar, delega la búsqueda por UDF en `FacturaService`, confirma si aparece — "no encontrado" no es un error, no hay `reportar-error` para esta etapa); `ExecuteCancelacionSync` (GET pendientes de cancelar, delega la anulación en `FacturaService`, confirma — tampoco hay `reportar-error` acá). Reintento con backoff propio (misma copia del patrón ya usada en `GrpoSyncService`, no una abstracción compartida). **Los 3 se disparan manualmente** (`--sync-factura-preliminar`/`-reconciliar`/`-cancelar`) — ninguno forma parte de `WorkerThread` todavía | Portal (HttpClient propio) — nunca habla con SAP directamente, delega en `FacturaService` | `GET .../facturas-pendientes-preliminar/`, `POST .../confirmar-preliminar/`, `POST .../reportar-error/`, `GET .../facturas-preliminares/`, `POST .../confirmar-definitivo/`, `GET .../facturas-pendientes-cancelacion/`, `POST .../confirmar-cancelacion/` |
| `daemon_logical/Logger.vb` | Escribe `Logs/Log_YYYY-MM-DD.txt` (append, con limpieza automática >30 días) — usado por `SyncService`, `ServiceLayerSmokeTest`, `GrpoSyncService` y `FacturaSyncService` | — (sistema de archivos local) | — |
| `daemon_data/SyncSAPB1.vb` | Consultas SQL directas contra HANA: `GetPendingHeaders` (OC con `U_DRYZ_PC='1'`), `GetLinesByDocEntry` (líneas + precio/IGV/moneda reales de SAP), `MarkAsSynced` (única escritura: `UPDATE OPOR SET U_DRYZ_PC='2'`) | HANA (SQL directo, vía `HanaConnectionManager`) | Alimenta el payload de `POST /api/v1/sync-oc/` (no llama al Portal directamente — eso lo hace `SyncService`) |
| `daemon_data/HanaConnectionManager.vb` | Abre/cierra conexiones ADO.NET a HANA; lee `HanaServer`/`HanaCompanyDB`/`HanaDbUser`/`HanaDbPassword` de `App.config` | HANA | — |
| `daemon_data/SapServiceLayerClient.vb` | Cliente de sesión de Service Layer: `LoginAsync` (POST `/Login`, extrae `B1SESSION`/`ROUTEID` a mano de los `Set-Cookie` crudos), `GetAsync`/`PostAsync`/`DeleteAsync` (los 3 delegan en un mismo helper privado de reintento-en-401 — `DeleteAsync` agregado en la sesión 98 para la anulación del Preliminar, Etapa 5.4). Sesión única reutilizada mientras siga vigente — no logueo por cada llamada | Service Layer (REST/OData sobre HTTPS) | — (sin endpoint del Portal — es la capa de conexión pura a SAP; la usan `ServiceLayerSmokeTest`, `GrpoService` y `FacturaService` por composición) |
| `daemon_data/GrpoService.vb` | **Etapa 4 (sesión 97; sesión 99: `BatchNumbers` + `DocNum`).** Arma el payload "Copy From" (`CardCode` + `DocumentLines` con `BaseType=22`/`BaseEntry`/`BaseLine`/`Quantity` — sin `ItemCode` explícito, SAP lo deriva). **Sesión 99:** cada `DocumentLine` lleva `BatchNumbers=[{BatchNumber, Quantity, ExpiryDate?, ManufactureDate?}]` **SOLO cuando la línea trae `NumeroLote`** (un ítem NO gestionado por lote con `BatchNumbers` también hace que SAP rechace el documento). Sigue siendo `POST /PurchaseDeliveryNotes` directo — **un solo paso, sin Draft** (a diferencia de Factura). Parsea `DocEntry` **y `DocNum`** de la misma respuesta exitosa (`GrpoResult.DocNum`, opcional). Consume `SapServiceLayerClient` por composición (recibe una instancia ya lista por constructor — la misma sesión que usa todo el ciclo), nunca lo extiende ni lo reimplementa. Sin dependencia de `daemon_entity` a propósito — `GrpoLineaEntrada` es un tipo propio y mínimo, mantiene esta capa desacoplada de los DTOs del Portal | Service Layer (vía `SapServiceLayerClient`, nunca directo) | — |
| `daemon_data/FacturaService.vb` | **Etapa 5 (sesión 98).** `CrearPreliminarAsync` — arma el Draft "Copy From" (`DocObjectCode="oPurchaseInvoices"` + `DocumentLines` con `BaseType=20`/`BaseEntry`/`BaseLine`/`Quantity`/`UnitPrice`, UDFs `U_MSSL_FPC`/`FNC`/`TOP`/`CBS`/`PORTAL_ID`) y llama `PostAsync("Drafts", ...)`. `BuscarDefinitivoAsync` — `GetAsync` con `$filter=U_MSSL_PORTAL_ID eq '{id}'&$select=DocEntry` contra `PurchaseInvoices` (el documento real, nunca `/Drafts`). `CancelarPreliminarAsync` — `DeleteAsync("Drafts({id})")`. Mismos criterios que `GrpoService`: composición sobre `SapServiceLayerClient`, sin dependencia de `daemon_entity`, tipos propios (`FacturaPreliminarCabecera`/`FacturaPreliminarLinea`) | Service Layer (vía `SapServiceLayerClient`, nunca directo) | — |
| `daemon_data/HanaService.vb` | ⚠️ **Código huérfano — no compila, nunca corre.** No está referenciado en `daemon_data.vbproj` (`<Compile Include>`). Consulta una vista distinta (`VW_DRYZ_FE`, facturas de venta/AR) de un contexto no relacionado con este demonio, y depende de tipos (`MF_Entities.FiltroDTO`/`Documento`) que ni siquiera existen en este proyecto. Documentado acá explícitamente para que nadie lo confunda con código activo ni pierda tiempo debugueándolo | (ninguno — no compila) | — |
| `daemon_entity/PurchaseOrderDTO.vb` | DTOs `PurchaseOrderDTO`/`PurchaseOrderLineDTO` — forma exacta del JSON enviado a `/sync-oc/`, nombres de propiedad en snake_case coincidiendo 1:1 con el serializer real de Django (`apps/sap_sync/serializers.py`) | — (solo forma de datos, sin lógica ni llamadas) | Alimenta `POST /api/v1/sync-oc/` |
| `daemon_entity/EntradaMercaderiaDTO.vb` | **Etapa 4 (sesión 97; sesión 99: LOTE).** DTOs `EntradaMercaderiaDTO`/`EntradaMercaderiaLineaDTO` — forma exacta del JSON que devuelve `GET /api/v1/entradas-pendientes/`, snake_case 1:1 con `apps/operations/serializers.py::EntradaMercaderiaSerializer`. **Sesión 99:** la línea gana `numero_lote As String` (siempre poblado en lo que devuelve el endpoint — el Portal valida que todas las líneas tengan lote antes de permitir el envío), `fecha_vencimiento_lote As Date?` y `fecha_fabricacion_lote As Date?` (opcionales, `null` si el artículo no las requiere) | — (solo forma de datos) | Deserializado desde `GET /api/v1/entradas-pendientes/` |
| `daemon_entity/FacturaDTO.vb` | **Etapa 5 (sesión 98).** DTOs `FacturaPreliminarDTO`/`FacturaLineaPreliminarDTO`/`FacturaReconciliacionDTO`/`FacturaCancelacionDTO` — forma exacta del JSON de los 3 endpoints de Factura, snake_case 1:1 con `apps/invoicing/serializers.py`. Formas confirmadas empíricamente contra el serializer real (no adivinadas) — `cantidad`/`precio`/`tipo_cambio` llegan como string (DRF coerciona `DecimalField`), `tax_date`/`doc_due_date` como ISO 8601 o null | — (solo forma de datos) | Deserializado desde los 3 `GET` de Factura |

**Endpoints del Portal — todos ya consumidos.** Los 4 de Entrada de Mercancía/GRPO (`entradas-pendientes`, `confirmar-borrador`, `confirmar-definitivo`, `reportar-error`) desde la Etapa 4 (sesión 97); los 7 de Factura (`facturas-pendientes-preliminar`, `confirmar-preliminar`, `reportar-error`, `facturas-preliminares`, `confirmar-definitivo`, `facturas-pendientes-cancelacion`, `confirmar-cancelacion`) desde la Etapa 5 (sesión 98) — ver sección 6.

---

## 3. Puntos de falla conocidos y cómo detectarlos

Los 2 hallazgos reales de la sesión 95 — encontrados al probar `SapServiceLayerClient` contra el servidor real por primera vez, no anticipados en el diseño original. Documentados acá con el síntoma exacto observado, para que una reaparición futura (ej. tras una actualización de .NET Framework o de Service Layer) tenga un precedente en vez de investigarse desde cero.

### 3.1 — Login devuelve `500 Internal Server Error` con body vacío, en cualquier nodo del balanceador

**Síntoma observado exacto** (capturado real durante el diagnóstico):
```
Status=InternalServerError
Headers: DataServiceVersion: 3.0
         Connection: close
         Set-Cookie: ROUTEID=.node3; path=/b1s
         Server: Apache/2.4.43 (Unix)
ContentHeaders: Content-Length: 0
```
El body de la respuesta viene **completamente vacío** (`Content-Length: 0`) — no hay ningún mensaje de error de SAP que leer. Reproducible en los 4 nodos del balanceador (`ROUTEID=.node1` a `.node4`), nunca depende de a cuál te haya tocado.

**Causa real**: `HttpClient` de .NET Framework 4.8 envía el header `Expect: 100-continue` por default en **cualquier** `POST` con body. El balanceador Apache/`mod_proxy_balancer` que reparte Service Layer entre sus nodos no maneja ese handshake y corta la conexión con un `500` vacío antes de que el body llegue a ningún nodo real de SAP.

**Cómo confirmarlo si reaparece**: repetir el mismo `POST` con `curl` (que NO manda `Expect: 100-continue` por default) — si `curl` funciona (`200`) y el cliente .NET no, agregarle a mano `-H "Expect: 100-continue"` a curl; si eso reproduce el mismo `500` vacío, es este mismo problema.

**Fix ya aplicado**: `ServicePointManager.Expect100Continue = False`, en el constructor de `SapServiceLayerClient`. Nota: es una configuración **global del proceso** (`ServicePointManager` es estático) — una vez que cualquier instancia de la clase la setea, aplica a todo el `AppDomain`. Si en el futuro se instancia `HttpClient` en algún otro punto del código sin pasar por `SapServiceLayerClient` (ej. un test aislado que nunca construye esa clase), esta protección no estaría activa todavía en ese momento.

### 3.2 — Login exitoso (200, `SessionId` real en el body), pero el primer `GET` posterior falla con `401`

**Síntoma observado exacto**:
```json
{
   "error" : {
      "code" : 301,
      "message" : { "lang" : "en-us", "value" : "Invalid session or session already timeout." }
   }
}
```
...inmediatamente después de un login que sí devolvió `200` con un `SessionId` válido en el body — no es un problema de credenciales ni de expiración real por inactividad.

**Causa real**: el `Set-Cookie` real que Service Layer devuelve para `B1SESSION` tiene esta forma exacta: `B1SESSION=<guid>;HttpOnly;` — sin atributo `Path`, y con un punto y coma colgante después de `HttpOnly`. El parser de cookies de `System.Net.CookieContainer` en .NET Framework 4.8 no logra almacenar esa cookie con ese formato (queda descartada en silencio, sin ninguna excepción) — mientras que `ROUTEID` (formato de cookie más convencional) sí se guarda con normalidad. El resultado: cada request posterior sale sin `B1SESSION`, y SAP lo rechaza como sesión inválida.

**Cómo confirmarlo si reaparece**: si se reintrodujera `CookieContainer` en algún punto, inspeccionar explícitamente qué cookies contiene DESPUÉS del login (`_cookies.GetCookies(uri)`) — si `B1SESSION` está ausente pero `ROUTEID` sí aparece, es este mismo problema.

**Fix ya aplicado**: `SapServiceLayerClient` **no usa `CookieContainer`** (`HttpClientHandler.UseCookies = False`). Ambas cookies (`B1SESSION` y `ROUTEID`) se extraen a mano de los headers `Set-Cookie` crudos de la respuesta de `/Login` (`response.Headers.TryGetValues("Set-Cookie", ...)`) y se reenvían a mano en el header `Cookie` de cada request subsiguiente (`ArmarHeaderCookie()`). Cualquier código futuro que hable con Service Layer desde este proyecto debería **reutilizar `SapServiceLayerClient` tal cual**, no reimplementar el manejo de sesión — este problema ya está resuelto ahí.

**Riesgo de reaparición**: si una actualización futura de Service Layer cambiara el formato exacto del `Set-Cookie` de `B1SESSION` (ej. agregándole un atributo `Path` real), el parseo manual actual (`ExtraerNombreValor`, que toma todo antes del primer `;`) seguiría funcionando igual — es robusto a ese cambio específico. Rompería solo si SAP cambiara el **nombre** de la cookie de sesión (no ha pasado nunca, no es un cambio típico de una actualización menor).

### 3.3 — `--sync-grpo` (Etapa 4) probado con datos del Portal que resultaron ser sintéticos, no respaldados en SAP real

**Contexto**: al ejecutar `--sync-grpo` por primera vez contra datos reales del Portal (sesión 97), las 5 `EntradaMercaderia` disponibles en estado `'L'` fueron **rechazadas las 5 por SAP real**, con mensajes de error genuinos y específicos (no genéricos):

```
Invalid BP code [OPDN.CardCode] , 'P20609988771'
Invalid BP code [OPDN.CardCode] , 'P20544332211'
Item number is missing; specify an item number [DocumentLines.ItemCode][line: 4]
Base document card and target document card do not match.
```

**Causa real, confirmada, no solo sospechada**: las 5 `EntradaMercaderia` disponibles estaban construidas sobre `PurchaseOrder` **sintéticas** — creadas directamente por ORM en sesiones de prueba manual del Portal (sesiones 40/54, `doc_entry` en el rango 29001-29025), nunca sincronizadas realmente desde SAP. Se confirmó comparando `created_at`/`updated_at` de esos registros (idénticos entre sí — nunca se volvieron a tocar tras la creación, a diferencia de una OC real re-sincronizada) contra las OC genuinamente sincronizadas de esta misma sesión (`doc_num` 79001190 en adelante, `doc_entry` 33765+, en `BK_1808261700`) — esas sí tienen `updated_at` posterior a `created_at`, reflejando resyncs reales. Un `CardCode`/`DocEntry` que nunca existió en SAP no puede ser referenciado por un "Copy From" — el rechazo de SAP es exactamente el comportamiento correcto ante datos que, del lado de SAP, simplemente no existen.

**Lo que esto SÍ probó, de punta a punta y con datos 100% reales**: el camino de error (Sub-etapa 4.4) — SAP rechaza → `GrpoService.CrearGrpoAsync` devuelve `Exitoso=False` con el mensaje real de SAP (nunca inventado) → `GrpoSyncService` llama `POST reportar-error/` real al Portal → verificado en la BD real de Django que las 5 `EntradaMercaderia` quedaron con `estado_sap='L'` intacto (nunca marcadas como falso-éxito) y `error_mensaje` poblado con el texto real de SAP. Ningún documento falso se confirmó nunca del lado del Portal.

**Lo que esto NO probó todavía**: el camino de éxito (Sub-etapa 4.2 — un GRPO real creado con un `DocEntry` real). Para probarlo hace falta una `EntradaMercaderia` construida sobre un Ticket real, cuya OC haya sido genuinamente sincronizada desde SAP (ej. completando un ciclo de cita real en el Portal sobre alguna de las OC ya confirmadas como reales de esta sesión, `doc_num` 79001190-79001194).

**Por esto mismo `--sync-grpo` sigue siendo un flag manual, no parte de `WorkerThread`** (mismo criterio ya aplicado a `--test-service-layer`, sesión 95): es la primera vez que el demonio intenta *crear* un documento real en SAP, y automatizarlo antes de haber validado el camino de éxito con datos genuinamente reales sería un riesgo innecesario. Wireado al loop automático recién cuando el usuario lo pida explícitamente, después de una prueba de éxito real.

**Actualización sesión 99 — se agregó un paso humano intermedio en el Portal antes del daemon.** Desde esta sesión, una `EntradaMercaderia` recién generada NO aparece en `GET /api/v1/entradas-pendientes/`: nace en estado `PENDIENTE` (`estado_sap=''`) y solo pasa a `ENVIADO` (`estado_sap='L'`, visible para el daemon) cuando Almacén/MP completa el número de lote de todas las líneas en la pantalla de detalle del Portal y hace clic en "Enviar a SAP B1". Esto NO resuelve el hallazgo de datos sintéticos de arriba — una OC sintética con lote completado seguirá siendo rechazada por SAP (ahora por `CardCode`/`ItemCode` inexistente, antes de llegar a validar el `BatchNumber`). La prueba del camino de éxito sigue pendiente de una OC real (`79001066`/`79000775`/`16089947` u otra genuinamente sincronizada) con su Ticket `FINALIZADO` y su lote cargado.

### 3.4 — Etapa 5: `BaseType` verificado antes de escribir código; `U_MSSL_PORTAL_ID` sigue sin verificar (único punto abierto)

**`BaseType=20`, no 22, confirmado ANTES de implementar (sesión 98)** — el dato más importante de sanity check de toda la Etapa 5, tal como se pidió explícitamente. El diseño original (sesión 86) no especificaba el `BaseType` exacto para copiar un GRPO hacia una Factura; por analogía con `GrpoService` (que copia una OC con `BaseType=22`) habría sido fácil asumir el mismo valor por error — un origen de documento distinto (Purchase Delivery Note, no Purchase Order) tiene su propio código. Verificado por 2 vías independientes antes de escribir `FacturaService.vb`:
1. Búsqueda de la tabla real de `BoObjectTypes` de SAP B1 (Purchase Order=22, **Goods Receipt PO/Purchase Delivery Note=20**, A/P Invoice=18) — confirmada contra una fuente que lista el catálogo completo, no un único ejemplo aislado.
2. Un ejemplo real de payload de Service Layer, `POST /PurchaseInvoices` con `"BaseType": 20, "BaseEntry": 460, "BaseLine": 0` copiando explícitamente desde un Purchase Delivery Note — coincide exactamente con el valor de la vía 1.

Corroborado indirectamente además por el propio resultado de la Etapa 4 (sección 3.3): los 5 rechazos reales de SAP fueron por `CardCode`/`ItemCode`/documento no coincidente — **nunca** por un `BaseType` inválido, lo que habría sido el síntoma esperado si `GrpoService` estuviera usando el código equivocado para copiar desde una OC. No es una prueba directa de que `20` sea correcto para Factura (`GrpoService` copia de una OC con `22`, un caso distinto), pero sí confirma que SAP procesa y valida el campo `BaseType` normalmente contra este balanceador/instalación, sin ningún comportamiento inesperado a ese nivel.

**`U_MSSL_PORTAL_ID` — único punto que sigue sin verificar contra SAP real**, documentado explícitamente en el código (`FacturaPreliminarCabecera.PortalId`, `FacturaService.CrearPreliminarAsync`/`BuscarDefinitivoAsync`): se asumió que el UDF es de tipo **Alfanumérico** (se envía como string al crear, se consulta entre comillas simples en el filtro OData al reconciliar) — el default más común para un UDF custom sin tipo declarado explícitamente, pero **no confirmado contra la definición real del campo en la instalación de SAP de Daryza**, algo que solo se puede verificar inspeccionando el UDF en SAP B1 mismo (Herramientas → Personalización → Campos definidos por el usuario) o con una prueba real de creación. Si la prueba integral revela un rechazo específico de ese campo (mensaje típico: `"Invalid value..."` o `"Field cannot be updated..."` sobre `U_MSSL_PORTAL_ID`), es el primer punto a ajustar — cambiar `.ToString()` por el valor entero crudo en `CrearPreliminarAsync`, y quitar las comillas simples del filtro en `BuscarDefinitivoAsync`.

---

## 4. Variables de configuración (`App.config`)

`App.config` (real, con credenciales) **nunca se versiona** — está en `daemon-sap/.gitignore`. Solo se versiona `App.config.example`, con las mismas claves y valores vacíos. Cada máquina que compila/ejecuta el demonio necesita su propia copia local de `App.config` con los valores reales.

| Clave | Usada por | Propósito |
|---|---|---|
| `HanaServer` | `HanaConnectionManager.vb` | Host:puerto del servidor HANA (conexión SQL directa) |
| `HanaCompanyDB` | `HanaConnectionManager.vb` | Base de datos/schema de SAP a consultar por HANA — **debe apuntar a la misma base de datos SAP** que `ServiceLayerCompanyDB` (son 2 claves separadas porque HANA y Service Layer identifican la base de forma distinta, pero conceptualmente son "la misma SAP" — mantenerlas sincronizadas a mano al cambiar de entorno de pruebas a producción o viceversa) |
| `HanaDbUser` / `HanaDbPassword` | `HanaConnectionManager.vb` | Credenciales de la conexión SQL directa a HANA |
| `DaryzaApiToken` | `SyncService.vb`, `GrpoSyncService.vb`, `FacturaSyncService.vb` | Token DRF del usuario `daemon_sap` en el Portal Django — autentica todos los endpoints del Portal que consume este demonio (misma clave, un solo token, para las 3 clases) |
| `DaryzaApiBaseUrl` | `SyncService.vb`, `GrpoSyncService.vb`, `FacturaSyncService.vb` | URL base del Portal (`https://nexo.daryza.pe` en producción; una URL local tipo `http://127.0.0.1:8000` para desarrollo) |
| `ServiceLayerBaseUrl` | `SapServiceLayerClient.vb` | URL base de Service Layer, ej. `https://<host-interno>:50000/b1s/v1` — siempre una IP/host interno de la red de Daryza, nunca expuesto a Internet (certificado autofirmado, bypass de validación TLS deliberado, ver sección 1) |
| `ServiceLayerCompanyDB` | `SapServiceLayerClient.vb` | Nombre de la base de datos SAP para el login de Service Layer (`CompanyDB` del body de `/Login`) — debe coincidir conceptualmente con `HanaCompanyDB`, ver nota arriba |
| `ServiceLayerUser` / `ServiceLayerPassword` | `SapServiceLayerClient.vb` | Credenciales del usuario de Service Layer (licencia indirecta de SAP B1 — ver `CLAUDE.md`, sesión 86, punto 5, sobre el límite de sesiones concurrentes) |
| `FacturaReconciliacionIntervaloMinutos` | `FacturaSyncService.vb` | Sesión 98 (Etapa 5.3) — intervalo mínimo en minutos entre corridas del ciclo de reconciliación, gateado en memoria (ver sección 6). Si falta o no es un entero positivo, el código usa `30` por default — nunca falla por esta clave ausente |

**Entorno de pruebas vs. producción**: la única diferencia real entre ambos es el VALOR de estas 8 claves (más `HanaDbUser`/`HanaDbPassword`) — el código no tiene ninguna bifurcación explícita "si estoy en test, hago X". Al momento de escribir este documento, el `App.config` local de desarrollo apunta a la base de pruebas de SAP (`BK_1808261700`, confirmada como base de pruebas designada para las Etapas 3-6 — ver `CLAUDE.md`, sesión 86) y a un Portal local; el `App.config` real de producción (en la máquina donde corre el servicio Windows instalado) debería apuntar a la base productiva de SAP y a `https://nexo.daryza.pe` — **verificar explícitamente cuál `App.config` está activo en cada máquina antes de asumir contra qué entorno se está corriendo el demonio**, no hay ningún indicador visible en el propio proceso que lo distinga.

---

## 5. Flujo de una operación típica: crear un GRPO (Etapa 4, sesión 97; rediseñado sesión 99)

Esta sección describe el flujo **ya implementado y compilado (0 errores/0 warnings)**. Sigue disponible solo vía el flag manual `--sync-grpo` (ver 3.3) — no forma parte de `WorkerThread` todavía.

### 5.1 — Paso HUMANO en el Portal (sesión 99, NO daemon)

**Contexto de negocio**: los artículos de Daryza se gestionan por LOTE, y el lote lo ingresa Almacén al recibir la mercadería. Ese dato ahora se captura en el Portal, no directamente en SAP.

1. Un Ticket llega a `FINALIZADO` (Vigilancia registra la salida). El Portal genera automáticamente una `EntradaMercaderia` con la cantidad REAL inspeccionada por línea — pero en estado **`PENDIENTE`** (`estado_sap=''`), NO disponible todavía para el daemon.
2. Almacén / Materia Prima (el actor de recepción del Ticket, según `Ticket.es_materia_prima`) abre una pantalla de detalle en el Portal (`/operations/entrada-mercaderia/<id>/`, mismo layout visual que "Copiar de OC" de Factura): ajusta la cantidad si hace falta, completa el **número de lote** por línea (obligatorio) más fecha de vencimiento / fabricación (opcionales).
3. Almacén hace clic en **"Enviar a SAP B1"** → `services_entrada.enviar_a_sap` valida que TODAS las líneas tengan lote y hace la transición `estado` `PENDIENTE → ENVIADO`, `estado_sap '' → 'L'`. Recién ahí el daemon la ve.

### 5.2 — El daemon crea el GRPO real (UN SOLO PASO, sin Draft)

**El GRPO sigue siendo de un solo paso** (confirmado, sesión 86): el demonio crea el documento GRPO real (`POST /PurchaseDeliveryNotes`) y lo confirma en la misma corrida, con el mismo `DocEntry` en `confirmar-borrador` y `confirmar-definitivo` (a diferencia de Factura, que sí tiene Draft + definitivo). **NO se cambió a `POST /Drafts`** — decisión explícita del usuario en la sesión 99.

1. **`app/Service1.Designer.vb :: Main()`** — flag `--sync-grpo` (manual) instancia `GrpoSyncService` y llama `ExecuteGrpoSync().GetAwaiter().GetResult()`.
2. **`daemon_logical/GrpoSyncService.vb :: ExecuteGrpoSync()`**:
   a. `ObtenerEntradasPendientesAsync()` — `GET /api/v1/entradas-pendientes/` (el Portal solo devuelve las `estado_sap='L'`, ya con lote) → `List(Of EntradaMercaderiaDTO)`.
   b. **Una sola** instancia de `SapServiceLayerClient` + **una sola** de `GrpoService` para todo el ciclo.
   c. Por cada `EntradaMercaderia`, `ProcesarEntradaAsync()` mapea `entrada.lineas` → `GrpoLineaEntrada` — **incluyendo `NumeroLote`/`FechaVencimientoLote`/`FechaFabricacionLote`** (único punto de cruce de vocabularios, solo asignación de campos).
3. **`daemon_data/GrpoService.vb :: CrearGrpoAsync(cardCode, lineas)`** — arma `CardCode` + `DocumentLines` (`BaseType=22`, `BaseEntry`, `BaseLine`, `Quantity` — sin `ItemCode`, SAP lo deriva del "Copy From"). **Sesión 99:** por cada línea con `NumeroLote` no vacío, agrega `BatchNumbers=[{BatchNumber, Quantity, ExpiryDate?, ManufactureDate?}]`; una línea SIN lote NO recibe `BatchNumbers` (un ítem no gestionado por lote con `BatchNumbers` también hace que SAP rechace). Llama `_cliente.PostAsync("PurchaseDeliveryNotes", body)`.
4. **`daemon_data/SapServiceLayerClient.vb :: PostAsync()`** — `POST` real a Service Layer. `GrpoService` interpreta la respuesta: `Exitoso` + `DocEntry` en el body → `GrpoResult.Exitoso=True`, y **parsea también `DocNum`** de la misma respuesta (`GrpoResult.DocNum`, opcional — no invalida el éxito si faltara); si no, `Exitoso=False` con el mensaje real de SAP.
5. De vuelta en `GrpoSyncService.ProcesarEntradaAsync()`:
   - `Exitoso=True` → `ConfirmarAsync(entrada.id, "confirmar-borrador", "doc_entry_borrador", DocEntry, DocNum)` **y** `ConfirmarAsync(entrada.id, "confirmar-definitivo", "doc_entry_definitivo", DocEntry, DocNum)`. **Sesión 99:** el body de ambas confirmaciones ahora incluye `doc_num` (si SAP lo devolvió) — el Portal lo guarda en `EntradaMercaderia.doc_num_sap` y avanza `estado` a `CREADO_SAP` en `confirmar-definitivo`. Si `confirmar-borrador` falla (problema de red hacia el Portal, no de SAP), NO se llama `confirmar-definitivo` ni `reportar-error` — caso de borde conocido, la entrada queda en `'L'` y se reintentará.
   - `Exitoso=False` → `ReportarErrorAsync(entrada.id, ...)` → `POST .../reportar-error/` con el mensaje real de SAP, sin tocar `estado`/`estado_sap` — la entrada sigue disponible para reintento, el ciclo continúa con la siguiente. `enviar_a_sap` (paso 5.1) ya exige lote en todas las líneas, así que el caso "falta el lote" no debería llegar a SAP; un rechazo por otro motivo queda visible en `trazabilidad_ticket` para que un humano lo resuelva.
6. **`daemon_logical/Logger.vb`** — cada paso con prefijos `[GRPO-*]`.

**Separación de capas, verificada**: `daemon_data` (`GrpoService`) es la única capa que arma el payload de SAP (`BatchNumbers` incluido) y parsea su respuesta — `daemon_logical` (`GrpoSyncService`) nunca ve un `BaseType`/`DocumentLines`/`BatchNumbers` crudo, solo decide *cuándo* llamar y *con qué datos* (el mapeo DTO→`GrpoLineaEntrada`, dato, no lógica de payload). El consumo del Portal usa el mismo `HttpClient`+`Token`+reintento-con-backoff de `sync-oc`.

**Estado real de la prueba**: código compilado (0/0). **No probado contra datos reales todavía** — requiere una de las OC reales de SAP (`79001066`/`79000775`/`16089947`) con un Ticket ya `FINALIZADO` y su lote completado en el Portal. El hallazgo de la sesión 97 (las 5 `EntradaMercaderia` disponibles estaban sobre OC sintéticas — ver 3.3) sigue vigente: sin una OC genuinamente sincronizada desde SAP, el "Copy From" seguirá siendo rechazado por SAP, ahora por `CardCode`/`ItemCode` inexistente antes que por el lote.

**Lo que NO cambia**: la capa HANA (`SyncSAPB1`) no participa — la creación del GRPO es 100% vía Service Layer, tal como establece la regla dura de la sección 1. Y la única incógnita de SAP que queda abierta en esta etapa: si un artículo gestionado por lote de Daryza exige además atributos de lote obligatorios (fecha de vencimiento / fabricación) — se envían `ExpiryDate`/`ManufactureDate` solo si Almacén los completó; si SAP rechaza por un atributo de lote obligatorio faltante, el mensaje llega vía `reportar-error` y Almacén completa la fecha en el Portal.

---

## 6. Flujo de una operación típica: crear/reconciliar/cancelar el Preliminar de Factura (Etapa 5 — CÓDIGO EXISTENTE, sesión 98)

Esta sección describe los 3 flujos **ya implementados, compilados (0 errores/0 warnings) y ejercitados contra el Portal real** (sesión 98) — sin datos calificados todavía del lado de SAP (ver sección 3.4 para el estado real de la prueba). Los 3 siguen disponibles solo vía sus flags manuales respectivos — ninguno forma parte de `WorkerThread` todavía.

**Contexto de negocio**: cuando Compras aprueba una Factura en el Portal (`estado='APROBADA_COMPRAS'`), queda con `estado_sap='L'` — lista para que el demonio cree su Preliminar en SAP ("Copy From" el GRPO ya confirmado, no la OC directamente). A diferencia de GRPO (flujo de un solo paso), Factura tiene **2 documentos reales distintos** — el Preliminar (Draft) y el documento definitivo, que Contabilidad crea/convierte directamente en SAP, **fuera del Portal** — de ahí que el ciclo de reconciliación sea un ciclo *separado*, más lento, que solo *busca* si ya apareció.

### 6.1 — Sub-etapa 5.1/5.2: creación del Preliminar

1. **`app/Service1.Designer.vb :: Main()`** — flag `--sync-factura-preliminar` instancia `FacturaSyncService` y llama `ExecutePreliminarSync().GetAwaiter().GetResult()`.
2. **`daemon_logical/FacturaSyncService.vb :: ExecutePreliminarSync()`**:
   a. `GET /api/v1/facturas-pendientes-preliminar/` → deserializa a `List(Of FacturaPreliminarDTO)` (`daemon_entity`).
   b. Crea **una sola** instancia de `SapServiceLayerClient` para todo el ciclo + **una sola** instancia de `FacturaService` que la consume.
   c. Por cada Factura, `ProcesarPreliminarAsync()`:
      - **Precondición de negocio** (decisión de esta capa, no de `FacturaService`): si alguna línea trae `grpo_doc_entry` nulo (el GRPO correspondiente todavía no está confirmado como documento definitivo del lado del Portal — dato expuesto tal cual, sin filtrar, ver `apps/invoicing/serializers.py`), se **salta** esta Factura — sin llamar a SAP ni a `reportar-error/`, se reintentará en el próximo ciclo. No es un rechazo, es "todavía no está lista".
      - Mapea `factura.lineas` (`FacturaLineaPreliminarDTO`) a `FacturaPreliminarLinea` y la cabecera a `FacturaPreliminarCabecera` (`daemon_data`) — único punto donde se cruzan ambos vocabularios.
3. **`daemon_data/FacturaService.vb :: CrearPreliminarAsync(cabecera, lineas)`** — arma el Draft real: `DocObjectCode="oPurchaseInvoices"`, `CardCode`/`NumAtCard`/`DocCurrency`/`TaxDate`/`DocDueDate` (estos 2 últimos omitidos del payload si vienen nulos), `U_MSSL_FPC`/`FNC`/`TOP`/`CBS`/`PORTAL_ID`, `DocumentLines` con una entrada por línea (**`BaseType=20`** — Purchase Delivery Note/GRPO, confirmado sección 3.4 —, `BaseEntry`=DocEntry real del GRPO, `BaseLine`, `Quantity`/`UnitPrice` = los valores REALES facturados, no la cantidad completa del GRPO), y llama `_cliente.PostAsync("Drafts", body)`.
4. **`daemon_data/SapServiceLayerClient.vb :: PostAsync()`** — mismo mecanismo ya usado por GRPO. `FacturaService` interpreta la respuesta: `DocEntry` real en éxito, mensaje real de SAP en rechazo.
5. De vuelta en `FacturaSyncService.ProcesarPreliminarAsync()`:
   - Éxito → `POST .../confirmar-preliminar/` con el `DocEntry` real. Si esa confirmación falla (problema de comunicación con el Portal, no de SAP), el Preliminar SÍ existe en SAP pero el Portal no lo sabe — mismo caso de borde ya documentado para GRPO, no resuelto en esta sub-etapa.
   - Rechazo → `POST .../reportar-error/` con el mensaje real, sin tocar `estado_sap` — se reintentará.
6. **`Logger.vb`** — cada paso con prefijo `[FACTURA-PRELIMINAR-*]`.

### 6.2 — Sub-etapa 5.3: reconciliación (ciclo separado, gateado por intervalo)

`ExecuteReconciliacionSync()` — antes de tocar nada, compara `Date.Now` contra la última corrida real (`_ultimaReconciliacion`, campo estático en memoria) más `FacturaReconciliacionIntervaloMinutos`; si no pasó suficiente tiempo, registra `[FACTURA-RECONCILIACIÓN-SALTADA]` y retorna sin llamar a SAP ni al Portal. Con el único camino de invocación que existe hoy (el flag manual, un proceso nuevo por corrida), el campo siempre arranca en `Nothing` — el gate solo se vuelve activo el día que este método se llame repetidamente desde un proceso de larga vida (`WorkerThread`, si se wirea a futuro).

Pasado el gate: `GET /api/v1/facturas-preliminares/` (Facturas con `estado_sap='B'`) → por cada una, `FacturaService.BuscarDefinitivoAsync(factura.id)` — `GET PurchaseInvoices?$filter=U_MSSL_PORTAL_ID eq '{id}'&$select=DocEntry` (colección real `PurchaseInvoices`, nunca `/Drafts` — el documento que se busca es el que Contabilidad ya convirtió/creó de verdad). Si aparece, `POST .../confirmar-definitivo/` con el `DocEntry` real. Si no aparece **sin ningún error de comunicación** (`Encontrado=False`, `MensajeError=Nothing`), es el caso normal — Contabilidad todavía no lo creó — se registra `[FACTURA-RECONCILIACIÓN-ESPERA]` y se sigue con la siguiente, **sin** llamar a ningún `reportar-error/` (esa etapa no tiene ese endpoint del lado del Portal — confirmado en `api/factura_api.py` antes de escribir este código, no asumido).

### 6.3 — Sub-etapa 5.4: cancelación

`ExecuteCancelacionSync()` — `GET /api/v1/facturas-pendientes-cancelacion/` (`estado='CANCELADO'` + `estado_sap='B'`) → por cada una, parsea `doc_entry_preliminar` (string del lado Django) a entero; si falla el parseo, se registra y se omite. `FacturaService.CancelarPreliminarAsync(docEntry)` — `DELETE /Drafts({docEntry})` (un Draft se anula borrándolo, no vía un endpoint de "Cancel" — ese patrón aplica a documentos ya posteados, no a borradores). Si SAP acepta, `POST .../confirmar-cancelacion/` (sin body — cancelar no genera ningún `DocEntry` nuevo). Mismo criterio que reconciliación: sin `reportar-error/` para esta etapa, un fallo solo se loggea localmente y se reintenta en el próximo ciclo.

### Separación de capas, verificada, no solo declarada

`daemon_data` (`FacturaService`) es la única capa que arma cualquier payload de SAP (el Draft, el filtro OData) y parsea cualquier respuesta de Service Layer — `daemon_logical` (`FacturaSyncService`) nunca ve un `DocObjectCode`/`DocumentLines`/`$filter` ni un JSON crudo de SAP, solo decide *cuándo* llamar (incluida la decisión de negocio de "línea sin GRPO confirmado, todavía no" y el gate de intervalo de reconciliación) y *con qué datos* (el mapeo DTO→tipos de `daemon_data`, que es dato, no lógica de payload). El consumo del Portal replica el mismo mecanismo `HttpClient`+`Token`+reintento-con-backoff ya usado por `sync-oc`/GRPO — ninguna clase nueva, ningún mecanismo distinto. Tipos propios de cada capa (`FacturaPreliminarCabecera`/`FacturaPreliminarLinea`/`*Result` en `daemon_data`; `FacturaPreliminarDTO`/`FacturaLineaPreliminarDTO`/etc. en `daemon_entity`; `PortalResult` reutilizado de `GrpoSyncService.vb`, mismo proyecto `daemon_logical`) — sin ninguna referencia de `daemon_data` hacia `daemon_entity`, mismo criterio ya establecido por `GrpoService`.

**Resultado real de la prueba (sesión 98)**: los 3 flags se ejecutaron contra el Portal real — las 3 listas de "pendientes" están vacías hoy (0 Facturas reales en `APROBADA_COMPRAS`/`estado_sap='L'` en la BD local; el flujo completo de Facturación con datos 100% reales tampoco se ha corrido todavía). Verificado que esto es un resultado limpio de "sin trabajo pendiente" y no un fallo silencioso — se corrompió deliberadamente el token del daemon y se re-corrió `--sync-factura-preliminar`, confirmando un `401` real, correctamente logueado (`[FACTURA-API-FAIL]`), sin reintento (4xx); restaurado el token real, la misma corrida vuelve a terminar limpia sin ningún log de error. Compilación real 0/0, sin ningún proceso residual al cierre. La prueba de creación de un Preliminar real (con `DocEntry` real) queda pendiente, igual que el resto de la prueba integral de esta etapa — hace falta una Factura real (proveedor → "Copiar de OC(s)" → 3 archivos → aprobación de Compras) sobre una OC/GRPO genuinamente sincronizados desde SAP.

**Lo que NO cambia de este flujo**: la capa HANA (`SyncSAPB1`) no participa en absoluto — la creación/reconciliación/cancelación del Preliminar es 100% vía Service Layer, tal como establece la regla dura de la sección 1.
