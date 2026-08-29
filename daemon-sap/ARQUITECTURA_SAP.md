# Arquitectura de la integración con SAP Business One — `daemon-sap`

Documento de referencia técnica, no narrativo — para eso está `CLAUDE.md` (historial de sesiones). Este archivo describe el estado de la arquitectura **al cierre de la Etapa 3** (sesión 95): SQL directo contra HANA para lectura masiva de OC (Etapa 1, en producción), y un cliente de sesión de Service Layer ya funcional (Etapa 3.1/3.2) pero **todavía no conectado** al ciclo real del demonio — hoy solo se usa vía un flag manual de prueba (`--test-service-layer`). Las Etapas 4-6 (creación real de GRPO/Factura en SAP) están diseñadas y confirmadas sin preguntas abiertas, pero **sin ningún código todavía** — la sección 5 de este documento describe ese flujo como diseño, no como código existente.

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
│                                                                        │
│  Service1.vb :: OnStart() → arranca WorkerThread (hilo dedicado)      │
│  Service1.vb :: WorkerThread → loop: ExecuteSync() ; Sleep(17s)       │
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
│  Logger.vb — Logs/Log_YYYY-MM-DD.txt, usado por ambos de arriba       │
└──────┬───────────────────────────────────────────┬───────────────────┘
       │                                            │
       ▼ SQL directo (lectura masiva + 1 flag)      ▼ Service Layer (sesión HTTP)
┌──────────────────────────┐              ┌──────────────────────────────┐
│  daemon_data              │              │  daemon_data                  │
│  SyncSAPB1.vb              │              │  SapServiceLayerClient.vb      │
│  HanaConnectionManager.vb  │              │  (login manual de cookies,     │
│                            │              │   re-auth automático en 401)   │
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
| `daemon_logical/Logger.vb` | Escribe `Logs/Log_YYYY-MM-DD.txt` (append, con limpieza automática >30 días) — usado por `SyncService` y `ServiceLayerSmokeTest` | — (sistema de archivos local) | — |
| `daemon_data/SyncSAPB1.vb` | Consultas SQL directas contra HANA: `GetPendingHeaders` (OC con `U_DRYZ_PC='1'`), `GetLinesByDocEntry` (líneas + precio/IGV/moneda reales de SAP), `MarkAsSynced` (única escritura: `UPDATE OPOR SET U_DRYZ_PC='2'`) | HANA (SQL directo, vía `HanaConnectionManager`) | Alimenta el payload de `POST /api/v1/sync-oc/` (no llama al Portal directamente — eso lo hace `SyncService`) |
| `daemon_data/HanaConnectionManager.vb` | Abre/cierra conexiones ADO.NET a HANA; lee `HanaServer`/`HanaCompanyDB`/`HanaDbUser`/`HanaDbPassword` de `App.config` | HANA | — |
| `daemon_data/SapServiceLayerClient.vb` | Cliente de sesión de Service Layer: `LoginAsync` (POST `/Login`, extrae `B1SESSION`/`ROUTEID` a mano de los `Set-Cookie` crudos), `GetAsync` (con re-login automático ante un 401). Sesión única reutilizada mientras siga vigente — no logueo por cada llamada | Service Layer (REST/OData sobre HTTPS) | — (sin endpoint del Portal todavía; las Etapas 4/5 lo usarán para `POST`/`PATCH` de GRPO/Factura) |
| `daemon_data/HanaService.vb` | ⚠️ **Código huérfano — no compila, nunca corre.** No está referenciado en `daemon_data.vbproj` (`<Compile Include>`). Consulta una vista distinta (`VW_DRYZ_FE`, facturas de venta/AR) de un contexto no relacionado con este demonio, y depende de tipos (`MF_Entities.FiltroDTO`/`Documento`) que ni siquiera existen en este proyecto. Documentado acá explícitamente para que nadie lo confunda con código activo ni pierda tiempo debugueándolo | (ninguno — no compila) | — |
| `daemon_entity/PurchaseOrderDTO.vb` | DTOs `PurchaseOrderDTO`/`PurchaseOrderLineDTO` — forma exacta del JSON enviado a `/sync-oc/`, nombres de propiedad en snake_case coincidiendo 1:1 con el serializer real de Django (`apps/sap_sync/serializers.py`) | — (solo forma de datos, sin lógica ni llamadas) | Alimenta `POST /api/v1/sync-oc/` |

**Endpoints del Portal que YA EXISTEN del lado Django pero que este demonio todavía NO consume** (construidos en la Fase 3 de Facturación electrónica, Django-only hasta ahora — ver `CLAUDE.md` raíz del Portal, sesiones 57-58 y 82-83): `GET /api/v1/entradas-pendientes/`, `POST .../confirmar-borrador/`, `POST .../confirmar-definitivo/`, `POST .../reportar-error/` (Entrada de Mercancía / GRPO) y los 3 equivalentes de Factura (`facturas-pendientes-preliminar`, `facturas-preliminares`, `facturas-pendientes-cancelacion`). Consumirlos es exactamente el trabajo de las Etapas 4/5 — ver sección 5.

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

## 5. Flujo de una operación típica: crear un GRPO (Etapa 4 — DISEÑO CONFIRMADO, TODAVÍA SIN CÓDIGO)

Esta sección describe el flujo **ya diseñado y confirmado sin preguntas abiertas** (`CLAUDE.md`, sesión 86) para la Etapa 4, como mapa de navegación para cuando se implemente — **nada de esto existe como código todavía**. Sirve también como referencia para rastrear un error real una vez que sí exista.

**Contexto de negocio**: cuando un Ticket del Portal llega a `FINALIZADO` (Vigilancia registra la salida), el Portal ya generó automáticamente una `EntradaMercaderia` en estado `'L'` (pendiente) con la cantidad REAL inspeccionada por línea — no la cantidad de la OC. El demonio debe crear el documento GRPO real en SAP basado en esos datos, y confirmarle de vuelta al Portal el `DocEntry` real que SAP le asignó. Es un **flujo de un solo paso** (confirmado, sesión 86): el demonio crea el documento GRPO real y lo confirma en la misma corrida, con el mismo `DocEntry` en ambas llamadas al Portal (a diferencia de Factura, que sí tiene 2 documentos distintos — preliminar y definitivo).

**Pasos, con los archivos involucrados en orden:**

1. **`Service1.vb :: WorkerThread`** — dispara el ciclo (mismo hilo/loop ya existente de la Etapa 1, o uno nuevo dedicado a GRPO — decisión de implementación, no de diseño de negocio).
2. **Nuevo método en `daemon_logical`** (ej. `GrpoSyncService.vb`, todavía sin crear) — orquesta:
   a. `GET /api/v1/entradas-pendientes/` (vía un cliente HTTP hacia el Portal, mismo patrón ya usado por `SyncService.vb` para `sync-oc` — reutilizar ese `HttpClient`, no crear uno nuevo) → devuelve las `EntradaMercaderia` en estado `'L'`, con `card_code`, `card_name`, y sus `lineas` (`doc_entry_oc`, `doc_num_oc`, `base_line`, `item_code`, `cantidad` — campos reales ya confirmados en `apps/operations/serializers.py::EntradaMercaderiaSerializer`).
   b. Por cada `EntradaMercaderia` pendiente, arma el payload de creación del GRPO para Service Layer: `CardCode`, y una línea por cada ítem con `BaseType=22` (Purchase Order), `BaseEntry=doc_entry_oc`, `BaseLine=base_line`, `Quantity=cantidad` — el patrón estándar de SAP para "Copy From" una OC a una Entrada de Mercancía.
3. **`SapServiceLayerClient.vb`** (ya existe, reutilizado tal cual) — `PostAsync` (método nuevo a agregar a esta clase, análogo a `GetAsync`, con la misma sesión/reintento-en-401 ya construidos) contra `PurchaseDeliveryNotes` de Service Layer, con el payload del paso 2b.
4. Si Service Layer confirma la creación (`201`, con el `DocEntry` real del GRPO recién creado en su respuesta) — mismo `daemon_logical`:
   a. `POST /api/v1/entradas-pendientes/<id>/confirmar-borrador/` **y** `POST .../confirmar-definitivo/` al Portal, ambos con el mismo `DocEntry` (flujo de un solo paso, confirmado) — vía el cliente HTTP hacia el Portal (reutilizando el mismo patrón que `sync-oc`).
5. Si algo falla en el paso 3 (Service Layer rechaza la creación) — `POST /api/v1/entradas-pendientes/<id>/reportar-error/` con el mensaje real de SAP, para que quede visible del lado del Portal sin bloquear el resto del ciclo (mismo criterio ya establecido: un fallo no debe interrumpir el procesamiento de las demás `EntradaMercaderia` pendientes de esa corrida — ver el patrón ya usado en `sync-proveedores`, que procesa cada registro de forma independiente).
6. **`Logger.vb`** — cada paso relevante (éxito, error de Service Layer, error de red hacia el Portal) queda registrado, mismo criterio ya aplicado en `SyncService.vb`/`ServiceLayerSmokeTest.vb`.

**Lo que NO cambia de este diseño una vez implementado**: la capa HANA (`SyncSAPB1`) no participa en absoluto en este flujo — la creación del GRPO es 100% vía Service Layer, tal como establece la regla dura de la sección 1.
