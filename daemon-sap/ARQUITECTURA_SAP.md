# Arquitectura de la integración con SAP Business One — `daemon-sap`

Documento de referencia técnica, no narrativo — para eso está `CLAUDE.md` (historial de sesiones). Este archivo describe el estado de la arquitectura **al cierre de la Etapa 4** (sesión 97): SQL directo contra HANA para lectura masiva de OC (Etapa 1, en producción), un cliente de sesión de Service Layer ya funcional (Etapa 3.1/3.2, todavía solo accesible vía un flag manual de prueba `--test-service-layer`), y ahora también la **creación real de GRPO** (Etapa 4) — código completo, compilado, y ejercitado contra el Portal real y Service Layer real (sección 5). Sigue **sin estar conectada al ciclo automático** de `WorkerThread` — se dispara solo a mano, vía `--sync-grpo`, mismo criterio deliberado que `--test-service-layer` (ver sección 3.3). La Etapa 5 (Factura Preliminar) está diseñada y confirmada sin preguntas abiertas, pero sin ningún código todavía.

Léase junto con `CLAUDE.md` → sección "Estructura del repositorio" → "Plan de integración VB.NET ↔ Portal" (la tabla de 6 etapas y las decisiones de diseño ya confirmadas).

---

## 1. Diagrama de capas

Este proyecto habla con SAP B1 por **2 mecanismos completamente distintos, que nunca deben mezclarse**:

- **SQL directo contra HANA** (`Sap.Data.Hana`, ADO.NET) — lectura masiva rápida de datos ya existentes en SAP (cabeceras/líneas de OC), y una única escritura de control (marcar un UDF de flag, `U_DRYZ_PC`). **Nunca se usa, y nunca debe usarse, para crear documentos de negocio nuevos** (Entradas de Mercancía, Facturas) — un `INSERT`/lógica SQL directa no replica la numeración de series, los asientos contables, los movimientos de inventario ni las validaciones que SAP B1 aplica internamente al crear un documento real; eso es exactamente el trabajo que hace la capa de negocio de SAP, a la que solo se accede vía Service Layer o DI API — nunca escribiendo filas a mano en las tablas base.
- **SAP Service Layer** (REST/OData sobre HTTPS, login con sesión) — el mecanismo oficial de SAP para *crear* documentos de negocio nuevos (GRPO, Factura Preliminar — Etapas 4/5) respetando toda esa lógica interna. Hoy también se usa para una lectura de prueba (`BusinessPartners`), pero su razón de ser en este proyecto es la escritura.

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
│                                                                        │
│  Service1.vb :: OnStart() → arranca WorkerThread (hilo dedicado)      │
│  Service1.vb :: WorkerThread → loop: ExecuteSync() ; Sleep(17s)       │
│    (GrpoSyncService NO forma parte de este loop todavía — ver 3.3)    │
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
│  Logger.vb — Logs/Log_YYYY-MM-DD.txt, usado por los 3 de arriba       │
└──────┬───────────────────────────────────────────┬───────────────────┘
       │                                            │
       ▼ SQL directo (lectura masiva + 1 flag)      ▼ Service Layer (sesión HTTP)
┌──────────────────────────┐              ┌──────────────────────────────┐
│  daemon_data               │              │  daemon_data                  │
│  SyncSAPB1.vb               │              │  SapServiceLayerClient.vb      │
│  HanaConnectionManager.vb   │              │  (login manual de cookies,     │
│                             │              │   re-auth automático en 401,   │
│                             │              │   GetAsync + PostAsync         │
│                             │              │   comparten un mismo helper    │
│                             │              │   de reintento — sesión 97)    │
│                             │              │  GrpoService.vb (Etapa 4):     │
│                             │              │  arma el payload "Copy From"   │
│                             │              │  (BaseType=22) y llama a       │
│                             │              │  PostAsync("PurchaseDelivery-  │
│                             │              │  Notes", ...) — consume        │
│                             │              │  SapServiceLayerClient por     │
│                             │              │  composición, no lo extiende   │
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
| `daemon_logical/GrpoSyncService.vb` | **Etapa 4 (sesión 97).** Orquesta el ciclo de creación de GRPO: `GET entradas-pendientes/`, mapea el DTO del Portal a `GrpoLineaEntrada` (único punto del proyecto donde se cruzan el vocabulario del Portal y el de SAP — solo asignación de campos, sin lógica), delega la creación real a `GrpoService` (`daemon_data`), y confirma/reporta error de vuelta al Portal. Reintento con backoff propio (`EnviarAlPortalConReintentoAsync`, mismo criterio que `AwakeApiPost`), un fallo por entrada no interrumpe el resto del ciclo. **Solo se dispara manualmente** (`--sync-grpo`) — no forma parte de `WorkerThread` todavía (ver sección 3.3) | Portal (HttpClient propio) — nunca habla con SAP directamente, delega en `GrpoService` | `GET /api/v1/entradas-pendientes/`, `POST .../confirmar-borrador/`, `POST .../confirmar-definitivo/`, `POST .../reportar-error/` |
| `daemon_logical/Logger.vb` | Escribe `Logs/Log_YYYY-MM-DD.txt` (append, con limpieza automática >30 días) — usado por `SyncService`, `ServiceLayerSmokeTest` y `GrpoSyncService` | — (sistema de archivos local) | — |
| `daemon_data/SyncSAPB1.vb` | Consultas SQL directas contra HANA: `GetPendingHeaders` (OC con `U_DRYZ_PC='1'`), `GetLinesByDocEntry` (líneas + precio/IGV/moneda reales de SAP), `MarkAsSynced` (única escritura: `UPDATE OPOR SET U_DRYZ_PC='2'`) | HANA (SQL directo, vía `HanaConnectionManager`) | Alimenta el payload de `POST /api/v1/sync-oc/` (no llama al Portal directamente — eso lo hace `SyncService`) |
| `daemon_data/HanaConnectionManager.vb` | Abre/cierra conexiones ADO.NET a HANA; lee `HanaServer`/`HanaCompanyDB`/`HanaDbUser`/`HanaDbPassword` de `App.config` | HANA | — |
| `daemon_data/SapServiceLayerClient.vb` | Cliente de sesión de Service Layer: `LoginAsync` (POST `/Login`, extrae `B1SESSION`/`ROUTEID` a mano de los `Set-Cookie` crudos), `GetAsync`/`PostAsync` (ambos delegan en un mismo helper privado de reintento-en-401, agregado en la sesión 97 al introducir `PostAsync` — antes solo existía para `GetAsync`). Sesión única reutilizada mientras siga vigente — no logueo por cada llamada | Service Layer (REST/OData sobre HTTPS) | — (sin endpoint del Portal — es la capa de conexión pura a SAP; la usan `ServiceLayerSmokeTest` y `GrpoService` por composición) |
| `daemon_data/GrpoService.vb` | **Etapa 4 (sesión 97).** Arma el payload "Copy From" (`CardCode` + `DocumentLines` con `BaseType=22`/`BaseEntry`/`BaseLine`/`Quantity` — sin `ItemCode` explícito, SAP lo deriva) y llama `SapServiceLayerClient.PostAsync("PurchaseDeliveryNotes", ...)`. Consume `SapServiceLayerClient` por composición (recibe una instancia ya lista por constructor — la misma sesión que usa todo el ciclo), nunca lo extiende ni lo reimplementa. Sin dependencia de `daemon_entity` a propósito — `GrpoLineaEntrada` es un tipo propio y mínimo, mantiene esta capa desacoplada de los DTOs del Portal | Service Layer (vía `SapServiceLayerClient`, nunca directo) | — |
| `daemon_data/HanaService.vb` | ⚠️ **Código huérfano — no compila, nunca corre.** No está referenciado en `daemon_data.vbproj` (`<Compile Include>`). Consulta una vista distinta (`VW_DRYZ_FE`, facturas de venta/AR) de un contexto no relacionado con este demonio, y depende de tipos (`MF_Entities.FiltroDTO`/`Documento`) que ni siquiera existen en este proyecto. Documentado acá explícitamente para que nadie lo confunda con código activo ni pierda tiempo debugueándolo | (ninguno — no compila) | — |
| `daemon_entity/PurchaseOrderDTO.vb` | DTOs `PurchaseOrderDTO`/`PurchaseOrderLineDTO` — forma exacta del JSON enviado a `/sync-oc/`, nombres de propiedad en snake_case coincidiendo 1:1 con el serializer real de Django (`apps/sap_sync/serializers.py`) | — (solo forma de datos, sin lógica ni llamadas) | Alimenta `POST /api/v1/sync-oc/` |
| `daemon_entity/EntradaMercaderiaDTO.vb` | **Etapa 4 (sesión 97).** DTOs `EntradaMercaderiaDTO`/`EntradaMercaderiaLineaDTO` — forma exacta del JSON que devuelve `GET /api/v1/entradas-pendientes/`, nombres de propiedad en snake_case coincidiendo 1:1 con `apps/operations/serializers.py::EntradaMercaderiaSerializer` | — (solo forma de datos) | Deserializado desde `GET /api/v1/entradas-pendientes/` |

**Endpoints del Portal que YA EXISTEN del lado Django pero que este demonio todavía NO consume** (construidos en la Fase 3 de Facturación electrónica, Django-only hasta ahora — ver `CLAUDE.md` raíz del Portal, sesiones 57-58 y 82-83): los 3 equivalentes de Factura (`GET facturas-pendientes-preliminar/`, `GET facturas-preliminares/`, `GET facturas-pendientes-cancelacion/`, con sus `POST` de confirmación/error). Consumirlos es exactamente el trabajo de la Etapa 5 — ver sección 5. Los 4 endpoints de Entrada de Mercancía/GRPO (`entradas-pendientes`, `confirmar-borrador`, `confirmar-definitivo`, `reportar-error`) **ya están consumidos** desde la Etapa 4 (tabla de arriba).

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

---

## 4. Variables de configuración (`App.config`)

`App.config` (real, con credenciales) **nunca se versiona** — está en `daemon-sap/.gitignore`. Solo se versiona `App.config.example`, con las mismas claves y valores vacíos. Cada máquina que compila/ejecuta el demonio necesita su propia copia local de `App.config` con los valores reales.

| Clave | Usada por | Propósito |
|---|---|---|
| `HanaServer` | `HanaConnectionManager.vb` | Host:puerto del servidor HANA (conexión SQL directa) |
| `HanaCompanyDB` | `HanaConnectionManager.vb` | Base de datos/schema de SAP a consultar por HANA — **debe apuntar a la misma base de datos SAP** que `ServiceLayerCompanyDB` (son 2 claves separadas porque HANA y Service Layer identifican la base de forma distinta, pero conceptualmente son "la misma SAP" — mantenerlas sincronizadas a mano al cambiar de entorno de pruebas a producción o viceversa) |
| `HanaDbUser` / `HanaDbPassword` | `HanaConnectionManager.vb` | Credenciales de la conexión SQL directa a HANA |
| `DaryzaApiToken` | `SyncService.vb` | Token DRF del usuario `daemon_sap` en el Portal Django — autentica `POST /api/v1/sync-oc/` |
| `DaryzaApiBaseUrl` | `SyncService.vb` | URL base del Portal (`https://nexo.daryza.pe` en producción; una URL local tipo `http://127.0.0.1:8000` para desarrollo) |
| `ServiceLayerBaseUrl` | `SapServiceLayerClient.vb` | URL base de Service Layer, ej. `https://<host-interno>:50000/b1s/v1` — siempre una IP/host interno de la red de Daryza, nunca expuesto a Internet (certificado autofirmado, bypass de validación TLS deliberado, ver sección 1) |
| `ServiceLayerCompanyDB` | `SapServiceLayerClient.vb` | Nombre de la base de datos SAP para el login de Service Layer (`CompanyDB` del body de `/Login`) — debe coincidir conceptualmente con `HanaCompanyDB`, ver nota arriba |
| `ServiceLayerUser` / `ServiceLayerPassword` | `SapServiceLayerClient.vb` | Credenciales del usuario de Service Layer (licencia indirecta de SAP B1 — ver `CLAUDE.md`, sesión 86, punto 5, sobre el límite de sesiones concurrentes) |

**Entorno de pruebas vs. producción**: la única diferencia real entre ambos es el VALOR de estas 8 claves (más `HanaDbUser`/`HanaDbPassword`) — el código no tiene ninguna bifurcación explícita "si estoy en test, hago X". Al momento de escribir este documento, el `App.config` local de desarrollo apunta a la base de pruebas de SAP (`BK_1808261700`, confirmada como base de pruebas designada para las Etapas 3-6 — ver `CLAUDE.md`, sesión 86) y a un Portal local; el `App.config` real de producción (en la máquina donde corre el servicio Windows instalado) debería apuntar a la base productiva de SAP y a `https://nexo.daryza.pe` — **verificar explícitamente cuál `App.config` está activo en cada máquina antes de asumir contra qué entorno se está corriendo el demonio**, no hay ningún indicador visible en el propio proceso que lo distinga.

---

## 5. Flujo de una operación típica: crear un GRPO (Etapa 4 — CÓDIGO EXISTENTE, sesión 97)

Esta sección describe el flujo **ya implementado, compilado (0 errores/0 warnings) y ejercitado contra el Portal real y Service Layer real** (sesión 97). Sigue disponible solo vía el flag manual `--sync-grpo` (ver 3.3) — no forma parte de `WorkerThread` todavía.

**Contexto de negocio**: cuando un Ticket del Portal llega a `FINALIZADO` (Vigilancia registra la salida), el Portal ya generó automáticamente una `EntradaMercaderia` en estado `'L'` (pendiente) con la cantidad REAL inspeccionada por línea — no la cantidad de la OC. El demonio crea el documento GRPO real en SAP basado en esos datos, y confirma de vuelta al Portal el `DocEntry` real que SAP le asignó. Es un **flujo de un solo paso** (confirmado, sesión 86): el demonio crea el documento GRPO real y lo confirma en la misma corrida, con el mismo `DocEntry` en ambas llamadas al Portal (a diferencia de Factura, que sí tiene 2 documentos distintos — preliminar y definitivo).

**Pasos, con los archivos reales involucrados en orden:**

1. **`app/Service1.Designer.vb :: Main()`** — flag `--sync-grpo` (manual, ver 3.3) instancia `GrpoSyncService` y llama `ExecuteGrpoSync().GetAwaiter().GetResult()`. Deliberadamente NO wireado a `Service1.vb :: WorkerThread` todavía.
2. **`daemon_logical/GrpoSyncService.vb :: ExecuteGrpoSync()`** — orquesta el ciclo completo:
   a. `ObtenerEntradasPendientesAsync()` — `GET /api/v1/entradas-pendientes/` (vía el `HttpClient` propio de esta clase, mismo patrón ya usado por `SyncService.vb` para `sync-oc`, no un mecanismo distinto) → deserializa a `List(Of EntradaMercaderiaDTO)` (`daemon_entity`).
   b. Crea **una sola** instancia de `SapServiceLayerClient` para todo el ciclo (una sesión, reutilizada para cada entrada — mismo criterio ya establecido, sesión 86 punto 5) y **una sola** instancia de `GrpoService` que la consume.
   c. Por cada `EntradaMercaderia`, `ProcesarEntradaAsync()` mapea `entrada.lineas` (`EntradaMercaderiaLineaDTO`) a `GrpoLineaEntrada` (`daemon_data`) — el único punto del proyecto donde el vocabulario del Portal y el de SAP se cruzan, y es solo una asignación de campos, sin ninguna lógica de negocio ni de HTTP.
3. **`daemon_data/GrpoService.vb :: CrearGrpoAsync(cardCode, lineas)`** — arma el payload real: `CardCode`, `DocumentLines` con una entrada por línea (`BaseType=22`, `BaseEntry=doc_entry_oc`, `BaseLine=base_line`, `Quantity=cantidad` — **sin `ItemCode`**, SAP lo deriva del "Copy From"), y llama `_cliente.PostAsync("PurchaseDeliveryNotes", body)`.
4. **`daemon_data/SapServiceLayerClient.vb :: PostAsync()`** — envía el `POST` real a Service Layer (mismo helper de reintento-en-401 que ya usaba `GetAsync`), devuelve un `ServiceLayerResult`. `GrpoService` interpreta la respuesta: si `Exitoso` y el body trae `DocEntry`, `GrpoResult.Exitoso=True` con ese `DocEntry` real; si no, `GrpoResult.Exitoso=False` con el mensaje real de SAP (nunca inventado).
5. De vuelta en `GrpoSyncService.ProcesarEntradaAsync()`:
   - Si `GrpoResult.Exitoso=True` → `ConfirmarAsync(entrada.id, "confirmar-borrador", ...)` **y** `ConfirmarAsync(entrada.id, "confirmar-definitivo", ...)` al Portal, ambos con el mismo `DocEntry` real (flujo de un solo paso). Si `confirmar-borrador` falla (problema de red hacia el Portal, no de SAP), NO se llama `confirmar-definitivo` ni `reportar-error` — el GRPO ya existe en SAP pero el Portal no lo sabe todavía; la entrada queda en `'L'` y se reintentará en el próximo ciclo (caso de borde conocido, documentado en el propio código, no resuelto en esta sub-etapa).
   - Si `GrpoResult.Exitoso=False` → `ReportarErrorAsync(entrada.id, resultado.MensajeError)` → `POST .../reportar-error/` al Portal con el mensaje real de SAP, sin tocar `estado_sap` — la entrada sigue disponible para reintento, y el ciclo continúa con la siguiente entrada (un fallo no interrumpe a las demás, mismo criterio ya usado en `sync-proveedores`).
6. **`daemon_logical/Logger.vb`** — cada paso relevante (inicio de ciclo, procesando, éxito, fallo de SAP, fallo de red hacia el Portal, confirmado) queda registrado con prefijos `[GRPO-*]`, mismo criterio ya aplicado en `SyncService.vb`/`ServiceLayerSmokeTest.vb`.

**Separación de capas, verificada, no solo declarada**: `daemon_data` (`GrpoService`) es la única capa que arma el payload de SAP y parsea su respuesta — `daemon_logical` (`GrpoSyncService`) nunca ve un `BaseType`/`DocumentLines` ni un JSON crudo de Service Layer, solo decide *cuándo* llamar y *con qué datos* (el mapeo DTO→`GrpoLineaEntrada`, que es dato, no lógica de payload). El consumo del Portal usa el mismo mecanismo `HttpClient`+`Token`+reintento-con-backoff ya establecido para `sync-oc`, sin inventar un segundo mecanismo de comunicación HTTP. `GrpoLineaEntrada`/`GrpoResult` (tipos de `daemon_data`) y `PortalResult` (tipo nuevo de `daemon_logical`, deliberadamente distinto de `ServiceLayerResult` aunque tenga la misma forma, para no mezclar vocabulario de 2 capas distintas) mantienen cada capa hablando su propio idioma.

**Resultado real de la prueba (sesión 97, ver 3.3 para el detalle completo)**: 5/5 `EntradaMercaderia` disponibles fueron rechazadas por SAP real, con mensajes reales — los datos de origen resultaron ser sintéticos (OC nunca sincronizadas realmente desde SAP), no un bug de este código. El camino de error (pasos 5/`reportar-error`) quedó probado de punta a punta con datos 100% reales; el camino de éxito (un `DocEntry` real creado) queda pendiente de una `EntradaMercaderia` construida sobre una OC genuinamente sincronizada.

**Lo que NO cambia de este flujo**: la capa HANA (`SyncSAPB1`) no participa en absoluto — la creación del GRPO es 100% vía Service Layer, tal como establece la regla dura de la sección 1.
