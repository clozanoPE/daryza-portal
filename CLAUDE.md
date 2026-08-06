# CLAUDE.md — Daryza Portal (VBS · Vendor Booking System)

Guía de referencia rápida para trabajar en este repositorio. Generado a partir de un análisis completo del código el **2026-07-30**. Ver también `INFORME_ANALISIS.md` para el detalle de inconsistencias y prioridades.

## Reglas de esta sesión en adelante (sesión 26)

- **Las notas de implementación NUNCA van como comentario dentro de un `.html` de template** — ni `{# ... #}`, ni `{% comment %}...{% endcomment %}`, ni ninguna otra forma. Ese contenido vive únicamente en `CLAUDE.md` (historial de sesiones) o en el resumen entregado en el chat. Motivo: el patrón `{# ... #}` escrito en varias líneas ya causó texto de implementación visible en pantalla **dos veces** (sesión 20, reincidencia en la sesión 25) — el tokenizer de Django (`django/template/base.py::tag_re = re.compile(r"({%.*?%}|{{.*?}}|{#.*?#})")`, sin la flag `re.DOTALL`) no reconoce un comentario `{# #}` cuyo contenido cruza un salto de línea: en vez de descartarlo, lo trata como texto literal y lo renderiza tal cual. La única forma segura de anotar código con contexto de sesión sin este riesgo es no ponerlo en el `.html` en absoluto.
- **Antes de cerrar cualquier sesión que haya tocado archivos `.html`**, correr como último chequeo un barrido de `{#` con contenido multilínea en los archivos tocados (no basta con "creo que esta vez lo escribí en una sola línea" — ya fue la causa de la reincidencia). Ver el script de verificación documentado en la sesión 20/26.
- **`CLAUDE.md` se actualiza siempre al cierre de cada sesión** (pedido explícito, sesión 27), incluidas sesiones de solo análisis/investigación sin cambios de código — en esos casos, un resumen breve basta (no hace falta repetir el análisis completo ya entregado en el chat), pero el hecho de que la sesión ocurrió y qué se decidió en ella sí debe quedar registrado aquí. Única excepción: si el propio pedido del usuario dice explícitamente "no actualices CLAUDE.md todavía" para una sesión puntual (p. ej. mientras una decisión de diseño sigue abierta y podría cambiar) — ahí se respeta esa instrucción puntual sobre esta regla general, y se documenta recién cuando el usuario confirme.

## Descripción del negocio

Portal Django para que proveedores agenden citas de entrega a las plantas de Daryza. Flujo funcional:

```
Proveedor solicita cita (con OCs de SAP)
   → Compras/Almacén confirma (genera Ticket + QR)
   → Proveedor carga COA (Certificado de Análisis) por línea de OC
   → Vigilancia escanea QR y autoriza ingreso a planta
   → Almacén recibe físicamente
   → Calidad inspecciona (solo si la OC es Materia Prima / tipo_flujo=CON_CALIDAD)
   → Vigilancia registra salida → Ticket FINALIZADO
```

Áreas/roles (Django Groups, no hay modelo `User` propio): `PROVEEDORES`, `COMPRAS`, `ALMACEN`, `CALIDAD`, `VIGILANCIA`. El enrutamiento por rol ocurre en `apps/base/views.py::redirect_by_role`.

## Stack técnico

- **Backend**: Django 6.0.3, Django REST Framework 3.17
- **DB**: PostgreSQL (`psycopg2-binary`), configurada en `core/settings.py` vía variables de entorno cargadas desde `core/.env`
- **Auth API**: `rest_framework.authtoken` (Token Authentication) — usado por el endpoint de sincronización SAP
- **Estáticos**: WhiteNoise (`CompressedManifestStaticFilesStorage`)
- **CORS**: `django-cors-headers` instalado y con middleware activo (nota: sin `CORS_ALLOWED_ORIGINS` configurado, ver informe)
- **Archivos**: Pillow, integración propia con **OneDrive/Microsoft Graph API** para almacenar COAs (`apps/base/utils.py::OneDriveClient`)
- **Frontend**: Server-rendered con Django Templates + Bootstrap 5 (CDN) + JS vanilla (`static/js/`), AJAX (fetch) contra endpoints propios devolviendo JSON
- **i18n**: `es-pe`, zona horaria `America/Lima`
- **Dependencia adicional no usada**: `django-extensions` está en `requirements.txt` pero no en `INSTALLED_APPS`

Variables de entorno esperadas en `core/.env`: `DEBUG`, `DJANGO_SECRET_KEY`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, `ONEDRIVE_CLIENT_ID`, `ONEDRIVE_TENANT_ID`, `ONEDRIVE_CLIENT_SECRET`, `ONEDRIVE_DRIVE_ID`. `DJANGO_SECRET_KEY` y `DB_PASSWORD` son **obligatorias** desde la sesión 18 (`settings.py` falla al arrancar con `ImproperlyConfigured` si faltan, sin fallback inseguro). Opcional: `ALLOWED_HOSTS` (lista separada por comas; default `localhost,127.0.0.1` si no se define). `SAP_SYNC_TOKEN` (opcional, solo para `test_api.py`, el script manual de humo — ya no tiene el token hardcodeado).

## Estructura de apps

| App | Propósito | Modelos clave |
|---|---|---|
| `apps.base` | Infra transversal: modelo abstracto `TimeStampedModel`, decoradores de permisos por grupo (`apps/base/decorators.py`), router de home por rol, cliente OneDrive | (sin modelos concretos) |
| `apps.sap_sync` | Espejo local de datos maestros de SAP (Órdenes de Compra) | `PurchaseOrder`, `PurchaseOrderLine` |
| `apps.appointments` | Portal del proveedor: slots de horario y solicitud/gestión de citas | `AppointmentSlot`, `Appointment` |
| `apps.operations` | Ciclo de vida operativo del Ticket (Compras/Almacén/Calidad/Vigilancia) | `Ticket`, `TicketStage`, `TicketLineInspection` |
| `apps.scheduling` | Administración de plantillas de horario semanal (genera `AppointmentSlot`) | `ScheduleTemplate`, `WeeklySlotRule` |
| `api` (raíz, fuera de `apps/`) | Endpoint REST de sincronización SAP (`/api/v1/sync-oc/`), consumido por un demonio VB.NET externo | usa modelos de `apps.sap_sync` |
| `services` (raíz) | **Código muerto/duplicado** — no lo uses como referencia, ver `INFORME_ANALISIS.md` | — |

Namespacing de URLs: `appointments:`, `operations:`, `scheduling:` (ver `core/urls.py`).

## Convenciones de código detectadas

- **Lógica de negocio en `services.py` por app** (patrón "fat services, thin views"): las vistas casi no tienen lógica, delegan a clases estáticas (`AppointmentService`, `OperationsService`, `SchedulingService`, `SlotService`).
- **Permisos por grupo, no por permission de Django**: se usan decoradores `@algo_required` de `apps/base/decorators.py` (`proveedor_required`, `almacen_required`, `calidad_required`, `vigilancia_required`, `compras_required`, `staff_interno_required`, `staff_o_proveedor_required`). Los superusuarios siempre pasan (`or user.is_superuser`).
- **Respuestas AJAX**: patrón uniforme `{'status': 'success'|'error', ...}` vía helpers `_json_ok`/`_json_err` (repetidos localmente en `appointments/views.py` y `operations/views.py`, no compartidos).
- **Choices en español, en mayúsculas** (`SOLICITADO`, `CONFIRMADA`, `PROGRAMADO`, `EN_PLANTA`, etc.) definidos como listas de tuplas dentro de cada modelo.
- **Trazabilidad por etapas**: `TicketStage` registra timestamps de inicio/fin por etapa; `TicketLineInspection` registra el detalle por línea de OC y por etapa (`VIGILANCIA`/`ALMACEN`/`CALIDAD`).
- **Nombres de URL**: `panel_<area>` para dashboards, `ajax_<verbo>_<recurso>` para AJAX (convención documentada en `apps/operations/urls.py`).
- Comentarios en español, abundantes docstrings explicando el "por qué" del flujo de negocio — útiles, consérvalos al editar.
- Hay comentarios/código obsoleto dejado in-place con explicaciones tipo "AJUSTE QUIRÚRGICO" o "# POR esto:" — señal de que el código ha sido parcheado varias veces sin limpiar rastros; revisar con cuidado antes de asumir que un comentario describe el estado actual (ver `INFORME_ANALISIS.md`).

## Comandos frecuentes

```powershell
# Entorno virtual (ya existe en ./env)
.\env\Scripts\Activate.ps1

# Migraciones
python manage.py makemigrations
python manage.py migrate
python manage.py showmigrations

# Servidor de desarrollo
python manage.py runserver

# Shell interactivo
python manage.py shell

# Crear superusuario
python manage.py createsuperuser
```

⚠️ La suite de tests automatizada (`tests.py`) sigue acotada a `apps/operations` (5 tests, sesión 5) — el resto del proyecto no tiene pruebas. `test_api.py` (raíz) es un script manual de humo contra el endpoint SAP; desde la sesión 18 ya no tiene el token hardcodeado (lee `SAP_SYNC_TOKEN` de `core/.env` o lo pide por `input()`) — el token viejo que estaba expuesto en texto plano sigue pendiente de rotación manual en el sistema real (fuera del alcance de Claude).

✅ `requirements.txt` está en UTF-8 real desde la sesión 14 (antes UTF-16LE con BOM).

## Historial de sesiones

### 2026-07-30 — Análisis inicial completo (sin modificar código)
- Se recorrió toda la estructura del proyecto (`apps/`, `core/`, `templates/`, `static/`, `services/`, `api/`, migraciones).
- Se verificó estado de migraciones: **al día**, sin cambios pendientes (`makemigrations --check` limpio) y todas aplicadas en la BD Postgres configurada (`showmigrations` sin pendientes).
- Se consultó la BD real: 5 `PurchaseOrder`, 3 `Appointment`, 25 `AppointmentSlot`, 3 `Ticket`, grupos `CALIDAD/VIGILANCIA/ALMACEN/COMPRAS/PROVEEDORES` ya creados, 9 usuarios.
- Se generó `INFORME_ANALISIS.md` con hallazgos clasificados por severidad. Pendiente: decisión del usuario sobre qué corregir primero antes de tocar código.
- Hallazgo más urgente: bug real en `apps/operations/services.py::get_resumen_ticket` (`insp.coa_url.url` sobre un `URLField`) que ya afecta datos existentes (25 inspecciones con `coa_url` no vacío en BD).

### 2026-07-30 (sesión 2) — Diagnóstico del flujo de etapas (Ticket → TicketLineInspection)
- Sin modificar código: se trazó el ciclo de vida completo de `TicketLineInspection` a través de las etapas (Vigilancia/Almacén/Calidad), con verificación contra los 3 `Ticket` reales.
- Se agregó la sección 5 a `INFORME_ANALISIS.md` con los 5 puntos de escritura identificados, la ausencia de un control de "etapa cerrada", el hallazgo de que `detalle_ticket.html` solo muestra `ocs_agrupadas_almacen` (nunca `ocs_agrupadas_calidad`/`_vigilancia`), y evidencia real de que una cuenta de `ALMACEN` registró resultados de `CALIDAD` (sin restricción de rol en el backend).
- Preguntas de seguimiento respondidas (sin tocar código): el COA por línea del proveedor solo vivía en `TicketLineInspection.coa_url` (etapa='ALMACEN'), sin campo propio previo; y no existe ninguna vista pública/semi-pública accesible solo con el `token_qr`.

### 2026-07-30 (sesión 3) — Implementación: modelo `TicketLineCOA` y desacople del COA por línea
Cambios de código realizados (con confirmación previa del usuario, alcance acotado — sin tocar `etapa_actual` ni permisos):

1. **Nuevo modelo `TicketLineCOA`** (`apps/operations/models.py`): guarda el COA por línea de OC de forma independiente a las etapas operativas (`ticket`, `po_line`, `coa_url`, `evidencia_url` opcional, `subido_por`, `fecha_carga`, único por `(ticket, po_line)`). Migración `apps/operations/migrations/0003_ticketlinecoa.py`, generada y aplicada.
2. **`OperationsService.registrar_coa_proveedor`** (`apps/operations/services.py`) reescrito: ahora crea/actualiza `TicketLineCOA` y **ya no toca `TicketLineInspection` en absoluto**.
3. **`AppointmentService.confirmar_cita`** (`apps/appointments/services.py`): se eliminó la creación prematura del esqueleto `TicketLineInspection` (etapa='ALMACEN') al confirmar la cita. El `Ticket` se sigue creando ahí.
4. **`detalle_ticket.html` + su vista** (`apps/operations/views.py`): la tabla "Estado de Certificados (COA)" (revisión pre-ingreso de Vigilancia) ahora lee de `TicketLineCOA` vía el nuevo `OperationsService.get_coa_status_por_oc()`; el cálculo de `coa_completo` también se centralizó ahí (`OperationsService.calcular_coa_completo()`).
5. **`panel_vigilancia`** (`apps/operations/views.py`): ahora anota `ticket.coa_completo` (mismo helper) en cada ticket `PROGRAMADO`, corrigiendo el badge del Kanban que antes siempre mostraba "Faltan COAs" (el atributo no existía).

**Ripple fixes necesarios, no pedidos explícitamente pero requeridos para no romper el flujo al ejecutar los puntos anteriores** (documentados también en el resumen entregado al usuario):
- `OperationsService.iniciar_ingreso_planta` reescrito: ya no depende de que exista el esqueleto `TicketLineInspection` etapa='ALMACEN' (eliminado en el punto 3); ahora lee las líneas directamente de las OCs de la cita y valida el COA contra `TicketLineCOA`. Sin este cambio, Vigilancia no podría autorizar ingreso nunca más (la validación `if not inspecciones_base.exists(): raise ValidationError` fallaría siempre).
- `OperationsService.autorizar_almacen`: se añadió `'doc_num': po.doc_num` a los `defaults` del `get_or_create` de `TicketLineInspection` (etapa='ALMACEN'). Antes era un no-op porque el esqueleto ya existía; al eliminarlo, este `get_or_create` pasa a **crear** la fila por primera vez, y `doc_num` es un campo obligatorio (`NOT NULL`, sin default) — sin este fix habría lanzado un `IntegrityError` en el primer ticket que llegara a este paso.
- `apps/appointments/views.py::api_lineas_cita` reescrito: ya no depende de `ticket.inspections.filter(etapa='ALMACEN')` (que ya no existe justo tras confirmar la cita); ahora lista siempre las líneas desde `appointment.purchase_orders__lines` y cruza el estado de COA contra `TicketLineCOA`. Sin este cambio, el panel de carga de COA del proveedor mostraría 0 líneas justo en la ventana en que el proveedor debe subir el COA.
- Se evitó reproducir el bug ya documentado de asignar una URL (string) al `FileField` `evidencia_url`: ni `registrar_coa_proveedor` ni el `iniciar_ingreso_planta` reescrito lo hacen ya (antes sí, en `iniciar_ingreso_planta` y en la versión anterior de `registrar_coa_proveedor`).

**Validación realizada** (sin dejar datos de prueba permanentes, todo dentro de `transaction.atomic()` con rollback forzado):
- `manage.py check` y `makemigrations --check --dry-run` limpios tras los cambios.
- Ciclo completo `CON_CALIDAD` (confirmar → cargar COA → ingreso → almacén → calidad → salida) ejecutado extremo a extremo sin excepciones sobre datos reales.
- Caso negativo: ingreso bloqueado correctamente cuando falta el COA obligatorio (mensaje "Acceso Denegado...").
- Ciclo completo `SOLO_ALMACEN` (con OC sintética autocontenida) ejecutado extremo a extremo; se confirmó que el comportamiento ya documentado de sobreescritura en el sitio de la fila `etapa='ALMACEN'` en el cierre sigue funcionando igual que antes (no se tocó esa parte, fuera de alcance de esta fase).
- `detalle_ticket` y `panel_vigilancia` renderizan correctamente (HTTP 200) para los 3 tickets reales vía Django test `Client`.

**Fuera de alcance de esta fase (confirmado explícitamente por el usuario, para fases posteriores):** control de "etapa cerrada"/`etapa_actual` en `TicketLineInspection`, y ajuste de permisos (`ajax_registrar_inspeccion` sigue usando `@staff_interno_required`, no restringido por rol de etapa).

### 2026-07-30 (sesión 4) — Máquina de estados `Ticket.etapa_actual` (candado a nivel de servicio)

**Modelo y migraciones:**
- Nuevo campo `Ticket.etapa_actual` (`apps/operations/models.py`), `CharField` con choices y constantes de clase (`Ticket.ETAPA_PENDIENTE_INGRESO`, `.ETAPA_VIGILANCIA_INGRESO`, `.ETAPA_ALMACEN`, `.ETAPA_CALIDAD`, `.ETAPA_VIGILANCIA_SALIDA`, `.ETAPA_FINALIZADO`), `default=ETAPA_PENDIENTE_INGRESO` (así todo `Ticket` nuevo arranca ahí sin tocar `confirmar_cita`).
- Migración de esquema: `apps/operations/migrations/0004_ticket_etapa_actual.py` (AddField).
- Migración de datos: `apps/operations/migrations/0005_inicializar_etapa_actual.py` (`RunPython`), reconstruye `etapa_actual` de los tickets existentes según `estado` + `TicketStage` ya registradas (no quedó ninguno en el valor por defecto si ya estaba en curso). Aplicada y verificada contra los 3 tickets reales: Ticket 1 (`EN_PLANTA`/`CON_CALIDAD`, con `CALIDAD_INSPECCION` abierta) → `CALIDAD`; Ticket 2 (`FINALIZADO`) → `FINALIZADO`; Ticket 3 (`EN_PLANTA`/`SOLO_ALMACEN`, con `ALMACEN_RECEPCION` abierta) → `ALMACEN`.

**Semántica elegida** (cada valor = última acción operativa completada, no "quién debe actuar"):

```
CON_CALIDAD:  PENDIENTE_INGRESO → VIGILANCIA_INGRESO → ALMACEN → CALIDAD → VIGILANCIA_SALIDA → FINALIZADO
SOLO_ALMACEN: PENDIENTE_INGRESO → VIGILANCIA_INGRESO → ALMACEN → VIGILANCIA_SALIDA → FINALIZADO
              (CALIDAD se omite: registrar_calidad salta directo a VIGILANCIA_SALIDA)
```

| Método (`OperationsService`) | Precondición (`etapa_actual` requerida) | Postcondición (nueva `etapa_actual`) |
|---|---|---|
| `iniciar_ingreso_planta` | `PENDIENTE_INGRESO` | `VIGILANCIA_INGRESO` |
| `autorizar_almacen` | `VIGILANCIA_INGRESO` | `ALMACEN` |
| `registrar_calidad` | `ALMACEN` (ambos flujos) | `CALIDAD` si `tipo_flujo=CON_CALIDAD`, si no `VIGILANCIA_SALIDA` (salta Calidad) |
| `registrar_salida` | `CALIDAD` si `CON_CALIDAD`, si no `VIGILANCIA_SALIDA` | `FINALIZADO` |

`registrar_coa_proveedor` (carga de COA) **no** tiene candado ni avanza `etapa_actual` — es independiente del flujo operativo por diseño (ver sesión 3).

**Implementación:**
- Nueva excepción `TicketEtapaError(ValidationError)` en `apps/operations/services.py` — hereda de `ValidationError` a propósito para que los endpoints AJAX existentes (que ya capturan `ValidationError`) sigan funcionando sin tocarlos (Fase 3 pendiente = ajustar permisos, no esta fase).
- Helper `OperationsService._validar_etapa(ticket, esperada)`: compara `ticket.etapa_actual`, lanza `TicketEtapaError` con mensaje legible (etapa actual vs. esperada) si no corresponde. Se llama al inicio de cada método, antes de cualquier `save()`.
- Cada método guarda `etapa_actual` junto con sus demás campos (mismo `save()`/`update_fields`, dentro del `@transaction.atomic` ya existente) — si algo falla a mitad del método, la transacción completa (incluida `etapa_actual`) se revierte.

**Validación realizada** (dentro de `transaction.atomic()` con rollback forzado, sin dejar datos permanentes):
- Ciclo `CON_CALIDAD` completo en orden correcto: `etapa_actual` avanza exactamente como en la tabla de arriba, verificado con `assert` en cada paso.
- Ciclo `SOLO_ALMACEN` completo: confirmado el salto `ALMACEN → VIGILANCIA_SALIDA` (sin pasar por `CALIDAD`).
- Intentos fuera de orden bloqueados correctamente con `TicketEtapaError` y mensaje claro: `registrar_calidad`/`registrar_salida` llamados antes de tiempo (ticket ya `EN_PLANTA` pero en la etapa incorrecta — el candado nuevo es justo lo que detecta esto, ya que el filtro `estado='EN_PLANTA'` preexistente no distingue entre estos tres pasos); reintento de `autorizar_almacen` después de ya haber avanzado a `ALMACEN`, también bloqueado.
- `manage.py check` y `makemigrations --check --dry-run` limpios; `detalle_ticket` y `panel_vigilancia` siguen respondiendo 200 OK para los 3 tickets reales.

**Fuera de alcance de esta fase (Fase 3, pendiente, confirmado por el usuario):** no se tocó el endpoint AJAX `ajax_registrar_inspeccion` ni ningún decorador de permisos — sigue siendo posible, a nivel de autenticación, que una cuenta de `ALMACEN` ejecute el paso de Calidad (ver hallazgo de la sesión 2); el candado de esta sesión solo garantiza el *orden* de las etapas, no *quién* las ejecuta.

### 2026-07-30 (sesión 5) — Fase 3: permiso por grupo según la etapa activa

**Diagnóstico previo (confirmado, no repetido aquí):** `ajax_registrar_inspeccion` (`apps/operations/views.py`) es el único endpoint de escritura sobre `TicketLineInspection`/ciclo del ticket que dependía de un decorador *genérico* de grupo interno (`@staff_interno_required`, permite ALMACEN/CALIDAD/VIGILANCIA/COMPRAS indistintamente). Revisé el resto de endpoints de escritura del ciclo (`ajax_autorizar_almacen`, `ajax_autorizar_ingreso`, `ajax_registrar_salida`, los de `panel_compras`) y todos ya usan un decorador de rol específico y único (`@almacen_required`, `@vigilancia_required`, `@compras_required`) — no había otro caso que corrigir. `ajax_get_ticket_json` también usa `@staff_interno_required`, pero es de solo lectura (no escribe `TicketLineInspection`), así que quedó fuera de alcance a propósito.

**Implementación:**
- Nuevo método `OperationsService.grupo_requerido_por_etapa(ticket)` (`apps/operations/services.py`): dado `ticket.etapa_actual` + `tipo_flujo`, devuelve el grupo (`VIGILANCIA`/`ALMACEN`/`CALIDAD`) al que le corresponde ejecutar la siguiente acción. Es la contraparte "de rol" del candado de orden (`_validar_etapa`) de la sesión 4 — ese valida la secuencia, este valida quién la ejecuta.
- `ajax_registrar_inspeccion` (`apps/operations/views.py`): conserva `@staff_interno_required` como primer filtro (login + algún grupo interno), y ahora además, dentro de la vista, obtiene el `Ticket`, calcula `grupo_requerido_por_etapa(ticket)` y exige que `request.user` pertenezca a ese grupo específico (o sea superusuario). Si no corresponde, responde `403` con un mensaje claro, sin llegar a invocar `OperationsService.registrar_calidad` (no se escribe nada). El frontend (`static/js/modules/api.js::postJSON`) ya maneja cualquier respuesta no-2xx mostrando `data.msg`, así que no fue necesario tocar JS/templates.

**Test nuevo:** `apps/operations/tests.py` (no existía ninguna suite de tests en el proyecto; este es el primer archivo). `RegistrarInspeccionPermisoPorEtapaTests` cubre:
- Un usuario `ALMACEN` **no puede** registrar la inspección de un ticket `CON_CALIDAD` en etapa `ALMACEN` (le corresponde a `CALIDAD`) → `403`, `etapa_actual` no avanza.
- Un usuario `CALIDAD` **no puede** registrar el cierre de un ticket `SOLO_ALMACEN` en etapa `ALMACEN` (le corresponde a `ALMACEN`) → `403`, `etapa_actual` no avanza.
- Caminos positivos (mismos escenarios, con el grupo correcto) → `200`, `etapa_actual` sí avanza — para asegurar que el test detecta tanto un bloqueo roto como un endpoint roto por completo.
- Superusuario siempre puede, sin importar el grupo.
- `manage.py test apps.operations`: **5/5 tests OK** (usa su propia BD de pruebas, `test_daryza_portal_db`, creada y destruida automáticamente; no toca la BD real).

**Validación adicional:** `manage.py check` y `makemigrations --check --dry-run` limpios; `detalle_ticket` y `panel_vigilancia` siguen respondiendo 200 OK para los 3 tickets reales tras el cambio.

**Fuera de alcance de esta fase (según lo pedido):** no se generalizó `grupo_requerido_por_etapa` a los endpoints que ya tenían decorador específico (no era necesario); no se tocaron plantillas ni JS.

### 2026-07-31 (sesión 6) — "Mi Sesión" vs "Estado Actual" en `detalle_ticket`: corrección del bug de visibilidad de Calidad

**Diagnóstico previo (confirmado, sesión 2):** `detalle_ticket.html` solo leía `ocs_agrupadas_almacen` (`OperationsService.get_grouped_by_oc(pk, etapa='ALMACEN')`), nunca `ocs_agrupadas_calidad`/`_vigilancia` (esas variables de contexto existían pero no se usaban en la plantilla). Como `registrar_calidad` escribe el resultado real de Calidad en una fila **aparte** (`etapa='CALIDAD'`, ver sesión 3), un `RECHAZADO` o una cantidad ajustada por Calidad quedaba guardado en BD pero la UI seguía mostrando el registro base de Almacén (`PENDIENTE`, cantidad SAP sin ajustar). Verificado contra el Ticket #1 real: línea `8040` tiene `ALMACEN: PENDIENTE/6.0000` vs. `CALIDAD: RECHAZADO/0.0000` — la UI anterior solo mostraba la primera.

**Implementación:**
- **`OperationsService.get_mi_sesion(ticket)`** (`apps/operations/services.py`): filas editables de la etapa activa. Solo `Ticket.etapa_actual == ETAPA_ALMACEN` tiene un formulario editable por línea (alimenta `ajax_registrar_inspeccion`/`registrar_calidad`, que siempre lee/escribe sobre `etapa='ALMACEN'` sin importar el flujo); el resto de etapas activas son acciones de un clic sin edición por línea, así que devuelve `{}`. Reutiliza `get_grouped_by_oc(ticket.id, etapa='ALMACEN')` sin duplicar lógica.
- **`OperationsService.get_estado_actual_por_oc(ticket_id)`** (`apps/operations/services.py`): agrupa por OC la fila **más reciente** (`fecha_registro`) de `TicketLineInspection` por línea, entre **todas** las etapas ya ejecutadas (VIGILANCIA/ALMACEN/CALIDAD), no solo ALMACEN. Siempre de solo lectura — esta es la corrección del bug de visibilidad.
- **Gate de edición en `detalle_ticket`** (`apps/operations/views.py`): se reemplazó la condición anterior (`es_calidad`/`es_almacen` + `es_flujo_calidad` exigido en ambas ramas + `etapas_completadas` vía `TicketStage`) por `puede_editar_mi_sesion = ticket.estado == 'EN_PLANTA' and ticket.etapa_actual == Ticket.ETAPA_ALMACEN and es_su_turno`, donde `es_su_turno` reutiliza `OperationsService.grupo_requerido_por_etapa(ticket)` (la misma función que ya usa `ajax_registrar_inspeccion` desde la sesión 5) comparado contra los grupos del usuario, con bypass de superusuario. `puede_registrar_calidad`/`puede_registrar_almacen` ahora se derivan de este único gate. El contexto pasa `mi_sesion_agrupada` (vacío si el gate no pasa — ni siquiera se envía al template) y `estado_actual_agrupada` (siempre, de solo lectura).
- **Bug lateral encontrado y corregido como parte de este cambio:** la condición anterior exigía `es_flujo_calidad` (`tipo_flujo == 'CON_CALIDAD'`) tanto para `puede_registrar_calidad` **como** para `puede_registrar_almacen` — es decir, para tickets `SOLO_ALMACEN` el formulario de cierre de Almacén (`ajax_registrar_inspeccion` con `etapa_linea='ALMACEN'`) **nunca se renderizaba en la UI**, aunque el backend sí lo permitía. Confirmado con el Ticket #3 real (`SOLO_ALMACEN`, `etapa_actual=ALMACEN`): con el gate nuevo, el botón "Guardar Inspección Almacén" aparece correctamente para un usuario `ALMACEN`.
- **Plantilla nueva reutilizable:** `apps/operations/templates/operations/_partials/tabla_inspeccion.html` — misma estructura de tabla para ambas vistas, parametrizada por `editable` (inputs/select vs. texto/badges, y presencia de `data-inspeccion-id`) y `mostrar_etapa` (columna extra "Etapa", solo en "Estado Actual", que mezcla filas de distintas etapas).
- **`detalle_ticket.html`**: si `acciones.puede_editar_mi_sesion` es verdadero, renderiza pestañas Bootstrap ("Mi Sesión" / "Estado Actual"); si no, renderiza solo "Estado Actual" (sin pestañas, sin formularios) — accesible para cualquier rol interno (incluido Compras) y para el proveedor dueño de la cita, igual que antes.
- **`static/js/panels/ticket_actions.js`**: `guardarInspeccion` ahora limita `document.querySelectorAll('[data-inspeccion-id]')` al contenedor `#mi-sesion-panel` (antes buscaba en todo el documento) — evita que una futura fila de "Estado Actual" contamine el payload enviado a `ajax_registrar_inspeccion`. `static/js/operations/ticket_actions.js` sigue siendo el duplicado muerto ya documentado (sin referencias); no se tocó.

**Validación realizada** (contra los 3 tickets reales, `Client` de pruebas, sin escrituras — la vista es de solo lectura):
- `manage.py check` y `makemigrations --check --dry-run` limpios (no hay cambios de modelo).
- `manage.py test apps.operations`: **5/5 OK** (sin cambios respecto a la sesión 5; `ajax_registrar_inspeccion` no se tocó).
- `detalle_ticket` responde `200` para los 3 tickets reales con superusuario y con un usuario de cada grupo interno (`ALMACEN`, `CALIDAD`, `VIGILANCIA`, `COMPRAS`).
- Ticket #1 (`CON_CALIDAD`, `etapa_actual=CALIDAD`): `get_estado_actual_por_oc` devuelve la fila `CALIDAD` (p.ej. línea `8040`: `RECHAZADO`/`0.0000`) en vez de la fila `ALMACEN` (`PENDIENTE`/`6.0000`) — bug de visibilidad confirmado corregido. `get_mi_sesion` devuelve `{}` (correcto: la etapa activa es `CALIDAD`, no `ALMACEN`).
- Ticket #3 (`SOLO_ALMACEN`, `etapa_actual=ALMACEN`): un usuario `ALMACEN` ve la pestaña "Mi Sesión" y el botón "Guardar Inspección Almacén" (antes, nunca aparecía); un usuario `CALIDAD` sobre el mismo ticket **no** ve la pestaña "Mi Sesión" ni ningún input editable (`insp-cantidad`, `data-inspeccion-id` ausentes), solo la tabla de solo lectura.
- Usuarios `CALIDAD` y `COMPRAS` ven "Detalle de OC — Inspección" con los datos de las líneas (`oc-group-card`) pero sin ningún elemento de formulario — confirma el punto 2 (Compras y roles de consulta acceden de solo lectura).

**Fuera de alcance de esta fase (no pedido, no tocado):** no se agregaron tests nuevos (no se solicitaron en este pedido); no se tocó `ajax_registrar_inspeccion` ni el candado de `Ticket.etapa_actual` (siguen igual que en la sesión 5); no se tocó `ticket_detalle.html` (plantilla sin ruta activa, ya detectada como código muerto) ni `static/js/operations/ticket_actions.js` (duplicado muerto).

### 2026-07-31 (sesión 7) — Fase 5 (cierre): vista de trazabilidad de solo lectura + enrutamiento de `scan_qr` por rol

Con esta sesión se cierra el plan de 5 fases sobre el ciclo `Ticket → TicketLineInspection` iniciado en el diagnóstico de la sesión 2 (`INFORME_ANALISIS.md` §5): Fase 1-2 (`TicketLineCOA` y desacople del COA, sesión 3), Fase 2b (candado de orden `etapa_actual`, sesión 4), Fase 3 (permiso por grupo según etapa activa, sesión 5), Fase 4 (`mi_sesion` / `estado_actual` + corrección del bug de visibilidad de Calidad, sesión 6), Fase 5 (esta sesión: vista de trazabilidad + `scan_qr` consciente del rol).

**Implementación:**
- **`OperationsService.grupo_requerido_por_etapa(ticket)`** (sesión 5, sin cambios) se reutiliza ahora como la única fuente de verdad para "¿tiene este usuario una acción pendiente sobre este ticket?", ya usada por el gate de escritura (`ajax_registrar_inspeccion`) y por el gate de edición de `detalle_ticket` (sesión 6). Esta sesión la extiende a un tercer consumidor: el enrutamiento de `scan_qr`.
- **Vista nueva `OperationsService`-consciente `trazabilidad_ticket`** (`apps/operations/views.py`), protegida con `@staff_o_proveedor_required` (mismo decorador que `detalle_ticket`) y con la misma verificación de propiedad para proveedores (`ticket.appointment.user != request.user` → redirect a `portal_proveedor`). Contexto: `ticket` (para el stepper de `TicketStage`) + `estado_actual_agrupada` = `OperationsService.get_estado_actual_por_oc(pk)` (el mismo método de la Fase 4, sin acciones ni formularios).
- **Ruta nueva**: `operations:trazabilidad_ticket` → `ticket/<int:pk>/trazabilidad/` (`apps/operations/urls.py`, junto a `detalle_ticket`).
- **Plantilla nueva** `apps/operations/templates/operations/trazabilidad_ticket.html`: cabecera + "Datos de la Cita" + stepper de etapas + tabla de "Estado Actual" (solo lectura, `mostrar_etapa=True`), sin ningún botón de acción ni `<script>` de `ticket_actions.js`.
- **`scan_qr`** (`apps/operations/views.py`) reescrito: decorador cambiado de `@vigilancia_required` a `@staff_o_proveedor_required` (antes solo Vigilancia podía usarlo; ahora cualquier rol interno o el proveedor pueden escanear/abrir un QR). Calcula `grupo_pendiente = OperationsService.grupo_requerido_por_etapa(ticket)` (solo si `ticket.estado` es `PROGRAMADO` o `EN_PLANTA`; `None` en cualquier otro caso, p.ej. `FINALIZADO`) y redirige a `detalle_ticket` si el grupo del usuario coincide con `grupo_pendiente` (o es superusuario), o a `trazabilidad_ticket` en cualquier otro caso — lo que cubre automáticamente al proveedor (nunca pertenece a un grupo operativo) sin necesitar un caso especial. La verificación de propiedad para proveedores no se duplica en `scan_qr`: queda delegada a las vistas de destino. Los redirects de error (código vacío / QR no encontrado) ahora van a `portal_proveedor` si el usuario es proveedor, o a `panel_vigilancia` en cualquier otro caso (antes siempre iban a `panel_vigilancia`, lo que habría producido un rebote a login para un proveedor con un código inválido, dado que `panel_vigilancia` sigue siendo `@vigilancia_required`).
- **Higiene de plantillas (sin cambio de comportamiento):** se extrajeron a partials compartidos dos bloques que iban a duplicarse entre `detalle_ticket.html` y la nueva `trazabilidad_ticket.html`: `_partials/stages_stepper.html` (el stepper de `TicketStage`) y `_partials/inspeccion_styles.html` (el `<style>` de `.oc-group-card`/`.badge-insp`/`.transition-icon` que antes vivía inline al final de `detalle_ticket.html`, dentro de `{% block extra_js %}`; ahora se incluye vía `{% block extra_head %}` en ambas plantillas). Mismo principio ya aplicado a `_partials/tabla_inspeccion.html` en la sesión 6.

**Validación realizada** (contra los 3 tickets reales, `Client` de pruebas, sin escrituras — ambas vistas son de solo lectura):
- `manage.py check` y `makemigrations --check --dry-run` limpios (no hay cambios de modelo).
- `manage.py test apps.operations`: **5/5 OK** (sin cambios respecto a la sesión 6).
- `trazabilidad_ticket` responde `200` para los 3 tickets reales con superusuario y con un usuario de cada grupo interno.
- Proveedor dueño del Ticket #1 ve su propia trazabilidad (`200`); un proveedor distinto es redirigido a `portal_proveedor` (`302` → `200` tras seguir el redirect).
- Contenido verificado en la página de trazabilidad del Ticket #1: aparece el resultado real de Calidad (`RECHAZADO`, clase `badge-insp--rechazado`) y **no** aparece ningún elemento de formulario (`insp-cantidad`, `data-inspeccion-id`) ni botón de acción (`accionTicket(`, `guardarInspeccion(`).
- `scan_qr` verificado con los 3 tickets reales y los 4 grupos + superusuario + proveedor dueño:
  - Ticket #1 (`CON_CALIDAD`, `etapa_actual=CALIDAD`, turno de `VIGILANCIA`): superusuario y `VIGILANCIA` → `detalle_ticket`; `ALMACEN`, `CALIDAD`, `COMPRAS` y el proveedor dueño → `trazabilidad_ticket`.
  - Ticket #2 (`FINALIZADO`, sin turno pendiente): los 4 grupos, superusuario y proveedor → `trazabilidad_ticket` para todos.
  - Ticket #3 (`SOLO_ALMACEN`, `etapa_actual=ALMACEN`, turno de `ALMACEN`): superusuario y `ALMACEN` → `detalle_ticket`; `CALIDAD`, `VIGILANCIA`, `COMPRAS` y el proveedor dueño → `trazabilidad_ticket`.

**Fuera de alcance de esta fase (no pedido, no tocado):** no se agregó ningún enlace cruzado entre `detalle_ticket` y `trazabilidad_ticket` en las plantillas (p.ej. un botón "Ver trazabilidad completa" desde la ficha operativa); no se tocó `panel_vigilancia.html` (el formulario de búsqueda por QR sigue apuntando al mismo `operations:scan_qr`, ahora con el enrutamiento nuevo, pero la UI del panel no cambió); no se añadieron tests nuevos (no se solicitaron); no se tocó el manejo de `ticket.estado == 'CANCELADO'` en `scan_qr` más allá de tratarlo como "sin turno pendiente" (mismo criterio que `FINALIZADO`).

**Cierre del plan de 5 fases:** el ciclo completo `Ticket ↔ TicketLineInspection` ahora tiene: (1) COA por línea desacoplado de las etapas operativas, (2) un candado de orden que impide saltarse etapas, (3) un candado de rol que impide que un grupo ejecute la etapa de otro, (4) una UI que distingue "lo que yo puedo editar ahora" de "el estado real más reciente de cada línea" (con el bug de visibilidad de Calidad corregido), y (5) una vista de consulta pura para quien no tiene turno, con el QR como punto de entrada único que enruta automáticamente según a quién le toca actuar.

### 2026-07-31 (sesión 7b) — Datos de prueba: usuarios y `AppointmentSlot` futuros para pruebas manuales

Sin cambios de código. A pedido del usuario, para habilitar pruebas manuales end-to-end del flujo completo (horarios → proveedor solicita → Compras confirma → COA → Vigilancia → Almacén → Calidad → Vigilancia salida):

- **Contraseña de prueba `Prueba2026!`** asignada (vía `set_password`, sin crear cuentas nuevas) a las 5 cuentas ya existentes que mapean 1:1 con los grupos operativos: `ucompras` (COMPRAS), `ualmacen` (ALMACEN), `ucalidad` (CALIDAD), `uvigilancia` (VIGILANCIA), y `P20100055237` (PROVEEDORES — ALICORP S.A.A., la única cuenta proveedor con una OC real todavía sin cita: `doc_num 16089835`, tipo Materia Prima). No se tocó `clozano` (superusuario) ni `daemon_user` (token del daemon SAP).
- **24 `AppointmentSlot` futuros** creados (2026-08-03 a 2026-08-14, lunes a viernes, dos semanas), replicando exactamente el patrón horario/muelle/capacidad de los 25 slots históricos (capacidad 2, `end_time = start_time + 45min`, mismos muelles por día de la semana). Los 25 slots originales ya habían vencido (ninguno con fecha ≥ hoy) y no existe ninguna `ScheduleTemplate`/`WeeklySlotRule` en el sistema (tampoco hay UI ni admin para crearlas), así que se generaron directamente por ORM en vez de vía `SchedulingService.generar_semana`.
- Confirmado en la sesión siguiente (8): el usuario ya usó este setup para generar el primer ticket real de prueba end-to-end (`Ticket #11`, `PROGRAMADO`, proveedor `P20100055237`, slot `2026-08-04`).

### 2026-08-01 (sesión 8) — Fase 6 (aditiva): historial de Tickets en Panel Compras

**Contexto:** `panel_compras` (`apps/operations/views.py`) solo listaba citas `SOLICITADO` pendientes de revisión. En cuanto Compras confirmaba una cita y se generaba el `Ticket`, este desaparecía de la vista de Compras sin ningún punto de entrada para volver a consultarlo — `trazabilidad_ticket` (Fase 5) ya existía y ya era accesible para COMPRAS (`@staff_o_proveedor_required` incluye el grupo COMPRAS sin restricción adicional por ticket), pero no había ningún enlace hacia ella desde el panel de Compras.

**Alcance explícitamente aditivo** (confirmado con el usuario antes de empezar): no se tocó `Ticket.etapa_actual`, el candado de `_validar_etapa`, los permisos de `ajax_registrar_inspeccion`/`grupo_requerido_por_etapa`, ni el modelo `TicketLineCOA`.

**Implementación:**
- **`panel_compras`** (`apps/operations/views.py`) ahora arma un segundo queryset, `tickets_qs` (`Ticket.objects...order_by('-fecha_creacion')`), con los **mismos filtros `q`/`fecha`** ya existentes para `solicitudes` pero aplicados sobre `Ticket`/`Appointment` (`id`, `appointment__purchase_orders__doc_num`, `appointment__user__username`, `appointment__slot__date`) — **sin** filtrar por quién confirmó la cita, es decir, devuelve todos los tickets del sistema. Se pasa al contexto como `tickets_historial`.
- **`panel_compras.html`**: se envolvió el contenido existente en pestañas Bootstrap (`nav-tabs` + `tab-content`), reutilizando el mismo patrón ya establecido en `detalle_ticket.html` (Fase 4/sesión 6: "Mi Sesión" / "Estado Actual") — así no se introduce un tercer estilo de UI en la app. Pestaña **"Pendientes"**: el listado de tarjetas que ya existía, sin cambios de contenido. Pestaña nueva **"Historial"**: una tabla (`table table-hover`, mismo patrón que `_partials/tabla_inspeccion.html`) con columnas Ticket, OC(s) (mismo badge ya usado para OCs en este panel), Proveedor, Fecha de cita, Estado (mismos badges de color que `detalle_ticket.html`/`trazabilidad_ticket.html`: `PROGRAMADO`=azul, `EN_PLANTA`=amarillo, `FINALIZADO`=verde, `CANCELADO`=gris), Etapa activa (`ticket.get_etapa_actual_display` — ya incluye el label `'Finalizado'` para `ETAPA_FINALIZADO`; se agregó un único caso especial para `estado == 'CANCELADO'` → texto "Cancelado", ya que `etapa_actual` no tiene un valor propio para ese caso) y un botón **"Ver"** que enlaza a `{% url 'operations:trazabilidad_ticket' pk=ticket.id %}` — la misma vista de solo lectura de la Fase 5, sin duplicar su plantilla ni su lógica. Cada pestaña muestra su conteo en un badge (mismo patrón de badges-en-tab-title ya usado en Fase 4).
- Se agregó una nota de una línea bajo el formulario de filtros aclarando que aplican a ambas pestañas.

**Punto 5 (verificación de permisos, no modificación):** se confirmó que `trazabilidad_ticket` ya permite a COMPRAS ver **cualquier** ticket del sistema, no solo los que confirmó — `@staff_o_proveedor_required` (`apps/base/decorators.py::es_staff_o_proveedor`) incluye `COMPRAS` en la lista de grupos sin ninguna restricción adicional por ticket dentro de la vista (la única comprobación extra dentro de `trazabilidad_ticket` es la de propiedad para `PROVEEDORES`, que no aplica a COMPRAS). **No se encontró ninguna restricción que bloqueara esto**, por lo tanto no se tocó nada de permisos — se verificó empíricamente contra los 4 tickets reales.

**Validación realizada** (contra la BD real, sin escrituras — la vista es de solo lectura, `Client` de pruebas):
- `manage.py check` y `makemigrations --check --dry-run` limpios (no hay cambios de modelo).
- `manage.py test apps.operations`: **5/5 OK** (sin cambios respecto a la sesión 7).
- `panel_compras` responde `200` para `ucompras`; los 4 tickets reales (`#1`, `#2`, `#3`, y el `#11` nuevo de la sesión de pruebas manuales) aparecen en la pestaña "Historial", cada uno con su link a `/operations/ticket/<id>/trazabilidad/`.
- Filtro `?q=<username de un proveedor>` aplicado sobre `/operations/compras/`: el ticket de ese proveedor aparece, los de los demás proveedores no — confirma que el filtro ya existente ahora también filtra el historial.
- Las 4 etiquetas de "Etapa activa" (`Inspección de Calidad`, `Finalizado`, `Recepción en Almacén`, `Pendiente de Ingreso (Vigilancia)`) aparecen correctamente en el HTML renderizado, una por cada ticket real con su `etapa_actual` real.
- `ucompras` obtiene `200` en `trazabilidad_ticket` para los 4 tickets reales, incluidos los que no confirmó él mismo — confirma el punto 5.

**Fuera de alcance de esta fase (explícitamente no pedido, no tocado):** Almacén, Calidad y Vigilancia no reciben ningún historial equivalente todavía (pendiente de decisión posterior, según lo indicado); no se tocó `etapa_actual`, `_validar_etapa`, `grupo_requerido_por_etapa` ni `TicketLineCOA`; no se agregó paginación al historial (con 4 tickets reales no hace falta todavía); no se agregaron tests nuevos (no se solicitaron).

### 2026-08-01 (sesión 9) — 4 correcciones puntuales (bugs de UI/datos, sin tocar etapas/permisos/modelos)

Alcance explícitamente acotado por el usuario: sin tocar `etapa_actual`, permisos de `ajax_registrar_inspeccion`/`grupo_requerido_por_etapa`, ni ningún modelo. Los 4 puntos se investigaron primero (lectura de código + reproducción empírica contra la BD real) antes de tocar nada.

**1. Bug de COA vacío en la vista del proveedor — causa raíz encontrada y corregida.**
`OperationsService.get_estado_actual_por_oc` (Fase 4, sesión 6) toma la fila **más reciente** de `TicketLineInspection` por línea entre todas las etapas, pero leía `coa_url` de esa misma tabla — un campo que es solo una **foto tomada una única vez**, en `iniciar_ingreso_planta` (etapa VIGILANCIA), y que nunca se sincroniza en las filas posteriores de ALMACEN/CALIDAD (quedan en `None`/`''`, ver `autorizar_almacen` y `registrar_calidad` en `apps/operations/services.py`). Como la función toma la fila más reciente, en cuanto Calidad actúa esa fila casi siempre tiene el COA vacío aunque el proveedor sí lo haya cargado. **Reproducido con datos reales**: `Ticket #11`, línea `MP00000041` — `TicketLineCOA` (fuente correcta) tenía el link real de SharePoint, pero la fila `CALIDAD` de `TicketLineInspection` (la más reciente) tenía `coa_url=''`.
- **Fix**: `get_estado_actual_por_oc` ahora cruza `coas_cargados = {po_line_id: coa_url para TicketLineCOA.objects.filter(ticket_id=ticket_id)}` y usa ese dict para `coa_url`, en vez del campo de `TicketLineInspection`. Misma fuente que ya usan correctamente `get_coa_status_por_oc` y `api_lineas_cita`.
- Esto corrige automáticamente tanto `trazabilidad_ticket.html` como `detalle_ticket.html` (ambos consumen `get_estado_actual_por_oc`), sin tocar plantillas.
- **No se tocó** `OperationsService.get_grouped_by_oc` (usado por `mi_sesion`, la edición interna de Almacén/Calidad) — tiene un mecanismo de fallback distinto (busca la fila `VIGILANCIA` si la propia está vacía) que no fue reportado como roto y es una vista interna de staff, no del proveedor; fuera del alcance pedido ("vista del proveedor").
- **Historial de Entregas (columna "Documentos")**: `HistorialCitasView` (`apps/appointments/views.py`) leía `cita.coa_pdf`, el campo legacy a nivel de `Appointment` completo que la carga de COA por línea (Fase 3, vía OneDrive) nunca llena. Se agregó `get_context_data` que anota `cita.coa_completo` con `OperationsService.calcular_coa_completo(cita.ticket)` (Fase 4, ya existente — True/False/None si la cita aún no tiene Ticket). El template (`templates/appointments/historial_listado.html`) ahora muestra un ícono de estado (✓ completo / ⚠ pendiente / — no aplica) en vez del enlace directo al PDF legacy (que ya no representa la realidad, al ser el COA por línea y no un único PDF).
- Verificado con datos reales: `get_estado_actual_por_oc(11)` devuelve el link real de SharePoint para `MP00000041`; `trazabilidad_ticket` renderizado para el proveedor dueño del Ticket #11 muestra el badge "Cargado" con el link correcto.

**2. Menú de tres puntos del Historial de Entregas — confirmado sin funcionalidad, eliminado.**
`templates/appointments/historial_listado.html`: el ítem `<a href="#">Ver Detalle</a>` (enlace muerto, sin `href` real) fue eliminado. Se mantienen `Ver Ticket y QR` (funcional, va a `operations:detalle_ticket`) e `Imprimir Cargo` (pendiente de Fase 12, se deja tal cual, con su `href="#"` de placeholder). Efecto colateral corregido: el `<hr class="dropdown-divider">` que separaba "Ver Detalle"/"Ver Ticket y QR" de "Imprimir Cargo" quedaba como primer ítem del menú (huérfano, sin nada arriba) para citas no `CONFIRMADA`; se movió el divisor dentro del mismo `{% if cita.status == 'CONFIRMADA' and cita.token_qr %}` para que solo aparezca junto con "Ver Ticket y QR".

**3. Botón "Volver" en Administración de Horarios — agregado.**
`apps/scheduling/templates/scheduling/panel_horarios.html` no tenía ningún botón de retorno. Se agregó `<a href="{% url 'operations:panel_compras' %}" class="btn btn-light btn-sm rounded-3 border"><i class="bi bi-arrow-left me-1"></i>Volver</a>` como primer botón del grupo de acciones del header, mismo patrón visual (clases, ícono, texto) que ya usan `detalle_ticket.html` y `trazabilidad_ticket.html` — con la diferencia de que aquí apunta a `panel_compras` (pedido explícito), no a `home_router`, ya que este panel solo lo usa Compras (y superusuario).

**4. Bug de redirect post-login para COMPRAS — investigado a fondo, NO reproducido, NO se tocó código.**
Se revisó `apps/base/views.py::redirect_by_role`, `core/settings.py` (`LOGIN_REDIRECT_URL='home_router'`, `LOGIN_URL='login'`), `core/urls.py` (el `path('login/', ...)` usa el `LoginView` estándar de Django sin override), y `templates/login.html` (formulario plano, sin JS ni AJAX que intercepte el submit). El bloque `elif user.groups.filter(name='COMPRAS').exists(): return redirect('operations:panel_compras')` es **estructuralmente idéntico** a los de ALMACEN/CALIDAD/VIGILANCIA (mismo patrón, sin typo en el nombre del grupo, sin `return` faltante).

Se reprodujo el flujo completo con el `Client` de pruebas de Django simulando un login real (`POST /login/` con usuario `ucompras`, grupo `COMPRAS` únicamente): la cadena de redirects completa exitosamente — `/login/` → `302` → `/home/` → `302` → `/operations/compras/` → `200`. **No se pudo reproducir el bug descrito** ("la pantalla de login queda visible").

Hipótesis más probable, no verificable desde el código (requiere revisar la cuenta real usada en la prueba manual, no `ucompras`): si esa cuenta **no está realmente asignada al grupo `COMPRAS`** en Django (aunque el usuario la identifique como "de Compras"), `redirect_by_role` no entra en ningún `elif` y cae al fallback final `return redirect('/admin/')` — y como esa cuenta no es `is_staff`, Django Admin la vuelve a mandar a **su propia pantalla de login** (`/admin/login/?next=/admin/`, visualmente distinta de `/login/` pero también "una pantalla de login"), sin ningún camino de vuelta a `panel_compras`. Esto coincidiría exactamente con el síntoma descrito. **No se corrigió nada** — se deja pendiente de que el usuario confirme la membresía de grupo de la cuenta real afectada antes de tocar `redirect_by_role` o el fallback a `/admin/`.

No se detectó indicio de que el mismo problema (de existir) afecte a otro grupo — el código es simétrico para los 4 roles internos.

**Validación realizada** (contra la BD real, `Client` de pruebas, sin escrituras salvo las ya explícitas de sesiones anteriores):
- `manage.py check` y `makemigrations --check --dry-run` limpios (no hay cambios de modelo).
- `manage.py test apps.operations`: **5/5 OK** (sin cambios respecto a la sesión 8).
- `trazabilidad_ticket` para el Ticket #11 (proveedor dueño): contiene el link real de SharePoint y el badge "Cargado" para `MP00000041`.
- `/appointments/historial/`: contiene el ícono de COA completo, ya no contiene "Ver Detalle", sí contiene "Ver Ticket y QR" e "Imprimir Cargo".
- `/scheduling/panel/` (como `ucompras`): contiene el botón "Volver" apuntando a `/operations/compras/`.

**Fuera de alcance de esta fase (no tocado):** `get_grouped_by_oc`/`mi_sesion` (vista interna de staff, no reportada como rota); "Imprimir Cargo" (Fase 12, se deja como placeholder); `redirect_by_role`/fallback a `/admin/` (punto 4, sin causa raíz confirmada en código — pendiente de más información del usuario).

### 2026-08-01 (sesión 10) — Panel lateral de citas al hacer click en un slot ocupado (Administración de Horarios)

**Diagnóstico confirmado:** en `/scheduling/panel/`, el click sobre un slot ocupado ya tenía un `onclick="verDetalleSlot(...)"` (`static/js/panel_horarios.js`), pero la función era un placeholder real (`console.log` y nada más — "Expansión futura", nunca implementada). La leyenda del panel (`panel_horarios.html`) ya decía "Click para detalles" sin que existiera esa funcionalidad.

**Implementación:**
- **`apps/scheduling/views.py::ajax_get_citas_slot`** (nuevo, `@compras_required`, `GET /scheduling/api/citas-slot/<slot_id>/`): dado un `AppointmentSlot`, recorre `slot.appointments.select_related('user')` (related_name ya existente, `apps/appointments/models.py`) y por cada `Appointment` devuelve proveedor (nombre y código/RUC = username), y si ya existe `Ticket` (`getattr(appointment, 'ticket', None)`, patrón ya usado en `api_lineas_cita`), su `id` y `get_estado_display()`; si aún no hay Ticket (cita `SOLICITADO`, no confirmada por Compras), devuelve `ticket_id: null` y el `get_status_display()` de la cita en su lugar.
- **Ruta nueva** `scheduling:ajax_get_citas_slot` en `apps/scheduling/urls.py`.
- **Panel lateral (offcanvas de Bootstrap 5)** agregado en `panel_horarios.html` — no había ningún patrón de offcanvas/popover previo en el proyecto (se buscó explícitamente), así que se introdujo el componente offcanvas nativo de Bootstrap (ya cargado globalmente vía `base.html`, sin dependencias nuevas) en vez de un modal, para calzar literalmente con "panel lateral" pedido.
- **`static/js/panel_horarios.js`**: `construirCeldaSlot` ahora pasa `slot.citas_count` al `onclick`; `verDetalleSlot(slotId, fecha, hora, citasCount)` reescrito con lógica real: si `citasCount` es `0`, no abre nada — solo un toast "Sin citas" (una de las dos opciones pedidas explícitamente); si hay citas, abre el offcanvas y hace `fetch` a `ajax_get_citas_slot`. Nueva función `construirListaCitas(citas)` arma cada ítem con proveedor, código, badge de estado, y un botón "Ver" que enlaza a `/operations/ticket/<ticket_id>/trazabilidad/` (Fase 5, reutilizada tal cual, sin duplicar la vista) — el botón solo aparece si `ticket_id` no es `null`; para citas aún sin confirmar se muestra únicamente "Cita #<id> (sin confirmar)", sin enlace (no hay Ticket todavía al que apuntar).
- Bump de `?v=1.0` a `?v=1.1` en el `<script>` de `panel_horarios.js` (cache-busting, mismo patrón ya usado en el resto de JS del proyecto).

**Validación realizada** (contra la BD real, `Client` de pruebas):
- `manage.py check` y `makemigrations --check --dry-run` limpios (no hay cambios de modelo).
- `manage.py test apps.operations`: **5/5 OK** (sin cambios respecto a la sesión 9).
- `ajax_get_citas_slot` probado contra un slot real con cita (`slot #25`, 2026-04-29 14:00): devuelve `ticket_id=2`, proveedor `P20100055237`, estado `Finalizado`.
- Probado contra un slot real sin citas (uno de los 24 creados en la sesión 7b): devuelve `citas: []` — la UI, al recibir `citas_count=0` desde `slots_json`, ni siquiera llega a hacer la llamada (se corta antes con el toast).
- `/scheduling/panel/` renderiza `200` con el offcanvas presente en el HTML y el script apuntando a la versión `v=1.1`.
- No se pudo ejercitar en vivo el camino "cita sin confirmar, sin Ticket" (no hay ninguna `Appointment` en estado `SOLICITADO` en la BD real en este momento) — verificado solo por lectura de código, mismo patrón `hasattr(appointment, 'ticket')` ya usado y probado en `api_lineas_cita`.

**Fuera de alcance de esta fase (no tocado):** no se agregó paginación/scroll infinito al panel lateral (un slot no debería tener más de `max_capacity` citas, que en la práctica es un número bajo); no se tocó el resto de acciones del panel (bloquear/desbloquear/eliminar/generar semana); no se tocó `etapa_actual`, permisos ni modelos.

### 2026-08-02 (sesión 11) — Historial de solo lectura (Fase 6) replicado en Vigilancia, Calidad y Portal del Proveedor

Alcance explícitamente acotado por el usuario: replicar el MISMO patrón de la Fase 6 (pestañas Bootstrap + tabla + link a `trazabilidad_ticket`), sin reinventar. Sin tocar `etapa_actual`, permisos de escritura, ni `ajax_registrar_inspeccion`. Sin implementar todavía el filtro por mes (Fase 10, pendiente).

**Refactor previo (habilita la reutilización real, no solo visual):** se extrajo la tabla de historial de `panel_compras.html` a un partial nuevo, `apps/operations/templates/operations/_partials/historial_tickets.html` (mismo patrón ya aplicado en Fases 4-5 con `_partials/tabla_inspeccion.html` y `_partials/stages_stepper.html` — evitar duplicar HTML). `panel_compras.html` ahora incluye ese partial en vez de tener la tabla inline; se verificó que renderiza exactamente igual que antes (mismos 4 tickets reales, mismos links).

**1. Panel Vigilancia** (`apps/operations/views.py::panel_vigilancia`, `panel_vigilancia.html`): se agregó `tickets_historial` al contexto (`Ticket.objects...order_by('-fecha_creacion')`, sin filtro de fecha ni de estado — a diferencia de `base_qs`, que sigue igual, filtrado a `appointment__slot__date=hoy`). La plantilla ahora envuelve el Kanban existente (Programados/En Planta/Finalizados de hoy, sin ningún cambio de contenido ni de lógica) en una pestaña **"Hoy"**, y agrega una pestaña **"Historial"** con el partial `historial_tickets.html` — cualquier ticket, de cualquier fecha, es consultable en modo solo lectura.

**2. Panel Calidad** (`apps/operations/views.py::panel_calidad`, `panel_calidad.html`): mismo patrón — `tickets_historial` agregado al contexto, el listado de tickets pendientes de inspección (con su formulario inline de `ajax_registrar_inspeccion`, sin tocar) queda envuelto en una pestaña **"Pendientes"**, y se agrega **"Historial"** con el mismo partial. Antes, en cuanto Calidad terminaba su inspección, el ticket desaparecía del panel sin ningún rastro consultable desde ahí.

**3. Portal del Proveedor** (`templates/appointments/portal_proveedor.html`) — bug de raíz encontrado y corregido: el bloque completo (QR + carga de COA + botón "Ver mi Ticket") estaba condicionado a `{% if cita.status == 'CONFIRMADA' and cita.token_qr %}`. `OperationsService.registrar_salida` (sin tocar en esta sesión) cambia `Appointment.status` a `'FINALIZADA'` al cerrar el ticket (`apps/operations/services.py`, ya existente desde antes) — por eso el proveedor perdía el bloque completo, incluido "Ver mi Ticket", justo cuando el ticket terminaba. Confirmado con datos reales: `Ticket #11` → `estado=FINALIZADO`, `appointment.status=FINALIZADA`.
- **Fix**: la condición externa ahora es solo `{% if cita.token_qr %}` (hay Ticket, sin importar su estado). Dentro, el QR + "Cargar COA por Ítem" (sin tocar su lógica ni gating interno) solo se muestran si `cita.status == 'CONFIRMADA'` (idéntico a antes para ese sub-bloque); en cualquier otro estado se muestra un texto neutro con `cita.get_status_display`. El botón **"Ver mi Ticket" ahora es incondicional siempre que exista Ticket**, y se cambió su destino de `operations:detalle_ticket` a `operations:trazabilidad_ticket` (pedido explícito) — no se tocó la protección de esa vista (`@staff_o_proveedor_required` + validación de propiedad, Fase 5, intacta).
- **Bug gemelo encontrado, NO corregido (fuera del alcance nombrado):** `templates/appointments/historial_listado.html` (el dropdown de "Historial de Entregas", ya tocado en la sesión 9) tiene la misma condición `{% if cita.status == 'CONFIRMADA' and cita.token_qr %}` para su ítem "Ver Ticket y QR" → mismo síntoma, el proveedor tampoco puede llegar a un ticket finalizado desde ahí. No se tocó porque el pedido nombraba explícitamente "Portal del Proveedor"; además ese enlace SÍ muestra el QR real (vía `detalle_ticket`), cosa que `trazabilidad_ticket` no hace, así que la corrección ahí requiere decidir si se pierde esa capacidad para tickets activos o se maneja distinto — se deja pendiente de confirmación antes de tocarlo.

**Validación realizada** (contra la BD real, `Client` de pruebas, sin escrituras):
- `manage.py check` y `makemigrations --check --dry-run` limpios (no hay cambios de modelo).
- `manage.py test apps.operations`: **5/5 OK** (sin cambios respecto a la sesión 10).
- `panel_compras` tras el refactor a partial: sigue mostrando los 4 tickets reales con sus links a trazabilidad, sin diferencia visible.
- `panel_vigilancia`: pestañas "Hoy"/"Historial" presentes; los 4 tickets reales (incluidos los de abril, muy anteriores a "hoy") aparecen en "Historial" con su link a trazabilidad.
- `panel_calidad`: pestañas "Pendientes"/"Historial" presentes; mismos 4 tickets en "Historial".
- `portal_proveedor` para el dueño del Ticket #11 (`FINALIZADO`): "Ver mi Ticket" presente, apuntando a `/operations/ticket/11/trazabilidad/` (ya no a `detalle_ticket`).

**Fuera de alcance de esta fase (no tocado, según lo pedido):** filtro por mes (Fase 10, pendiente); `historial_listado.html` (bug gemelo detectado, no corregido — pendiente de confirmación); `etapa_actual`, `_validar_etapa`, `grupo_requerido_por_etapa`, `ajax_registrar_inspeccion` y el formulario inline de Calidad (sin tocar su lógica de escritura); paginación en los nuevos historiales (con 4 tickets reales no hace falta todavía).

### 2026-08-02 (sesión 12) — Fase 10: filtro de período (mes/año) en los 4 historiales, con mes actual por defecto

**Componente compartido (factorizado en 2 capas, reutilizado por los 4 historiales — no se implementó 4 veces):**
- **`apps/base/filters.py::resolver_periodo(request)`** (nuevo): lee `request.GET['periodo']` (formato `YYYY-MM`, el que produce nativamente `<input type="month">`); si falta o es inválido, devuelve el mes/año **actual** como valor por defecto. Devuelve `(anio, mes, periodo_str)` — `periodo_str` siempre normalizado, para repoblar el input incluso cuando se usó el default. Se eligió `apps/base/` (no `apps/operations/`) porque el mismo helper se usa también desde `apps/appointments` (Portal del Proveedor) — evita import cruzado entre apps de negocio.
- **`templates/partials/campo_filtro_periodo.html`** (nuevo, a nivel de proyecto — no dentro de `apps/operations/templates/`, justamente porque lo consume también `apps/appointments`): el campo `<input type="month" name="periodo">` solo, pensado para embeberse dentro del `<form method="get">` que ya existe en cada página (no es un `<form>` propio), ya que las 4 páginas tienen formularios de filtro con formas distintas.

**Los 3 historiales de `apps/operations` (Compras/Vigilancia/Calidad) comparten el mismo partial de tabla desde la sesión 11** (`_partials/historial_tickets.html`), así que el filtro de período se agregó **una sola vez, ahí mismo** (como un `<form>` propio al inicio del partial, con inputs ocultos `q`/`fecha` — solo presentes en el contexto de `panel_compras`, no-op en Vigilancia/Calidad donde esas variables no existen) y automáticamente quedó disponible en los 3 paneles sin tocar sus plantillas. Cada vista (`panel_compras`, `panel_vigilancia`, `panel_calidad`) ahora llama `resolver_periodo(request)` y filtra su `tickets_historial` por `appointment__slot__date__year`/`__month`, y pasa `periodo` al contexto. **El filtro NO aplica a las colas activas** ("Pendientes" en Compras/Calidad, "Hoy" en Vigilancia) — no tendría sentido acotarlas por mes, y no se tocó su lógica.
- En `panel_compras.html`, que ya tenía su propio filtro (`q`/`fecha`, aplicado a ambas pestañas desde la sesión 8) en un `<form>` separado arriba de las pestañas, se agregó un campo oculto `<input type="hidden" name="periodo" value="{{ periodo }}">` a ESE formulario, para que enviarlo no resetee el período que el usuario haya elegido en la pestaña Historial (y viceversa: el formulario de período reenvía `q`/`fecha` como ocultos). Verificado con datos reales que ambos formularios se preservan mutuamente al enviarse por separado.

**4. Portal del Proveedor** (`apps/appointments/views.py::PortalProveedorView`, `templates/appointments/portal_proveedor.html`): el "Expediente de Citas" (`context['historial']`, la lista de citas en la barra lateral del portal — no `historial_listado.html`, que ya tenía paginación propia desde antes y no fue tocado) era el que realmente "cargaba todos los registros sin límite" (sin paginación, sin filtro de fecha, solo `q`/`estado` opcionales). Se agregó `resolver_periodo(request)` filtrando por `slot__date__year`/`__month`, y se extendió el formulario de búsqueda existente (antes solo `q`) para incluir el campo de período (reutilizando el mismo partial), con un botón de filtro adicional. Se ajustó también la etiqueta del KPI "Mis Solicitudes" → **"Solicitudes del Período"**, porque tras el filtro ese número ya no representa el total histórico sino solo el mes visible (consecuencia directa del cambio, no una funcionalidad nueva).

**Validación realizada** (contra la BD real, `Client` de pruebas, sin escrituras):
- `manage.py check` y `makemigrations --check --dry-run` limpios (no hay cambios de modelo).
- `manage.py test apps.operations`: **5/5 OK** (sin cambios respecto a la sesión 11).
- Con los 4 tickets reales repartidos en 3 meses distintos (`#1`/`#2`: abril 2026; `#3`: mayo 2026; `#11`: agosto 2026 — el mes "actual" del sistema): sin parámetro `periodo`, los 4 historiales (Compras/Vigilancia/Calidad/Proveedor) muestran únicamente el `#11`; con `?periodo=2026-04`, Compras muestra `#1` y `#2` y oculta `#3`/`#11` — coincide exactamente con lo esperado en todos los casos probados.
- Verificado que el `<input type="month">` de cada historial se repuebla con el mes actual (`2026-08`) cuando no se pasa `periodo`.
- Verificado que en `panel_compras` el formulario de arriba (`q`/`fecha`) y el del Historial (`periodo`) se preservan mutuamente al enviarse por separado (campos ocultos cruzados), y que "Pendientes" no se ve afectado por `periodo` aunque apunte a un mes sin tickets.

**Fuera de alcance de esta fase (no tocado, según lo pedido):** `historial_listado.html` ("Historial de Entregas", `/appointments/historial/`) no recibió el filtro de período — ya tenía paginación (`paginate_by=10`) y filtros propios desde antes, y no es el historial "sin límite" que describía el pedido; queda pendiente de que el usuario confirme si también lo quiere acotado por período. No se tocó `etapa_actual`, permisos de escritura, ni `ajax_registrar_inspeccion`. No se agregó paginación adicional (el filtro de período ya acota el volumen).

### 2026-08-02 (sesión 13) — Datos de Ingreso (placa/conductor): diseño confirmado + implementación, cierre de Fase 11

**Diseño acordado con el usuario antes de tocar código** (mismo criterio que `TicketLineCOA`, Fase 1):
1. El dato vive en un modelo **nuevo asociado al `Ticket`**, no en `Appointment` — Vigilancia valida contra el `Ticket`, y el dato es pre-flujo (independiente de `etapa_actual`), igual que el COA.
2. Se pide en el portal del proveedor **después de que Compras confirma la cita** (existe `Ticket`) y **hasta que Vigilancia autoriza el ingreso** — no al solicitar la cita (el vehículo/conductor normalmente no se conoce con tanta anticipación).
3. **Confirmado por el usuario**: `conductor_nombre` es un solo campo (nombre y apellidos juntos, sin separar). **Confirmado**: informativo, NO bloqueante — Vigilancia puede autorizar el ingreso aunque falte o esté incompleto; si falta, se muestra explícitamente "No completado por el proveedor" (nunca se omite en silencio).

**Implementación:**
- **Modelo `TicketDatosIngreso`** (`apps/operations/models.py`): `OneToOneField(Ticket, related_name='datos_ingreso')` — un solo registro por Ticket (no por línea de OC, a diferencia de `TicketLineCOA`). Campos `placa_vehiculo`, `conductor_dni` ("DNI/CE del conductor"), `conductor_nombre` ("Nombre y apellidos del conductor"), los 3 `blank=True` (se acepta guardar parcial); `actualizado_por` (FK User) y `fecha_actualizacion` (`auto_now`), mismo patrón de auditoría que `TicketLineCOA.subido_por`/`fecha_carga`. Migración `apps/operations/migrations/0006_ticketdatosingreso.py`, generada y **aplicada** contra la BD real.
- **`OperationsService.registrar_datos_ingreso(ticket_id, placa_vehiculo, conductor_dni, conductor_nombre, usuario)`** (`apps/operations/services.py`): `update_or_create` sobre `TicketDatosIngreso`, dentro de `@transaction.atomic`. No toca `etapa_actual` ni ninguna otra lógica operativa — mismo principio que `registrar_coa_proveedor`.
- **Endpoint AJAX `registrar_datos_ingreso_ajax`** (`apps/appointments/views.py`, `POST /appointments/<appointment_id>/datos-ingreso/`, ruta `apps/appointments/urls.py`): calcado de `subir_coa_linea_ajax` — exige `appointment.status == 'CONFIRMADA'` y `appointment.ticket.estado == 'PROGRAMADO'` (mismo guard exacto, mismo mensaje de error "el proveedor ya ingresó a planta" adaptado). Exige al menos un campo no vacío (evita guardados totalmente en blanco), pero no exige los 3 — consistente con "no bloqueante".
- **Formulario del proveedor** (`templates/appointments/portal_proveedor.html`): nuevo botón "Datos del Vehículo / Conductor" + panel colapsable con los 3 campos, en la MISMA tarjeta donde ya vivía "Cargar COA por Ítem" (dentro del mismo `{% if cita.status == 'CONFIRMADA' %}` de la sesión 11) — visible y editable en la misma ventana que el COA. `PortalProveedorView.get_context_data` anota `cita.datos_ingreso_actual` (mismo patrón que `ticket.coa_completo` de la sesión 6) para repoblar el formulario sin consulta extra por fila. JS nuevo en `static/js/panels/portal_proveedor.js`: `toggleDatosIngreso`/`guardarDatosIngreso` (async/await, mismo estilo que `subirCoaLinea`), usando `DzApi.postFormData` ya existente.
- **Vista de solo lectura para Vigilancia** (`apps/operations/templates/operations/detalle_ticket.html`): nueva sección "Datos del Vehículo / Conductor" dentro del mismo bloque `{% if acciones.puede_autorizar_ingreso %}` que ya mostraba "Estado de Certificados (COA)" — a diferencia del COA (que solo se muestra si `tipo_flujo == 'CON_CALIDAD'`), esta sección se muestra **siempre** (aplica a cualquier flujo). Cada campo usa `{% if ticket.datos_ingreso.<campo> %}...{% else %}<span class="text-danger fst-italic">No completado por el proveedor</span>{% endif %}` — cubre tanto "no existe ningún registro" como "existe pero el campo específico está vacío" con el mismo código (Django resuelve un `OneToOneField` inverso inexistente como fallo silencioso en templates, `ObjectDoesNotExist.silent_variable_failure = True`). Vigilancia no tiene ningún formulario aquí — es puramente de lectura, sin cambios de permisos.
- **Confirmado explícitamente, sin cambios**: `OperationsService.iniciar_ingreso_planta` no fue tocado — no hay ningún check nuevo contra `TicketDatosIngreso`, Vigilancia puede autorizar el ingreso exactamente igual que antes, con o sin estos datos.

**Nota de interacción con la sesión 12 (esperada, no es un bug):** el bloque del proveedor con este formulario vive dentro de la lista `historial` de `portal_proveedor.html`, que desde la sesión 12 está acotada por el filtro de período (mes actual por defecto). Una cita de un mes distinto al actual no aparece en el portal por defecto — hay que pasar `?periodo=YYYY-MM` para verla — igual que le pasaría a cualquier otra tarjeta de esa lista. Se verificó explícitamente durante la validación de esta sesión.

**Validación realizada** (dentro de `transaction.atomic()` con `savepoint_rollback` forzado — Ticket #3 se pasó temporalmente a `PROGRAMADO` para probar el camino positivo, sin dejar datos permanentes; `manage.py test` corrido aparte, normal):
- `manage.py check` y `makemigrations --check --dry-run` limpios; migración aplicada sin errores.
- `manage.py test apps.operations`: **5/5 OK** (sin cambios respecto a la sesión 12).
- Guardado exitoso vía el endpoint mientras `ticket.estado == 'PROGRAMADO'`; los 3 datos aparecen correctamente en `detalle_ticket` para Vigilancia.
- Actualización parcial (DNI vacío a propósito): Vigilancia ve el nuevo valor de placa/nombre y **"No completado por el proveedor" exactamente donde falta el DNI** — no se omite en silencio.
- Una vez que el ticket pasa a `EN_PLANTA`, el mismo endpoint devuelve `400` y **no modifica el registro existente** — confirma el guard "editable mientras PROGRAMADO".
- Con un Ticket real `PROGRAMADO` (temporal) y **sin ningún** `TicketDatosIngreso`, Vigilancia ve "No completado por el proveedor" exactamente **3 veces** (una por campo).
- Confirmado que la sección de Vigilancia solo aparece junto a la validación de COA previa a "Autorizar Ingreso" (`acciones.puede_autorizar_ingreso`) — una vez que el ticket ya está `EN_PLANTA`, la sección completa desaparece junto con esa tarjeta, tal como se pidió ("junto a la validación de COA previa a Autorizar Ingreso").

**Fuera de alcance de esta fase (no tocado, según lo pedido):** no se bloqueó el ingreso por datos faltantes (confirmado explícitamente como no bloqueante); no se tocó `etapa_actual`, `_validar_etapa`, `grupo_requerido_por_etapa` ni ningún permiso existente; no se agregó este dato a los historiales (Fase 6/11) ni a `trazabilidad_ticket` (no pedido, evaluar en fase futura si Compras/Calidad también lo necesitan).

### 2026-08-02 (sesión 14) — Fase 12: "Imprimir Cargo" → PDF, y corrección de `requirements.txt` a UTF-8

**Dependencia nueva evaluada y confirmada con el usuario antes de instalar:** no había ninguna librería de generación de PDF en el proyecto. Se propusieron 3 opciones (WeasyPrint, ReportLab puro, xhtml2pdf) y se eligió **`xhtml2pdf==0.2.17`** — 100% Python, sin dependencias de sistema (a diferencia de WeasyPrint, que requiere Pango/Cairo/GDK-Pixbuf, notoriamente frágil de instalar en Windows), y permite escribir la plantilla como HTML/CSS de Django en vez de armar el PDF por código (a diferencia de ReportLab puro) — consistente con el resto de la app.

**Corrección de `requirements.txt` (Alta prioridad #4 de `INFORME_ANALISIS.md`), aprovechada en esta sesión:**
- El archivo estaba en **UTF-16LE con BOM** (confirmado por bytes: `FF-FE-61-00-73-00...`). Se re-escribió como **UTF-8 sin BOM** real — el primer intento con la herramienta de escritura habitual preservó la codificación UTF-16 original (solo perdió el BOM, seguía siendo 2 bytes por carácter); se forzó la reescritura real vía `.NET` (`System.Text.UTF8Encoding($false)`) para obtener UTF-8 genuino (`61-73-67-69-72-65-66...`, 1 byte por carácter). Verificado con `pip install -r requirements.txt` contra el entorno virtual del proyecto: **todas las líneas resueltas correctamente** ("Requirement already satisfied" para las 12), sin errores de parseo — confirma que el archivo resultante es válido.
- Se agregó `xhtml2pdf==0.2.17` como línea nueva (orden alfabético, al final). No se listaron sus dependencias transitivas (`reportlab`, `pyHanko`, `pypdf`, etc.) explícitamente — el archivo ya era una lista curada de dependencias directas, no un `pip freeze` completo, y pip las resuelve solo.

**Implementación de "Imprimir Cargo":**
- **Plantilla nueva** `apps/operations/templates/operations/cargo_ticket_pdf.html`: documento HTML **standalone** (no extiende `base.html` — xhtml2pdf soporta un subconjunto de CSS 2.1, sin flexbox/grid/box-shadow, así que Bootstrap no serviría). Mismos datos que `trazabilidad_ticket.html` (Fase 5): ID de ticket, proveedor, sede, fecha/hora de la cita, OC(s) vinculada(s), estado del ticket, código QR, y la tabla de Trazabilidad de Etapas (`TicketStage`, ordenada por `fecha_inicio`). Sin gradientes ni layouts complejos, una sola tabla de datos + una tabla de etapas — confirmado que renderiza en una sola página con datos reales.
- **Vista `imprimir_cargo_pdf`** (`apps/operations/views.py`, `GET /operations/ticket/<pk>/cargo/`, ruta `operations:imprimir_cargo` en `apps/operations/urls.py`): mismo criterio de propiedad que `detalle_ticket`/`trazabilidad_ticket` (`@staff_o_proveedor_required` + verificación manual de que un proveedor solo genere el cargo de sus propios tickets). Renderiza la plantilla a string (`render_to_string`), la convierte con `xhtml2pdf.pisa.CreatePDF(html, dest=buffer)`, y responde `HttpResponse(..., content_type='application/pdf')` con `Content-Disposition: inline` (no `attachment`) — así el navegador lo abre listo para imprimir en vez de forzar la descarga.
- **`templates/appointments/historial_listado.html`**: el ítem "Imprimir Cargo" del menú de tres puntos (antes `href="#"`, sin funcionalidad) ahora enlaza a `{% url 'operations:imprimir_cargo' cita.ticket.id %}` con `target="_blank"` (mismo patrón que "Ver Ticket y QR", no reemplaza la navegación del historial). Gateado por `{% if cita.ticket %}` — a diferencia de "Ver Ticket y QR" (que sigue exigiendo `cita.status == 'CONFIRMADA'`, bug gemelo ya reportado en la sesión 11 y todavía sin corregir), "Imprimir Cargo" está disponible en **cualquier estado del ticket, incluido `FINALIZADO`** (tiene sentido como comprobante de entrega ya completada). Se agregó un mensaje "Sin ticket generado todavía" cuando la cita no tiene Ticket, para que el menú no quede vacío en ese caso.

**Validación realizada** (contra la BD real, `Client` de pruebas, sin escrituras — la vista es de solo lectura):
- `manage.py check` y `makemigrations --check --dry-run` limpios.
- `manage.py test apps.operations`: **5/5 OK** (sin cambios respecto a la sesión 13).
- PDF generado contra el Ticket #2 real (`FINALIZADO`): `Content-Type: application/pdf`, `Content-Disposition: inline; filename="cargo_ticket_2.pdf"`, firma de archivo `%PDF` válida, 3.4 KB. **Inspeccionado visualmente** (vía el lector de PDF): una sola página, con los 6 datos de cabecera y las 4 etapas del ticket real, formato limpio y legible.
- Un proveedor distinto al dueño del Ticket #2 es redirigido a `portal_proveedor` al intentar generar su cargo (`200` tras seguir el redirect) — mismo criterio de propiedad que `trazabilidad_ticket`.
- Un usuario de Vigilancia (staff) sí puede generar el cargo de cualquier ticket.
- El link en `/appointments/historial/` apunta a la URL correcta con `target="_blank"`.

**Fuera de alcance de esta fase (no tocado):** no se corrigió el bug gemelo de "Ver Ticket y QR" (gateado por `cita.status == 'CONFIRMADA'`, reportado en la sesión 11); no se agregó un botón de "Imprimir Cargo" a `detalle_ticket.html`/`trazabilidad_ticket.html` (solo se pidió conectar el del Historial de Entregas); no se firmó digitalmente el PDF (el texto del footer ya aclara "no requiere firma para su validez interna").

---

### 2026-08-02 (sesión 15) — Resolución de la "deuda conocida" reportada al cierre de la sesión 14

El usuario pidió resolver los 3 puntos de deuda antes de aceptar el cierre del lote (no se cierra como "lote completo" todavía — queda pendiente de su confirmación final). Los 3 puntos exigían primero **investigar y responder**, y solo corregir código donde correspondiera según la respuesta:

**1. "Ver Ticket y QR" (`historial_listado.html`) vs. "Ver mi Ticket" (`portal_proveedor.html`, sesión 11) — confirmado: eran vistas de destino DISTINTAS, por una razón real.**
- `historial_listado.html` apuntaba a `operations:detalle_ticket` (sin cambios desde la sesión 9); `portal_proveedor.html` apunta a `operations:trazabilidad_ticket` desde la sesión 11.
- Motivo de la diferencia: `detalle_ticket.html` renderiza el widget de QR escaneable (`#qrcode` + JS `QRCode`); `trazabilidad_ticket.html` no lo tiene. El nombre "...y QR" prometía específicamente esa imagen.
- **Resuelto** (corresponde el mismo fix, per lo indicado por el usuario): se unificó el criterio y el destino — `{% if cita.ticket %}` (antes `cita.status == 'CONFIRMADA' and cita.token_qr`) y el link ahora va a `operations:trazabilidad_ticket`, igual que "Ver mi Ticket". Se renombró la etiqueta a **"Ver Ticket"** (ya no muestra QR). Se combinaron los dos bloques `{% if cita.ticket %}` duplicados (este ítem + "Imprimir Cargo", sesión 14) en uno solo. Sin pérdida operativa real: el QR ya se ve de forma inline en la tarjeta de `portal_proveedor.html` mientras la cita sigue `CONFIRMADA`/`PROGRAMADO` — el único momento en que tiene sentido presentarlo físicamente en Vigilancia.
- Verificado con el Ticket #2 real (`FINALIZADO`): "Ver Ticket" (sin "y QR") presente, apunta a `/operations/ticket/2/trazabilidad/`, `200 OK`.

**2. Bug de redirect post-login para COMPRAS — investigado en datos reales, NO corregido (solo reportado, según lo pedido).**
Se listaron los 9 usuarios del sistema completo y sus grupos vía ORM. Resultado: **`ucompras` es la ÚNICA cuenta en el grupo `COMPRAS`**, pertenece exclusivamente a ese grupo (sin membresía múltiple, sin grupos faltantes), y su `last_login` es de **hoy** (2026-08-02) — confirma que es la cuenta que se usó para probar. Esto **descarta por completo** la hipótesis de la sesión 9 (membresía de grupo incorrecta → fallback a `/admin/`). El código de `redirect_by_role` y el flujo completo de login ya se habían verificado funcionando correctamente end-to-end con esta misma cuenta (sesión 9). **No hay ninguna causa identificable en datos ni en código** en este momento — si el síntoma persiste, lo más probable es que sea específico del navegador/cliente usado (caché, extensión, etc.), no del servidor. Se necesitan más detalles de reproducción (navegador, URL exacta en la barra tras el intento, capturas) para seguir investigando; no se tocó ningún archivo.

**3. `historial_listado.html` — confirmado: NO es una de las 4 vistas de la Fase 10, es una vista separada.**
Las 4 vistas filtradas en la Fase 10 (sesión 12) fueron `panel_compras`, `panel_vigilancia`, `panel_calidad` (los 3 comparten `_partials/historial_tickets.html`), y `PortalProveedorView.get_context_data` → `context['historial']` (el "Expediente de Citas", la lista compacta dentro de `portal_proveedor.html`, sin paginación, esa era la que "cargaba todo sin límite").
`historial_listado.html` es servida por `HistorialCitasView` (`ListView`, `/appointments/historial/`, enlazada desde el sidebar como "Mi Historial") — **una página aparte**, con su propia paginación (`paginate_by=10`) y sus propios filtros (`q`, `status`, `sede`) desde antes de esta serie de fases. Su propósito: el historial completo y navegable (por páginas) de todas las citas del proveedor, a diferencia del "Expediente de Citas" del portal principal, que es un resumen reciente sin paginar embebido en el dashboard. **No se le agregó el filtro de período** — no corresponde según lo indicado por el usuario (la instrucción era aplicarlo solo si resultaba ser una de las 4 vistas ya tocadas, y no lo es); si se quiere agregar por consistencia visual en una fase futura, es una decisión aparte a confirmar.

**Validación realizada:**
- `manage.py check` limpio; `manage.py test apps.operations`: **5/5 OK** (sin cambios de lógica de escritura).
- "Ver Ticket" verificado contra el Ticket #2 real (`FINALIZADO`): aparece sin "y QR", apunta a `trazabilidad_ticket`, `200 OK` al acceder directamente.
- Consulta de grupos verificada contra los 9 usuarios reales del sistema (no hay ningún otro candidato a "cuenta de Compras" además de `ucompras`).

**Estado del lote de Fases 7-12: sigue sin cerrarse formalmente — pendiente de confirmación final del usuario**, ahora con la deuda reducida a un único punto genuinamente abierto (punto 2, que requiere más información del usuario para seguir investigando, ya que no hay nada más que el código/los datos puedan revelar en este momento).

### 2026-08-02 (sesión 16) — Causa raíz real del bug de login de COMPRAS encontrada y corregida; cierre definitivo del lote Fases 7-13

El usuario confirmó el punto 2 como bug reproducible (Chrome y Edge por igual, no de datos/sesión) y pidió investigar puntualmente `redirect_by_role` (nombre exacto del grupo, orden de los `if/elif`, que la rama de COMPRAS realmente redirija) antes de corregir a ciegas.

**Los 3 puntos que pidió revisar, uno por uno:**
1. **Nombre del grupo**: `'COMPRAS'` en `redirect_by_role` coincide byte a byte con `'COMPRAS'` en `apps/base/decorators.py::compras_required` y con el nombre real en BD (confirmado sesión 15). Sin discrepancia de mayúsculas/minúsculas ni espacios.
2. **Orden de los `if/elif`**: revisado de nuevo, simétrico entre los 5 roles, sin ninguna condición previa que pudiera interceptar a COMPRAS.
3. **¿La rama de COMPRAS redirige de verdad?** Sí — `return redirect('operations:panel_compras')`, no solo arma contexto.

**Con el código de `redirect_by_role` descartado por tercera vez** (sesiones 9, 15 y ahora esta), se cambió de estrategia: en vez de seguir mirando ese archivo, se reprodujo el flujo completo contra el **servidor real corriendo** (`manage.py runserver`, puerto 8000, el mismo que usa el usuario) vía HTTP real con cookies persistentes (PowerShell `Invoke-WebRequest` con `-SessionVariable`, replicando exactamente lo que hace un navegador) — no el `Client` de pruebas de Django, que no pasa por la pila HTTP/WSGI real y podía estar ocultando algo. Resultado: **el flujo funciona perfecto también por HTTP real** (`POST /login/` → `302` → `302` → `200` en `/operations/compras/`, título "Panel Compras" confirmado en el HTML). Se intentó además reproducir literalmente en Chrome vía las herramientas de automatización de navegador, pero la extensión no estaba conectada en este entorno.

**Causa raíz real, encontrada al probar deliberadamente con una contraseña incorrecta contra el servidor real:** `templates/login.html` **no muestra ningún mensaje de error cuando el login falla**. Un `POST` con credenciales inválidas responde `200` sobre `/login/`, sin ninguna clase de error (`is-invalid`, `alert-danger`, etc.), sin cookie de sesión, y con el formulario **visualmente idéntico** al estado inicial — confirmado en la respuesta HTTP real. Un usuario no tiene ninguna señal de que su intento falló; ve exactamente "la pantalla de login sigue ahí", que es palabra por palabra el síntoma reportado.

Esto **no es un bug de `redirect_by_role`** (comprobado limpio 3 veces, por 3 métodos distintos) — es un bug genérico de UX en `login.html`, que solo se manifestó de forma reproducible con la cuenta de Compras porque el navegador de prueba probablemente tiene guardada/autocompletada una contraseña vieja o incorrecta para esa cuenta específica (`ucompras` fue reseteada en la sesión 7b; si el navegador guardó una contraseña de antes de ese reset, cada intento fallaría en silencio, siempre igual, en cualquier navegador que tenga esa credencial vieja guardada — coincide con "reproducible en Chrome y Edge por igual").

**Fix aplicado** (`templates/login.html`): se agregó un bloque `{% if form.errors %}` con una alerta Bootstrap visible ("Usuario o contraseña incorrectos. Verifique e intente nuevamente."), más la clase `is-invalid` en ambos campos y `value="{{ form.username.value }}"` para no perder lo ya tecleado. Django's `AuthenticationForm` ya generaba el error internamente (`form.errors`) — simplemente nunca se renderizaba.

Se reconfirmó (reseteo idempotente, incluido para eliminar cualquier duda) la contraseña de `ucompras` a `Prueba2026!`. **Se recomienda al usuario limpiar la contraseña guardada/autocompletada para esa cuenta en el navegador antes de volver a probar** — el fix de UI hace visible el error, pero no puede corregir una credencial mal guardada en el propio navegador.

**Validación realizada** (contra el servidor real corriendo, HTTP real, sin usar el `Client` de pruebas para esta parte):
- Con contraseña incorrecta: `200` sobre `/login/`, alerta de error visible, clase `is-invalid` presente — confirmado antes y después del fix (antes: sin alerta; después: con alerta).
- Con la contraseña correcta (`Prueba2026!`): `200` final sobre `/operations/compras/`, título "Panel Compras | Daryza VBS" — sigue funcionando exactamente igual tras el cambio de plantilla.
- `manage.py check` limpio; `manage.py test apps.operations`: **5/5 OK**.

**Fuera de alcance de esta sesión:** no se tocó `redirect_by_role`, `compras_required`, ni ningún otro decorador o vista — ninguno tenía el bug. No se implementó recuperación de contraseña (el link "¿Olvidó su contraseña?" en `login.html` sigue siendo un placeholder `href="#"`, no reportado como parte de este problema).

---

## Cierre definitivo del lote — Fases 7 a 13 (sesiones 6 a 16, 2026-08-01 a 2026-08-02)

Con la sesión 16 se resuelven los 3 puntos de deuda pendientes de la sesión 14 (uno corregido en la sesión 15, dos en esta sesión) y se cierra el lote completo de fases aditivas construidas sobre el plan original de 5 fases (sesiones 1-7):

- **Fase 6** (sesión 8): historial de Tickets de solo lectura en Panel Compras.
- **Fase 7** (sesión 9): 4 correcciones puntuales (COA vacío, "Ver Detalle" eliminado, botón Volver, investigación inicial del bug de login).
- **Fase 8** (sesión 10): panel lateral al hacer click en un slot ocupado.
- **Fase 9** (sesiones 11 y 15): historial replicado en Vigilancia/Calidad; "Ver mi Ticket"/"Ver Ticket" ya no desaparecen al finalizar, en **ambos** puntos de entrada (portal principal e Historial de Entregas).
- **Fase 10** (sesión 12): filtro de período (mes/año) en los 4 historiales correspondientes; confirmado que `historial_listado.html` es una vista aparte, fuera de ese alcance a propósito.
- **Fase 11** (sesión 13): modelo `TicketDatosIngreso`, informativo para Vigilancia.
- **Fase 12** (sesión 14): "Imprimir Cargo" → PDF vía `xhtml2pdf`; `requirements.txt` recodificado a UTF-8 real.
- **Fase 13** (sesión 16, esta sesión): causa raíz real del bug de login (falta de feedback de error en `login.html`, no relacionado con `redirect_by_role`) encontrada y corregida.

**Sin deuda pendiente conocida al cierre de este lote.**

### 2026-08-02 (sesión 17) — Actualización documental de `INFORME_ANALISIS.md` (cierre documental completo)

Sesión puramente documental: **no se modificó ningún archivo de código**, solo `INFORME_ANALISIS.md`. Se releyó `CLAUDE.md` completo (historial de sesiones 1-16) y se verificó contra el código actual (no solo memoria) cuál de los 23 hallazgos de la sección 2 del informe (CRÍTICO 1-6, MEDIO 7-17, BAJO 18-23) quedó realmente corregido a lo largo de las sesiones 3-16, antes de anotar nada.

**Hallazgos marcados como RESUELTO (verificados contra el código actual, no solo contra lo narrado en sesiones previas):**
- **CRÍTICO #2** (`evidencia_url` mal usado para URLs de OneDrive) → sesión 3. Verificado: `evidencia_url` no se asigna en ningún punto del código actual.
- **CRÍTICO #5** (`requirements.txt` en UTF-16LE) → sesión 14.
- **MEDIO #16** (fallback muerto `getattr(line, 'coa_url', None)` en `detalle_ticket`) → resuelto como efecto colateral de la sesión 6 (nunca fue el objetivo explícito de esa sesión, pero el fallback desapareció al reescribirse el cálculo de COA). Verificado: no queda ningún rastro de ese patrón en `views.py`.
- **BAJO #23** (sin suite de tests) → marcado **parcialmente** resuelto, sesión 5 (`apps/operations/tests.py`, 5 tests, cobertura acotada al candado de permiso por etapa — el resto del proyecto sigue sin tests).

**Hallazgos verificados como NO tocados (confirmado por lectura de código, no solo ausencia de mención en `CLAUDE.md`), dejados sin marcar:** CRÍTICO #1 (`insp.coa_url.url` — confirmado que el bug **sigue presente** en `get_resumen_ticket`, línea 689 de `services.py`), CRÍTICO #3, #4, #6 (`services/` sigue existiendo, confirmado con `Glob`); MEDIO #7, #8, #9, #10, #11, #12, #13, #14, #17; BAJO #15 (confirmado que no existe ningún `admin.py` en ninguna app, vía `Glob`), #18, #19, #20, #21, #22.

**Sección 5** (diagnóstico del flujo de etapas) no se tocó punto por punto — fuera del alcance pedido (solo sección 2); se agregó una nota de una línea señalando que sus 3 conclusiones ya fueron resueltas por el plan de 5 fases (sesiones 4-6) y remitiendo al cierre narrativo que ya tiene en `CLAUDE.md` (sesión 7).

**Sección 6 nueva** ("Funcionalidades agregadas post-análisis inicial"): lista las 10 piezas de funcionalidad nueva construidas en las Fases 1-13 que no corrigen ningún hallazgo del informe original (`TicketLineCOA`, `etapa_actual`, permiso por etapa, `trazabilidad_ticket`/`scan_qr`, historiales por rol, panel lateral de horarios, filtro de período, `TicketDatosIngreso`, generación de PDF), más una mención aparte de los bugs adicionales encontrados y corregidos en el camino que no estaban en el diagnóstico original (visibilidad de Calidad, COA vacío del proveedor, acceso perdido a ticket finalizado, falta de feedback de error en login).

**Fuera de alcance de esta sesión (no tocado, según lo pedido):** secciones 3 y 4 de `INFORME_ANALISIS.md` (brechas de flujo y próximos pasos priorizados) — no se pidió actualizarlas, aunque alguna referencia cruzada ahí (p. ej. el ítem "Alta: re-codificar requirements.txt" en la sección 4) ya está resuelta según la sección 2 actualizada.

**Cierre documental completo:** con esta sesión, tanto `CLAUDE.md` como `INFORME_ANALISIS.md` reflejan con precisión el estado real del código a 2026-08-02 — el lote de Fases 7-13 (sesión 16) y el informe de diagnóstico original (sesión 1-2) quedan reconciliados entre sí.

### 2026-08-02 (sesión 18) — Cierre de los 4 CRÍTICOS originales que quedaron sin tocar en 13 fases

Con esta sesión, **los 6 hallazgos CRÍTICO del informe original quedan resueltos** (2 y 5 ya lo estaban desde las sesiones 3 y 14; 1, 3, 4 y 6 se cierran aquí).

**1. CRÍTICO #1 (`insp.coa_url.url`, `get_resumen_ticket`).** Investigado primero, tal como se pidió: el endpoint que lo consume (`ajax_get_ticket_json`, `/operations/api/ticket/<pk>/json/`) **sigue registrado y accesible**, pero una búsqueda en todo `static/` y en todos los `.html` del proyecto confirma que **ningún template ni archivo JS lo llama** — quedó reemplazado de facto por `detalle_ticket`/`trazabilidad_ticket` (que usan `get_estado_actual_por_oc`, ya corregido desde la sesión 9). Es código huérfano desde la UI, aunque técnicamente "vivo" en el routing. Se corrigió de todas formas (`insp.coa_url or ''`, sin `.url`) en vez de eliminarlo — arreglar una línea es gratis y elimina una trampa activa; **queda pendiente la decisión del usuario sobre si además se elimina el endpoint completo** (vista + ruta + método de servicio) o se deja funcional por si se reutiliza más adelante. Verificado con el Ticket #11 real (con COA cargado): el endpoint responde `200` con el `coa_url` real como string, sin excepción — antes habría lanzado `AttributeError`.

**2. CRÍTICO #3 (`core/settings.py`).** Antes de tocar nada se confirmó que `core/.env` ya tenía `DJANGO_SECRET_KEY`, `DB_PASSWORD` y `DEBUG` definidos (se verificó la presencia de las claves, sin exponer sus valores), así que quitar los fallbacks no iba a romper el entorno de trabajo actual. Cambios: `SECRET_KEY` y `DB_PASSWORD` ya no tienen default inseguro — si faltan en `.env`, `settings.py` lanza `ImproperlyConfigured` con mensaje claro al arrancar (falla fuerte, no silenciosa). `DEBUG` pasa a `default='False'` (antes `'True'`). `ALLOWED_HOSTS` ahora se lee de la variable de entorno `ALLOWED_HOSTS` (separada por comas), con `localhost,127.0.0.1` como default — preserva el comportamiento actual sin obligar a tocar `.env`.

**3. CRÍTICO #4 (`test_api.py`).** El token DRF hardcodeado se reemplazó por `os.getenv('SAP_SYNC_TOKEN')` (vía `core/.env`, reutilizando `python-dotenv` ya presente en el proyecto) con `input()` como respaldo si no está definida. **Acción pendiente del usuario, fuera del alcance de este cambio**: el token real que estuvo expuesto en texto plano en el archivo (visible en el historial/copias del repo) sigue siendo válido en el sistema DRF real hasta que se rote manualmente — Claude no tiene forma de rotarlo ni acceso al sistema de tokens real.

**4. CRÍTICO #6 (`services/`).** Re-verificado por tercera vez (informe original, sesión 17, y esta sesión) con una búsqueda exhaustiva en todo el árbol `.py` del proyecto (`from services`, `import services`, `services.services`, `services.operations_service`): **cero coincidencias**. Con la autorización explícita del usuario en el pedido, se **eliminó la carpeta `services/` completa** (`services.py`, `operations_service.py`, `__init__.py`). Advertencia registrada: el proyecto **no tiene `git` inicializado**, así que esta eliminación no es reversible por control de versiones — se verificó `manage.py check` y la suite de tests (`5/5 OK`) inmediatamente después de borrar para confirmar que nada dependía de esa carpeta.

**Higiene adicional en `CLAUDE.md` (consecuencia directa de estos cambios, no un punto nuevo):** se actualizó la lista de variables de entorno esperadas (agrega `ALLOWED_HOSTS` opcional y `SAP_SYNC_TOKEN` opcional, y marca `DJANGO_SECRET_KEY`/`DB_PASSWORD` como obligatorias); se actualizaron las dos notas de advertencia obsoletas cerca del inicio del archivo (la de `requirements.txt` en UTF-16, ya resuelta desde la sesión 14 pero nunca actualizada ahí, y la de "no ejecutar `test_api.py` sin rotar el token", ajustada para reflejar que el token ya no está hardcodeado pero el token viejo sigue pendiente de rotación real).

**Validación realizada** (contra el entorno real, sin base de datos de prueba salvo la de `manage.py test`):
- `manage.py check` limpio después de cada uno de los 4 cambios (settings.py, eliminación de `services/`).
- `manage.py test apps.operations`: **5/5 OK** (sin cambios de lógica operativa; los 4 fixes son de configuración/seguridad/limpieza, no tocan `etapa_actual` ni permisos).
- `ajax_get_ticket_json` verificado contra el Ticket #11 real: `200`, `coa_url` real presente como string, sin excepción.
- `core/.env` verificado que ya tenía las claves necesarias antes de quitar los fallbacks (sin imprimir sus valores).
- `services/` verificado que ya no existe (`Test-Path` → `False`) y que el proyecto sigue funcionando sin ella.

**Fuera de alcance de esta sesión (no tocado, según lo pedido):** no se eliminó el endpoint `ajax_get_ticket_json` completo (decisión pendiente del usuario); no se rotó el token real en el sistema DRF (el usuario debe hacerlo); no se tocaron las secciones 3/4 de `INFORME_ANALISIS.md`; no se inicializó `git` (mencionado aquí solo como contexto de riesgo de la eliminación de `services/`, no se tomó ninguna acción al respecto).

### 2026-08-02 (sesión 19) — Inicialización de `git` + eliminación definitiva de `ajax_get_ticket_json`. Cierre del ciclo de 19 sesiones.

**Parte 1 — Repositorio git (red de seguridad, sin push):**
- Confirmado con `git status` (y verificando que no hubiera `.git` oculto ni en este directorio ni en ningún padre) que el proyecto **no tenía control de versiones**. Se inicializó (`git init`, rama `main`).
- **`.gitignore`** creado: lo pedido (`env/`, `__pycache__/`, `*.pyc`, `.env`, `media/`, `staticfiles/`) más dos hallazgos propios al revisar la raíz del proyecto: `.venv/` (un **segundo** entorno virtual además de `env/`, no documentado antes) y `.claude/settings.local.json` (config local de Claude Code, por convención no se versiona). Verificado con `git check-ignore -v` sobre cada ruta sensible (`core/.env`, `env/`, `.venv/`, `media/`, `.claude/settings.local.json`) **antes** de hacer `git add`, no solo asumido por el patrón del archivo.
- **Hallazgo antes del primer commit (respuesta al punto 4 pedido explícitamente):** el token real que estaba hardcodeado en `test_api.py` (ya reemplazado por variable de entorno en la sesión 18) seguía **citado textualmente** en `INFORME_ANALISIS.md`, como evidencia documentada del hallazgo original. Comitearlo así lo habría dejado permanente en el historial de git (aunque sea un repo 100% local). Se redactó el valor en `INFORME_ANALISIS.md` (`7f799b11...706d0 [redactado]`) antes de comitear — el hallazgo sigue documentado, solo sin el valor real expuesto. No se encontró ningún otro archivo con `.env`, credenciales, o el token trackeado o a punto de subirse.
- **Commit base** `bece686` — "Estado post plan de remediación Fases 1-18" (97 archivos). Sin push a ningún remoto (no pedido, explícitamente diferido a decisión del usuario).

**Parte 2 — Eliminación definitiva de `ajax_get_ticket_json`/`get_resumen_ticket`:**
- Búsqueda final en **todo** el proyecto (no solo `views.py`/`urls.py` como en la sesión 18) por `ajax_get_ticket_json` y `get_resumen_ticket`: los únicos resultados fuera de documentación (`CLAUDE.md`/`INFORME_ANALISIS.md`) son la definición de la vista (`views.py:807`), su registro de ruta (`urls.py:99-100`) y la definición/único-caller del método de servicio (`services.py:633`, llamado solo desde la propia vista en `views.py:810`) — **sin ninguna referencia nueva** respecto a lo ya confirmado en la sesión 18.
- Eliminados: `ajax_get_ticket_json` (`apps/operations/views.py`), su ruta `api/ticket/<pk>/json/` (`apps/operations/urls.py`), y `OperationsService.get_resumen_ticket` completo (`apps/operations/services.py`, incluido el bug original de `coa_url.url` ya corregido en la sesión 18 — ahora el código que lo contenía deja de existir).
- Verificado: `manage.py check` limpio, `manage.py test apps.operations` **5/5 OK**, `reverse('operations:ajax_get_ticket_json')` lanza `NoReverseMatch`, y `GET /operations/api/ticket/1/json/` responde `404` (antes `200` con el bug ya corregido en la sesión 18).
- **Commit separado** `be24cbe` — "Elimina endpoint huérfano ajax_get_ticket_json (código muerto desde Fase 1, sin referencias activas)" (3 archivos, 107 líneas eliminadas), sin mezclar con el commit base.

**`INFORME_ANALISIS.md`** actualizado: CRÍTICO #1 marcado como ✅ **RESUELTO Y ELIMINADO** (no solo corregido — el código que tenía el bug ya no existe); referencia cruzada a este commit. **Los 6 hallazgos CRÍTICO del informe original quedan cerrados.**

**Cierre del ciclo completo (19 sesiones):** informe de diagnóstico inicial (sesión 1) → diagnóstico del flujo de etapas (sesión 2) → plan original de 5 fases sobre `Ticket`/`TicketLineInspection` (sesiones 3-7) → datos de prueba (sesión 7b) → lote de Fases 6-12 aditivas (sesiones 8-14) → resolución de deuda pendiente (sesiones 15-16) → cierre documental (sesión 17) → cierre de los 6 CRÍTICOS originales (sesión 18) → control de versiones + eliminación definitiva del último código muerto confirmado (sesión 19, esta sesión). El proyecto queda con: el flujo operativo completo con candados de orden y de rol, historiales de consulta por rol con filtro de período, datos de ingreso para Vigilancia, generación de PDF, sin los 6 hallazgos críticos de seguridad/datos del informe original, sin la carpeta de código muerto `services/`, sin este último endpoint huérfano, y con una red de seguridad de control de versiones local (sin remoto todavía).

### 2026-08-03 (sesión 20) — 2 bugs encontrados en pruebas manuales: comentarios `{# #}` filtrándose como texto visible, y el filtro de período reseteando la pestaña activa

El usuario empezó pruebas manuales por etapas (sesión anterior) y reportó estos 2 bugs directamente, ya confirmados contra el historial de `CLAUDE.md` (sesiones 11 y 12).

**1. Notas de implementación visibles en pantalla — causa raíz confirmada antes de corregir, según lo pedido.**
No es `{% verbatim %}` (el proyecto no usa esa etiqueta en ningún template — verificado por búsqueda global) ni HTML generado fuera del motor de plantillas (ninguna de las 2 vistas arma el HTML en Python/JS; ambas pasan por `render`/`render_to_string` normal). **Causa real**: la sintaxis corta de comentario de Django, `{# ... #}`, **no soporta abarcar múltiples líneas** — es una limitación documentada del propio parser (optimización de rendimiento). Cuando el comentario se escribe en varias líneas, Django no lo reconoce como comentario en absoluto y lo renderiza literal, tal cual, como contenido de la página. Los 2 casos reportados por el usuario (`portal_proveedor.html` líneas 182-187, y `panel_compras.html` líneas 52-54) son exactamente eso: notas explicativas largas escritas como `{# texto...\n texto...\n texto... #}` en vez de `{% comment %}...{% endcomment %}` (que sí soporta multilínea) o varias líneas de `{# #}` de una sola línea cada una.

Búsqueda exhaustiva en todo el proyecto (script que recorre cada `{#`/`#}` de cada `.html` bajo `templates/` y `apps/*/templates/` y detecta si el bloque cruza un salto de línea) confirmó **9 casos** en **6 archivos**, no solo los 2 reportados: `templates/appointments/portal_proveedor.html` (2 casos), `templates/appointments/historial_listado.html` (2 casos), `apps/operations/templates/operations/panel_compras.html` (1 caso), `apps/operations/templates/operations/detalle_ticket.html` (3 casos), `templates/base/partials/sidebar.html` (1 caso, el bloque de cabecera del archivo). Los que sí usan `{% comment %}...{% endcomment %}` (`_partials/historial_tickets.html`, `partials/campo_filtro_periodo.html`) están bien y no se tocaron — esa sintaxis sí soporta multilínea correctamente.

**Fix**: se eliminaron las 9 notas multilínea de los templates (su contenido ya está documentado en las sesiones correspondientes de este archivo, así que no se pierde información). Se dejaron intactos los comentarios cortos de una sola línea (`{# ── Header KPI ── #}`, etc. — decorativos, sin bug) y los bloques `{% comment %}` existentes. Verificado con un script de re-escaneo tras el fix: **0 comentarios `{# #}` multilínea restantes en todo el proyecto**.

**2. Filtro de período en Historial resetea a la pestaña "Pendientes"/"Hoy" — corregido de forma consistente en los 3 paneles.**
Causa: el formulario de período (dentro de `_partials/historial_tickets.html`, compartido por los 3 paneles desde la Fase 9/sesión 11) hace un `GET` normal a la misma URL; al recargar la página, Bootstrap simplemente vuelve a mostrar la pestaña marcada `active` en el HTML estático (siempre la primera — "Pendientes" en Compras/Calidad, "Hoy" en Vigilancia), sin memoria de cuál estaba activa antes del envío. Lo mismo le pasaría a cualquier otro `<form>` de esas páginas (p. ej. el filtro `q`/`fecha` de Compras, que vive fuera de las pestañas pero también recarga la página).

**Fix, genérico y compartido por los 3 paneles** (nuevo archivo `static/js/panels/tabs_historial.js`, incluido en el `{% block extra_js %}` de `panel_compras.html`, `panel_vigilancia.html` y `panel_calidad.html`):
- Al cargar la página, si la URL trae `?tab=<id>` (p. ej. `tab=historial`), activa esa pestaña vía la API de Bootstrap (`new bootstrap.Tab(...).show()`), leyendo el botón por convención de ids ya existente en los 3 paneles (`tab-<id>-btn` / `tab-<id>`).
- Antes de que **cualquier** `<form>` de la página se envíe, inyecta (o actualiza) un campo oculto `name="tab"` con el id de la pestaña actualmente activa (derivado del botón `.nav-link.active` presente en el DOM) — así no hace falta tocar el HTML de cada formulario a mano ni mantener 3 copias de la lógica.
- No se tocaron las plantillas de los 3 paneles más que agregar la etiqueta `<script src=".../tabs_historial.js">`; el HTML de `_partials/historial_tickets.html` y del filtro `q`/`fecha` de Compras no necesitó cambios porque el hidden input se agrega dinámicamente por JS en cualquier `<form>` que exista en la página.

**Validación realizada** (contra el servidor real corriendo, HTTP real con cookies de sesión — mismo método de la sesión 16, no el `Client` de pruebas, para descartar cualquier diferencia con el flujo real del navegador):
- `manage.py check` limpio; `manage.py test apps.operations`: **5/5 OK** (ningún cambio de lógica de backend — ambos fixes son de templates/JS).
- Script de verificación global: `0` comentarios `{# #}` multilínea en todo `templates/` y `apps/*/templates/`.
- `panel_compras` (`ucompras`), `portal_proveedor` (`P20100055237`), `panel_vigilancia` (`uvigilancia`) y `panel_calidad` (`ucalidad`) cargados vía HTTP real con sesión autenticada: `200 OK`, título correcto, `0` ocurrencias de `{#` en el HTML renderizado, y el script `tabs_historial.js` presente en los 3 paneles operativos.
- `GET /operations/{compras,vigilancia,calidad}/?tab=historial&periodo=2026-08` responde `200` en los 3 paneles (el parámetro `tab` es ignorado tranquilamente por las vistas, como cualquier GET param no usado por el backend — la activación de la pestaña es 100% del lado del cliente).

**Fuera de alcance de esta sesión (no tocado, según lo pedido):** no se tocó `etapa_actual`, permisos, ni ningún modelo — ambos bugs eran de plantillas/JS puro. No se agregó el hidden `tab` a mano en ningún formulario específico (resuelto de forma genérica vía JS, según lo sugerido por el usuario). No se convirtió ninguna nota eliminada a `{% comment %}` — se eliminaron directamente, dado que su contenido ya vive en el historial de este archivo.

### 2026-08-04 (sesión 21) — Fase 15: "Reporte" — exportación a Excel/PDF del Historial ya filtrado

Primera pieza de "Reportería" del alcance original del proyecto, nunca construida en las 19 sesiones previas (confirmado por búsqueda global antes de empezar: cero referencias a "Fase 15" o "reporte" en `CLAUDE.md`, `INFORME_ANALISIS.md` o el código). Alcance acordado explícitamente: exportar el listado YA FILTRADO del Historial a Excel y PDF — sin dashboard ni cálculo de Lead Times (fase futura aparte).

**Librería de Excel — evaluada y confirmada con el usuario antes de instalar (mismo criterio que `xhtml2pdf` en la Fase 12):** `requirements.txt` no tenía ninguna librería de generación de Excel. Se propusieron 2 opciones (`openpyxl` vs. `xlsxwriter`) y el usuario eligió **`openpyxl==3.1.5`** — 100% Python, sin dependencias de sistema, la más usada del ecosistema Django, suficiente para un listado tabular simple (sin gráficos ni formato condicional, que sí ofrece `xlsxwriter` pero no se necesitan aquí). Instalada en `./env` y agregada a `requirements.txt` en orden alfabético.

**Diseño: un único módulo compartido, no 4 implementaciones separadas.**
- **`apps/base/reporting.py`** (nuevo) — vive en `apps.base`, no en `apps.operations`, por el mismo motivo que `apps.base.filters.resolver_periodo` (Fase 10): lo consumen tanto `apps.operations` (Ticket-based, los 3 paneles internos) como `apps.appointments` (Appointment-based, el Historial del proveedor) — evita el import cruzado entre apps de negocio.
  - `HistorialRow` (dataclass): las 6 columnas pedidas — Ticket, OC(s), Proveedor, Fecha de cita, Estado, Etapa activa — mismas que `_partials/historial_tickets.html`, sin la columna de acción "Trazabilidad" (no aplica a un archivo exportado).
  - `ticket_a_row(ticket)`: fila desde un `Ticket` (paneles internos).
  - `cita_a_row(cita)`: fila desde un `Appointment` (Historial del proveedor) — no todas las citas tienen `Ticket` todavía (`SOLICITADO`/`RECHAZADO`); en ese caso cae a un fallback basado en la cita misma (`Ticket: —`, `Estado: <cita.get_status_display>`, `Etapa activa: —`) en vez de fallar.
  - `exportar_excel(rows, filename, titulo)`: genera el `.xlsx` vía `openpyxl` (encabezado con título + fecha de generación + conteo, fila de cabecera con estilo, anchos de columna fijos), como descarga adjunta (`Content-Disposition: attachment`).
  - `exportar_pdf(rows, filename, titulo)`: mismo enfoque de plantilla standalone de `xhtml2pdf` ya usado en `cargo_ticket_pdf.html` (Fase 12) — sin Bootstrap, CSS 2.1 simple, A4 apaisado (`landscape`, más columnas que el cargo individual). Plantilla nueva `apps/base/templates/base/reporte_historial_pdf.html`.

**Reutilización de queryset — sin duplicar lógica de filtrado ni de permisos (punto 5 del pedido):**
- `apps/operations/views.py`: se extrajeron a funciones propias los querysets que cada panel ya armaba para su pestaña "Historial" — `_historial_compras_qs(request)` (filtros `q`/`fecha`/`periodo`, ya existía inline en `panel_compras`) y `_historial_por_periodo_qs(request)` (solo `periodo`, compartida por `panel_vigilancia` y `panel_calidad`, que nunca tuvieron filtro `q`/`fecha` propio). Cada vista de panel ahora llama a su helper para armar `tickets_historial`, y las 3 vistas de exportación nuevas (`exportar_historial_compras`, `exportar_historial_vigilancia`, `exportar_historial_calidad`) llaman al MISMO helper — el archivo exportado es, por construcción, el mismo queryset que la pantalla, nunca una copia divergente. Cada vista de exportación conserva el decorador de rol exacto del panel (`@compras_required`/`@vigilancia_required`/`@calidad_required`) — no se generalizó a un permiso más amplio.
- `apps/appointments/views.py`: se extrajo `_historial_citas_qs(request)` de `HistorialCitasView.get_queryset()` (filtros `q`/`status`/`sede`, sin filtro de período — confirmado en la sesión 15 que esta vista queda fuera de la Fase 10 a propósito). La nueva vista `exportar_historial_citas` (`@proveedor_required`) llama al mismo helper, que ya filtra por `user=request.user` — el proveedor solo puede exportar sus propias citas, sin ninguna comprobación de propiedad adicional (la restricción ya vivía en el queryset compartido).
- Despacho Excel/PDF compartido por los 3 paneles internos: `_exportar_tickets(tickets_qs, formato, filename, titulo)` en `apps/operations/views.py`. Un `formato` fuera de `{'excel', 'pdf'}` responde `400 Bad Request` con mensaje claro (probado con `/xml/`).

**Rutas nuevas** (mismo prefijo que cada panel, patrón `<panel>/historial/exportar/<str:formato>/`):
`operations:exportar_historial_compras`, `operations:exportar_historial_vigilancia`, `operations:exportar_historial_calidad` (`apps/operations/urls.py`); `appointments:exportar_historial_citas` (`apps/appointments/urls.py`).

**Botón "Reporte" — visible, no en menú de tres puntos (punto 2 del pedido):**
- `_partials/historial_tickets.html` (compartido por los 3 paneles desde la Fase 9): se agregó un dropdown Bootstrap (`btn-outline-success`, ícono `bi-download`, texto "Reporte") junto al formulario de filtro de período existente, con 2 ítems ("Exportar a Excel"/"Exportar a PDF"). El nombre de la URL de exportación llega como variable de contexto (`historial_export_url_name`) pasada por cada panel en su `{% include %}` — mismo partial, 3 destinos distintos, sin duplicar HTML. Cada link usa `{% url historial_export_url_name formato='excel' %}?{{ request.GET.urlencode }}` — el passthrough de TODA la querystring actual (no solo `periodo`) es lo que garantiza que el archivo exportado respete exactamente los filtros aplicados en pantalla en ese momento, incluidos `q`/`fecha` en Compras.
- `templates/appointments/historial_listado.html`: mismo patrón de dropdown, agregado junto al badge de "Registros encontrados" en la cabecera (visible sin abrir ningún menú), con el mismo passthrough de `request.GET.urlencode` (cubre `q`/`status`/`sede`).

**Validación realizada** (contra la BD real, `Client` de pruebas con `setup_test_environment()` para permitir el host `testserver`, sin escrituras — las 4 vistas de exportación son de solo lectura):
- `manage.py check` y `makemigrations --check --dry-run` limpios (no hay cambios de modelo). `manage.py test apps.operations`: **5/5 OK**.
- Los 4 endpoints de exportación responden `200` con el `Content-Type` correcto (`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` / `application/pdf`) para `ucompras`, `uvigilancia`, `ucalidad` y el proveedor `P20100055237`; firma de archivo `%PDF-1.4` verificada en el PDF.
- **Filtro respetado**: `panel_compras` sin `periodo` exporta solo el Ticket `#11` (mes actual, agosto 2026); con `?periodo=2026-04` exporta `#1` y `#2` (abril 2026) — coincide exactamente con lo que muestra la pantalla en cada caso (verificado leyendo el `.xlsx` generado con `openpyxl.load_workbook`, no solo el código de estado).
- **Permisos por rol respetados (punto 5)**: el proveedor `P20100055237` exporta únicamente sus propios 3 tickets (`#11`, `#3`, `#2`) — el Ticket `#1` (de otro proveedor, `P20100152941`) no aparece en su archivo. Un usuario `ALMACEN` que intenta acceder a `/operations/compras/historial/exportar/excel/` recibe `302` (bloqueado por `@compras_required`, no pertenece a ese grupo) — no se abrió ningún permiso nuevo.
- Formato inválido (`/xml/`) responde `400` con mensaje claro, sin intentar generar ningún archivo.

**Fuera de alcance de esta fase (confirmado explícitamente por el usuario, para una fase futura):** dashboard con cálculo de Lead Times u otras métricas agregadas; no se tocó `etapa_actual`, `_validar_etapa`, `grupo_requerido_por_etapa` ni ningún modelo — las 4 vistas nuevas son de solo lectura sobre datos ya existentes. No se agregó paginación al reporte exportado (el volumen actual de tickets no lo requiere; si crece, es una mejora aparte).

### 2026-08-04 (sesión 22) — Bug confirmado con capturas reales: `trazabilidad_ticket.html` no mostraba el QR; factorizado a partial compartido

El usuario reportó, con capturas reales del Ticket #11, que el mismo usuario (Vigilancia) veía dos resultados distintos para el mismo dato ("Datos de la Cita") según el camino de entrada:
- **"Finalizados Hoy" → "Ver trazabilidad"** (`panel_vigilancia.html`, línea 231): este botón, pese a su texto, enlaza a `operations:detalle_ticket` (no a `trazabilidad_ticket` — mismo patrón que el resto de tarjetas del Kanban "Hoy", que siempre apuntan a `detalle_ticket`). Renderiza `detalle_ticket.html`, que **siempre** mostró el QR completo (imagen + código `DYZ-11-260731-96F4AD`).
- **Pestaña "Historial" → "Ver"** (`_partials/historial_tickets.html`, Fase 5/9): este sí enlaza a `operations:trazabilidad_ticket`. Renderiza `trazabilidad_ticket.html`, cuyo bloque "Datos de la Cita" (Fase 5) **nunca tuvo el `<div id="qrcode">` ni el `<code>{{ ticket.codigo_qr }}</code>`** — solo el texto plano "Cita ID #X". Confirmado leyendo ambos templates: `detalle_ticket.html` tenía el bloque QR completo desde su creación; `trazabilidad_ticket.html` (construida en la Fase 5 reutilizando el resto del layout) se copió el `<dl>` de datos pero no el QR, y tampoco cargaba el script `new QRCode(...)` (vivía solo en el `{% block extra_js %}` de `detalle_ticket.html`, que `trazabilidad_ticket.html` nunca declaró).

**Fix — factorizado a un partial compartido (no solo igualado a mano en los 2 templates), mismo patrón que `_partials/stages_stepper.html` (Fase 5):**
- **`apps/operations/templates/operations/_partials/datos_cita.html`** (nuevo): todo el bloque "Datos de la Cita" — encabezado, `<div id="qrcode">` + `<code>{{ ticket.codigo_qr }}</code>`, el `<dl>` con Cita ID/Fecha/Proveedor/Sede/OCs — **más su propio `<script>` de renderizado** (`new QRCode(...)`, dentro del propio partial, no en un `{% block extra_js %}` separado). La librería `qrcode.min.js` ya se carga globalmente en `base.html`, así que cualquier página que incluya este partial obtiene el QR renderizado sin tener que declarar nada adicional — evita que esto vuelva a desalinearse si en el futuro se agrega un tercer punto de entrada. Parámetro esperado: `ticket` (ya presente en el contexto de ambas vistas).
- **`detalle_ticket.html`**: el bloque completo se reemplazó por `{% include "operations/_partials/datos_cita.html" %}`; se eliminó el `<script>` de `new QRCode(...)` duplicado de su `{% block extra_js %}` (ahora vive únicamente en el partial — verificado que no queda ningún duplicado, `new QRCode(` aparece exactamente 1 vez en el HTML renderizado).
- **`trazabilidad_ticket.html`**: el `<dl>` sin QR se reemplazó por el mismo `{% include %}` — no necesitó agregar ningún `{% block extra_js %}` nuevo (el script ya viaja dentro del partial).

**Validación realizada** (contra el Ticket #11 real, `Client` de pruebas, sin escrituras — ambas vistas son de solo lectura):
- `manage.py check` y `makemigrations --check --dry-run` limpios (no hay cambios de modelo). `manage.py test apps.operations`: **5/5 OK**.
- `detalle_ticket`/`trazabilidad_ticket` para el Ticket #11: ambos `200`, ambos contienen `id="qrcode"`, el script `new QRCode(` (exactamente 1 vez cada uno, sin duplicados) y el código real `DYZ-11-260731-96F4AD` — idéntico en los dos.
- **Los 4 accesos reales a `trazabilidad_ticket`** verificados con el Ticket #11: `ucompras` (Historial de Compras), `uvigilancia` (Historial de Vigilancia), `ucalidad` (Historial de Calidad) y el proveedor dueño `P20100055237` (su propio historial) — los 4 devuelven `200` con el QR y el código completos, ya no solo el texto "Cita ID #11".

**Fuera de alcance de esta sesión (no tocado, según lo pedido):** no se tocó el texto del botón "Ver trazabilidad" en `panel_vigilancia.html` (sigue enlazando a `detalle_ticket`, no a `trazabilidad_ticket` — no fue parte del pedido, que se centró en igualar el contenido de ambos templates, no en revisar a dónde apunta cada botón); no se tocó `etapa_actual`, permisos ni ningún modelo — el fix es 100% de plantillas.

### 2026-08-04 (sesión 23) — 2 bugs confirmados en `panel_calidad.html` (Ticket #12, pestaña Pendientes)

**BUG A — Cronómetro "Tiempo en planta: NaNm NaNs".**
Causa raíz confirmada (opción **b** de las 3 planteadas: formato de fecha incompatible con `new Date()`, no un timestamp ausente): `data-entrada="{{ ticket.stages.all.0.fecha_inicio|default:'' }}"` renderizaba el `DateTimeField` a través del filtro de salida por defecto de Django, que con `LANGUAGE_CODE='es-pe'` y localización activa produce texto humano (`'4 de agosto de 2026 a las 11:00'`), **no** un string ISO. `new Date('4 de agosto de 2026 a las 11:00')` devuelve `Invalid Date` en cualquier motor JS real, y `Date.now() - Invalid Date` es `NaN` — de ahí "NaNm NaNs". El dato **sí** estaba presente (se descartó la opción **a**); el problema era puramente de formato de salida.

Hallazgo adicional durante el diagnóstico: `ticket.stages.all.0` (equivalente a `ticket.stages.filter.0`, mismo patrón ya usado en el cronómetro de "En Planta ahora" de `panel_vigilancia.html`) toma la **primera fila que devuelva la BD sin `ORDER BY` explícito** — `TicketStage` no tiene `Meta.ordering`. Verificado contra el Ticket #12 real: aunque en este caso coincidía con `VIGILANCIA_ENTRADA` (la etapa correcta para "tiempo en planta"), depender de eso es frágil — Postgres no garantiza ningún orden sin `ORDER BY`.

**Fix**: `panel_calidad` (`apps/operations/views.py`) ahora anota `ticket.fecha_ingreso_planta` explícitamente — busca la etapa `VIGILANCIA_ENTRADA` por su nombre (no "la primera que haya"), sobre `ticket.stages.all()` ya prefetched (sin queries extra). El template usa `{{ ticket.fecha_ingreso_planta|date:'c'|default:'' }}` — formato `'c'` = ISO 8601 (`2026-08-04T11:00:26.153699-05:00`), verificado que `new Date(...)` lo parsea correctamente en cualquier motor JS. El script JS del cronómetro (inline en `{% block extra_js %}`) no necesitó ningún cambio — el bug estaba 100% en el dato de entrada, no en el cálculo.

**BUG B — Bloques de OC duplicados (4 en vez de 2 para el Ticket #12, que tiene 2 OCs).**
Causa raíz confirmada: `{% regroup ticket.inspections.all by po_line.purchase_order as oc_groups %}` — el tag `regroup` de Django **exige que el queryset ya venga ordenado por la clave de agrupación**; no ordena nada por sí mismo, solo agrupa filas *consecutivas* que comparten la clave. `TicketLineInspection` tampoco tiene `Meta.ordering` (solo `unique_together`), así que `ticket.inspections.all` no tenía ningún orden garantizado. Reproducido contra el Ticket #12 real: sus 6 filas de inspección (2 OCs × 3 líneas, 1 fila por etapa VIGILANCIA/ALMACEN ya ejecutada) podían llegar de la BD intercaladas por etapa en vez de por OC — cuando eso ocurre, `regroup` corta la misma OC en dos grupos no contiguos: uno con las filas de la etapa VIGILANCIA (sin ninguna fila `etapa == 'ALMACEN'`, así que el `{% if insp.etapa == 'ALMACEN' %}` interno filtra todo y la tabla queda vacía → "cabecera sin líneas") y otro, más adelante, con las filas de la etapa ALMACEN (las que sí pasan el filtro → "cabecera con líneas e inputs"). Con 2 OCs partidas así, el resultado son 4 bloques — coincide exactamente con lo reportado. No había ningún `{% include %}` duplicado del partial; la causa era 100% de orden de datos, no de estructura del template.

**Fix**: `panel_calidad` (`apps/operations/views.py`) ahora usa un `Prefetch('inspections', queryset=TicketLineInspection.objects.select_related('po_line__purchase_order').order_by('po_line__purchase_order_id', 'po_line__line_num'))` en vez de la ruta de string plano `'inspections__po_line__purchase_order'` — así `ticket.inspections.all` en el template llega **siempre** ordenado por OC y luego por línea, garantizando que `regroup` reciba datos contiguos por OC. `panel_calidad.html` no cambió su lógica de agrupación (sigue siendo el mismo `{% regroup %}` + `{% if insp.etapa == 'ALMACEN' %}`), solo se limpiaron 2 `{% with %}` muertos que no aportaban nada (`etapa_entrada`/`ocs`, variables asignadas y nunca usadas) para dejar el bloque más legible mientras se tocaba.

**Nota (auto-corregida antes de terminar la sesión):** al escribir los comentarios explicativos del fix directamente en `panel_calidad.html`, se reintrodujo por descuido el bug ya documentado en la sesión 20 (comentarios `{# ... #}` multilínea no soportados por el parser de Django, se renderizan como texto literal) — detectado de inmediato al validar (`TemplateSyntaxError: 'regroup' tag takes five arguments`, porque el propio ejemplo `{% regroup %}` citado dentro de un comentario roto se interpretó como una etiqueta real). Corregido usando `{% comment %}...{% endcomment %}` (la sintaxis que sí soporta multilínea, mismo criterio ya establecido en la sesión 20) antes de dar el fix por terminado.

**Validación realizada** (contra el Ticket #12 real — `CON_CALIDAD`, 2 OCs: 16089900 con 1 línea, 16089901 con 2 líneas —, `Client` de pruebas, sin escrituras):
- `manage.py check` y `makemigrations --check --dry-run` limpios (no hay cambios de modelo). `manage.py test apps.operations`: **5/5 OK**.
- `panel_calidad` responde `200` para `ucalidad`; `0` ocurrencias de `{#` en el HTML renderizado (confirma que no quedó ningún comentario roto).
- `data-entrada` del Ticket #12 en el HTML: `2026-08-04T11:00:26.153699-05:00` (ISO 8601 válido, antes era texto localizado no parseable).
- Exactamente **2** bloques `oc-group-card` en la página (uno por OC del Ticket #12, que es el único ticket pendiente de Calidad en este momento) — antes eran 4. Cada bloque contiene su cabecera y sus líneas juntas: **3** filas `data-inspeccion-id` en total (1 + 2, coincide con las 3 líneas reales) y **3** inputs `insp-cantidad`, ninguno huérfano fuera de su OC.

**Fuera de alcance de esta sesión (no tocado, según lo pedido — bugs "confirmados en `panel_calidad.html`" específicamente):** `panel_vigilancia.html` tiene el mismo patrón fragil para su cronómetro "En Planta ahora" (`data-entrada="{{ ticket.stages.filter.0.fecha_inicio|default:'' }}"`, mismo problema de formato localizado) — no reportado ni tocado en esta sesión, pendiente de que el usuario confirme si también lo quiere corregido. No se tocó `etapa_actual`, permisos ni ningún modelo.

### 2026-08-04 (sesión 24) — Columna "COA" mostraba "Falta" con COA real ya cargado (misma familia de bug que la Fase 1)

**Diagnóstico previo, revisando TODOS los lugares donde aparece la tabla de detalle por línea (Ítem/Descripción/Cant. SAP/Cant. Real/Estado/Observación/COA), según lo pedido:**

| Vista | Fuente de la columna COA | Estado encontrado |
|---|---|---|
| `panel_calidad.html` (pestaña Pendientes, tabla inline) | `insp.coa_url` — directo sobre `TicketLineInspection` (fila etapa='ALMACEN') | ❌ **Roto** |
| `detalle_ticket.html` → "Mi Sesión" (editable, Almacén/Calidad) | `OperationsService.get_grouped_by_oc()` | ❌ **Roto** |
| `detalle_ticket.html`/`trazabilidad_ticket.html` → "Estado Actual" (solo lectura) | `get_estado_actual_por_oc()` | ✅ OK (ya corregido sesión 9) |
| `detalle_ticket.html` → "Estado de Certificados (COA)" (pre-ingreso Vigilancia) | `get_coa_status_por_oc()` | ✅ OK (correcto desde sesión 3) |
| `panel_compras`/Historial (`_partials/historial_tickets.html`) | — | N/A, no tiene columna a nivel de línea |

**Causa raíz confirmada con datos reales del Ticket #12** (`TicketLineCOA` tenía las 2 URLs reales cargadas por el proveedor para `MP00000099` y `TEST-00123`): la fila `TicketLineInspection(etapa='ALMACEN')` de cada línea tiene `coa_url=None` en las 3 líneas — `autorizar_almacen()` (`apps/operations/services.py`) nunca incluye `coa_url` en los `defaults` de su `get_or_create`, así que ese campo queda vacío desde que la fila se crea y nunca se sincroniza (mismo hallazgo ya documentado en la sesión 9 para `get_estado_actual_por_oc`, pero esa corrección **no** alcanzó a `get_grouped_by_oc` — quedó fuera de alcance explícitamente: "vista interna de staff, no reportada como rota"). `get_grouped_by_oc` tenía un *fallback* frágil (buscar la fila `etapa='VIGILANCIA'` cruzando por `item_code`+`doc_num` en vez de por `po_line_id`) que en este caso específico sí hubiera recuperado el dato correcto, pero `panel_calidad.html` ni siquiera tenía ese fallback — leía `insp.coa_url` a secas, siempre `None`, mostrando "Falta" pese a que Vigilancia ya había validado el COA obligatorio para autorizar el ingreso (la inconsistencia que disparó el reporte del usuario).

**Fix — ambas fuentes ahora cruzan `TicketLineCOA` directo por `po_line_id`, mismo patrón que `get_estado_actual_por_oc`/`get_coa_status_por_oc` (sin fallbacks indirectos):**
- **`OperationsService.get_grouped_by_oc`** (`apps/operations/services.py`): reescrito — se eliminó el fallback por `item_code`+`doc_num` (y las líneas de `#print` de debug que quedaban, ya señaladas en `INFORME_ANALISIS.md` hallazgo BAJO #20) y se reemplazó por `coas_cargados = {coa.po_line_id: coa.coa_url for coa in TicketLineCOA.objects.filter(ticket_id=ticket_id)}`, igual que en las otras 2 funciones ya correctas. Esto corrige automáticamente `get_mi_sesion` → la pestaña "Mi Sesión" de `detalle_ticket.html` (usada por Almacén y Calidad mientras registran su inspección) sin tocar el template (la clave `coa_url` del dict no cambió, `_partials/tabla_inspeccion.html` sigue igual).
- **`panel_calidad` (vista, `apps/operations/views.py`)**: ahora anota `insp.coa_url_real` por cada inspección ya prefetched (mismo bucle que ya anotaba `ticket.fecha_ingreso_planta` en la sesión 23, sin queries extra), cruzando `TicketLineCOA.objects.filter(ticket=ticket)` por `po_line_id`. `panel_calidad.html` cambió `insp.coa_url` → `insp.coa_url_real` en la columna COA (única línea tocada en el template).

**Validación realizada** (contra el Ticket #12 real, `Client` de pruebas, sin escrituras):
- `manage.py check` y `makemigrations --check --dry-run` limpios (no hay cambios de modelo). `manage.py test apps.operations`: **5/5 OK**.
- `panel_calidad` (pestaña Pendientes) para `ucalidad`: **0** ocurrencias de "Falta"; **2** ocurrencias del dominio `sharepoint.com` (una por cada línea que `requiere_coa=True` y tiene `TicketLineCOA` real: `MP00000099` y `TEST-00123`), cada una con badge verde "OK"/ícono de archivo cargado; la línea `TEST-00124` (`requiere_coa=False`) sigue mostrando el guion neutro `—`, no afectada.
- `OperationsService.get_grouped_by_oc(12, etapa='ALMACEN')` invocado directamente: devuelve las URLs reales de SharePoint para ambas líneas correctas y `''` para la que no requiere COA — confirma el fix a nivel de servicio, no solo de template.
- `detalle_ticket` para el Ticket #12 (usuario `ucalidad`, su turno activo): contiene el dominio `sharepoint.com` y el ícono `bi-file-earmark-check` (COA cargado) en la pestaña "Mi Sesión".

**Fuera de alcance de esta sesión (no tocado, según lo pedido):** no se tocó `etapa_actual`, permisos ni ningún modelo — ambos fixes son de lectura (servicio + vista + 1 línea de template). No se consolidó `panel_calidad.html`'s tabla inline con `_partials/tabla_inspeccion.html` (seguirían siendo 2 implementaciones de tabla distintas) — no fue lo pedido, y hacerlo habría sido un refactor más grande de lo necesario para este fix puntual. Los templates confirmados como código muerto (`tickets_lista.html`, `ticket_detalle.html`, `solicitudes_lista.html` — sin ninguna vista que los referencie) no fueron revisados en detalle, consistente con el criterio ya establecido en sesiones anteriores de no tocar código muerto confirmado sin pedido explícito.

### 2026-08-04 (sesión 25) — 3 correcciones puntuales sobre hallazgos ya confirmados en el inventario de la sesión previa (sin re-investigar, solo corregir y verificar)

Los 3 puntos venían con evidencia ya reunida (inventario de deuda técnica entregado al usuario tras la sesión 24, sin tocar código en esa entrega): mismo patrón de bug ya corregido en Calidad, un link mal apuntado, y una feature de la Fase 11 ausente en una vista que ya existía.

**1. Cronómetro "En Planta ahora" en `panel_vigilancia.html` — mismo fix de la sesión 23.**
`data-entrada="{{ ticket.stages.filter.0.fecha_inicio|default:'' }}"` tenía exactamente el mismo bug que "NaNm NaNs" en Calidad: el filtro de salida por defecto de Django localiza el `DateTimeField` a texto humano en español, no ISO, y `new Date(...)` de eso da `Invalid Date`. **Fix**: `panel_vigilancia` (`apps/operations/views.py`) ahora anota `ticket.fecha_ingreso_planta` en cada ticket de `tickets_en_planta` (busca la etapa `VIGILANCIA_ENTRADA` explícita, mismo patrón que `panel_calidad`), y el template usa `{{ ticket.fecha_ingreso_planta|date:'c'|default:'' }}`. El cronómetro de "Finalizados hoy" (`ticket.tiempo_total_planta`, timedelta ya calculado en Python, sin JS) **no se tocó** — nunca tuvo este bug, tal como se pidió.
- **Validación**: ningún ticket real `EN_PLANTA` tenía su `appointment.slot.date` en la fecha de hoy (necesario para aparecer en la columna "En Planta ahora", filtrada a `appointment__slot__date=hoy`), así que se reasignó temporalmente el Ticket #12 real a un slot libre de hoy (`AppointmentSlot #38`, mismo patrón de prueba con reversión inmediata ya usado en la sesión 13), se confirmó `data-entrada="2026-08-04T11:00:26.153699-05:00"` (ISO 8601 válido) en el HTML renderizado para `uvigilancia`, y se revirtió el `appointment.slot_id` a su valor original inmediatamente después — sin dejar cambios permanentes.

**2. Link "Ver trazabilidad" en `panel_vigilancia.html` (columna "Finalizados hoy") apuntaba a `detalle_ticket`, no a `trazabilidad_ticket`.**
Corregido: `{% url 'operations:detalle_ticket' ticket.id %}` → `{% url 'operations:trazabilidad_ticket' ticket.id %}` en el botón de esa tarjeta. Esta era la única mención de esa inconsistencia en `CLAUDE.md` (sesión 22, "no fue parte del pedido, no se tocó").
- **Validación**: contra el Ticket #11 real (`FINALIZADO`, slot de hoy — el mismo usado para validar el fix del QR en la sesión 22), se confirmó que el `href` del botón ahora es `/operations/ticket/11/trazabilidad/` (antes iba a `/operations/ticket/11/`), y que la página de destino (`trazabilidad_ticket`) contiene `id="qrcode"` y el código real `DYZ-11-260731-96F4AD` — el fix de la Fase 16/sesión 22 nunca se había probado por este camino específico hasta ahora, y quedó confirmado que sí funciona también desde aquí.

**3. `TicketDatosIngreso` (placa/DNI/conductor) agregado a `trazabilidad_ticket.html`, solo lectura, sin gate de rol.**
Antes solo vivía en `detalle_ticket.html`, dentro del bloque `{% if acciones.puede_autorizar_ingreso %}` (visible únicamente para Vigilancia, y solo mientras el ticket sigue `PROGRAMADO`). Se **factorizó** a `apps/operations/templates/operations/_partials/datos_ingreso.html` (mismo criterio que `_partials/datos_cita.html` de la sesión 22 — única fuente de verdad, para no desalinearse si se agrega un tercer punto de entrada en el futuro): mismo `<dl>`, mismo criterio "No completado por el proveedor" si el registro no existe o el campo específico está vacío (Fase 11, sin cambios de lógica). `detalle_ticket.html` ahora incluye el partial en el mismo lugar donde antes tenía el bloque inline (sin cambio de comportamiento — mismo gate de `puede_autorizar_ingreso`, verificado sin regresión). `trazabilidad_ticket.html` agrega una tarjeta nueva con el mismo partial, **siempre visible** (sin condición de rol ni de estado), entre "Datos de la Cita" y "Trazabilidad de Etapas" — visible para Compras/Vigilancia/Calidad y el proveedor dueño, igual que el bloque de QR. La vista `trazabilidad_ticket` agrega `'datos_ingreso'` al `select_related` (evita una query extra por página).
- **Validación**: contra el Ticket #12 real (que sí tiene `TicketDatosIngreso`: placa `F5J-011`, DNI `40243876`, nombre `CARLOS LOZANO ARANCIAGA`) — `ucompras`, `uvigilancia`, `ucalidad` y el proveedor dueño real (`P20266614803`, no `P20100055237` como se probó por error al principio y corrigió antes de reportar) ven los 3 datos reales en `trazabilidad_ticket`. Contra el Ticket #11 (sin `TicketDatosIngreso`): aparece "No completado por el proveedor" exactamente **3 veces**, no se omite en silencio.

**Validación general realizada** (contra datos reales, `Client` de pruebas, cambios temporales revertidos donde aplicó):
- `manage.py check` y `makemigrations --check --dry-run` limpios (no hay cambios de modelo). `manage.py test apps.operations`: **5/5 OK**.
- `detalle_ticket` para el Ticket #12 (con `estado` temporalmente forzado a `PROGRAMADO` y revertido de inmediato): el partial `datos_ingreso.html` sigue renderizando igual que antes de factorizarlo — sin regresión.

**Fuera de alcance de esta sesión (no tocado, según lo pedido):** no se tocó `etapa_actual`, `_validar_etapa`, `grupo_requerido_por_etapa` ni ningún permiso — los 3 fixes son de lectura (2 de plantillas/vista, 1 de link). No se revisaron los otros puntos del inventario de deuda técnica entregado tras la sesión 24 (bug de login histórico, `TicketDatosIngreso` ausente en otros historiales, naming de otros botones) — quedan pendientes de que el usuario decida cuáles cerrar.

### 2026-08-04 (sesión 26) — Parte A: cierre de raíz del patrón `{# #}` multilínea + regla nueva. Parte B: bloque "Datos del Vehículo / Conductor" faltante en `detalle_ticket.html`

**PARTE A — Punto 1: causa raíz exacta determinada en la sesión 20 (no una simple limpieza a mano).**
La sesión 20 sí llegó a una causa raíz técnica concreta, no solo borró texto: la sintaxis corta de comentario de Django, `{# ... #}`, no soporta contenido que cruce un salto de línea, porque el tokenizer del motor de plantillas (`django/template/base.py`, línea 92) usa `tag_re = re.compile(r"({%.*?%}|{{.*?}}|{#.*?#})")` **sin la flag `re.DOTALL`** — el `.` de esa expresión regular no matchea `\n`. Cuando el contenido entre `{#` y `#}` incluye un salto de línea, la regex nunca encuentra el cierre, el lexer no reconoce ningún token de comentario ahí, y el bloque completo cae como `TOKEN_TEXT` literal: se renderiza tal cual en la página. Esta sesión (26) **re-verificó esa causa directamente contra el código fuente de Django instalado en el `venv`** (`env/Lib/site-packages/django/template/base.py:92`), no solo se confió en lo ya escrito en la sesión 20 — confirmado byte a byte, la explicación de la sesión 20 era correcta y completa.

**Punto 2 y 3 — Barrido completo del proyecto.**
Script Python (mismo enfoque que la sesión 20, ahora archivado como referencia): recorre todo `.html` bajo cualquier directorio `templates/` del proyecto, extrae cada `{# ... #}` con `re.DOTALL` (para encontrar el comentario tal como el autor lo escribió, no como Django lo parsea) y reporta los que contienen `\n` en su interior. **2 instancias encontradas**, ambas nuevas, ambas introducidas en la sesión 25 — ninguna preexistente sobrevivió desde la sesión 20:

| Archivo | Línea | Contenido (truncado) |
|---|---|---|
| `apps/operations/templates/operations/panel_vigilancia.html` | 180 | *"Cronómetro de tiempo en planta (sesión 25: mismo fix de la sesión 23 — formato ISO 8601 vía `\|date:'c'`..."* |
| `apps/operations/templates/operations/trazabilidad_ticket.html` | 57 | *"Datos del Vehículo / Conductor (Fase 11): solo lectura, sin gate de rol — visible para Compras/Vigilancia/Calidad..."* |

Ninguna de las dos estaba dentro de `{% verbatim %}` (confirmado de nuevo: `grep verbatim` en todo `templates/` — cero resultados, igual que en la sesión 20) ni dentro de ningún `{% include %}` que "rompiera el contexto" — Django tokeniza cada archivo de plantilla de forma independiente sin importar cuántos `{% include %}` lo envuelvan, así que esa hipótesis del mecanismo no aplica aquí ni en ningún caso: la única causa, en ambas instancias, es la misma regex sin `re.DOTALL` de la sesión 20. **Verificado empíricamente antes de corregir** (no solo por lectura de código): la de `panel_vigilancia.html` solo se filtra cuando hay al menos un ticket real en la columna "En Planta ahora" (el bloque roto vive dentro de ese `{% for %}`) — reproducido reasignando temporalmente el Ticket #12 a un slot de hoy; la de `trazabilidad_ticket.html` se filtraba siempre, confirmado contra el Ticket #12 real (texto roto presente en el HTML).

**Punto 4 — Eliminadas las 2 instancias** (su contenido ya vive en `CLAUDE.md`, sesión 25). Re-escaneo posterior con el mismo script: **0 instancias multilínea en todo el proyecto**.

**Punto 5 — Regla nueva agregada al inicio de `CLAUDE.md`** (sección "Reglas de esta sesión en adelante", antes de "Descripción del negocio" — la primera sección visible del archivo): las notas de implementación no van como comentario de ningún tipo dentro de un `.html`, solo en `CLAUDE.md`/chat; y antes de cerrar cualquier sesión que haya tocado `.html`, correr el barrido de `{#` multilínea sobre los archivos tocados como último chequeo — no asumir que "esta vez se escribió bien" (la sesión 25 asumió justamente eso, y fue la causa de esta reincidencia).

---

**PARTE B — "Datos del Vehículo / Conductor" faltante en `detalle_ticket.html`.**

Diagnóstico confirmado: el bloque **sí** existía en `detalle_ticket.html` desde la sesión 25, pero anidado dentro de `{% if acciones.puede_autorizar_ingreso %}` (`es_vigilancia and ticket.estado == 'PROGRAMADO'`) — visible solo para Vigilancia, y solo mientras el ticket sigue `PROGRAMADO`. Para el Ticket #12 (`EN_PLANTA`), visto por `ALMACEN` vía Panel Almacén → Confirmadas → "Ver Ticket", ese `if` es falso, así que el bloque completo no se renderizaba — exactamente el síntoma reportado con captura real, mientras que `trazabilidad_ticket.html` (agregado en la misma sesión 25, sin ningún gate) sí lo mostraba para el mismo ticket.

**Fix — mismo criterio ya aplicado al bloque de QR en la sesión 22 (factorizar, no duplicar a mano):**
- Se quitó el `{% include "operations/_partials/datos_ingreso.html" %}` que vivía dentro de la tarjeta de acción "Autorizar Ingreso a Planta" (evita mostrarlo dos veces cuando Vigilancia sí tiene el turno pendiente).
- Se agregó una tarjeta nueva, siempre visible, en la **columna izquierda** de `detalle_ticket.html` (mismo lugar que en `trazabilidad_ticket.html`: entre "Datos de la Cita" y "Trazabilidad de Etapas"), con el mismo `{% include "operations/_partials/datos_ingreso.html" %}` — sin condición de rol ni de estado, visible para cualquiera de los 4 roles que ya acceden a `detalle_ticket` (Almacén/Calidad/Vigilancia/Compras) y el proveedor dueño.
- El partial `_partials/datos_ingreso.html` (creado en la sesión 25) ya era la única fuente de verdad para ambos templates — no hubo que crear ningún partial nuevo, solo mover su punto de inclusión en `detalle_ticket.html` fuera del gate de acción.

**Validación realizada** (contra el Ticket #12 real — placa `F5J-011`, DNI `40243876`, nombre `CARLOS LOZANO ARANCIAGA` —, `Client` de pruebas):
- `manage.py check` y `makemigrations --check --dry-run` limpios (no hay cambios de modelo). `manage.py test apps.operations`: **5/5 OK**.
- `detalle_ticket` para el Ticket #12 con `ualmacen` (el caso exacto reportado): ahora muestra los 3 datos reales — antes no mostraba nada.
- `detalle_ticket` con `ucalidad` (otro rol que antes tampoco veía el bloque, al no ser Vigilancia): también muestra los 3 datos.
- **Sin duplicación**: se forzó temporalmente el Ticket #12 a `estado='PROGRAMADO'` (mismo patrón de prueba con reversión inmediata de la sesión 13) para simular el caso en que Vigilancia SÍ tiene la acción "Autorizar Ingreso" pendiente — el texto "Datos del Veh" y la placa `F5J-011` aparecen **exactamente 1 vez** cada uno, no 2; ticket revertido a `EN_PLANTA` de inmediato.
- `trazabilidad_ticket` para el mismo ticket (vía `ucompras`) sigue mostrando los mismos 3 datos, sin cambios — mismos datos, mismo lugar en el layout, en ambas vistas.

**Validación general de la sesión** (Partes A y B): `manage.py check`, `makemigrations --check --dry-run` y `manage.py test apps.operations` (**5/5 OK**) corridos una vez más al final, después de todos los cambios; barrido de `{#` multilínea repetido por última vez: **0 instancias**.

**Fuera de alcance de esta sesión (no tocado, según lo pedido):** no se tocó `etapa_actual`, permisos ni ningún modelo — todos los cambios son de plantillas (eliminación de comentarios rotos + reubicación de un `{% include %}` ya existente). No se revisaron los demás puntos del inventario de deuda técnica de la sesión 24 (siguen pendientes de que el usuario decida cuáles cerrar).

### 2026-08-05 (sesión 27) — Rediseño de flujo "Materia Prima": análisis de impacto, decisiones de diseño, reset de datos de prueba y Fase 1 (modelo)

**Contexto:** el usuario planteó un cambio de flujo importante — introducir un rol/grupo Django nuevo, **MATERIA_PRIMA**, paralelo a `ALMACEN`, para que la recepción física del ticket la ejecute uno u otro según el tipo de OC (materia prima vs. comercial), reemplazando la clasificación automática `tipo_flujo` (calculada hoy en `confirmar_cita`, antes de que Vigilancia siquiera actúe) por una decisión operativa explícita tomada al "Iniciar Recepción". Antes de escribir código, se pidió un análisis de impacto completo (sin tocar nada), luego una ronda de decisiones de diseño, y recién con eso confirmado, ejecutar el reset de datos + la primera fase de modelo. **Por pedido explícito del usuario, no se documentó nada de esto en `CLAUDE.md` hasta ahora** (para no dejar registrada una versión del diseño que pudiera cambiar en la revisión) — a partir de esta sesión, `CLAUDE.md` se actualiza siempre al cierre de cada sesión (ver regla nueva al inicio del archivo).

**Análisis de impacto (solo lectura, sin cambios de código) — hallazgo más importante:** se verificó contra los tickets reales que la premisa "las OCs de un Ticket son siempre del mismo tipo (nunca mezcladas MP/no-MP)" era **falsa**: 3 de los 6 tickets existentes (`#1`, `#12`, `#13`) mezclaban OCs `MP` y `CDL` en la misma cita, porque `confirmar_cita()` calcula `tipo_flujo='CON_CALIDAD'` con un `any()` sobre todas las OCs vinculadas (basta una MP para arrastrar a todas). Se mapearon además: los ~10 puntos de lectura de `tipo_flujo` en `services.py`/`views.py`/templates; la estructura de `Ticket.etapa_actual` y `grupo_requerido_por_etapa` (el punto exacto de bifurcación es la línea fija `VIGILANCIA_INGRESO → 'ALMACEN'`, hoy sin condición); que el campo confiable de tipo de OC es `PurchaseOrder.u_mss_tdb`/`es_materia_prima` (sincronizado desde el UDF de SAP, confirmado 100% poblado en los datos reales — no confundir con `PurchaseOrderLine.requiere_coa`, que es un flag ajustable por Compras, no un identificador de tipo); que `panel_almacen.html` (el panel real que usa hoy el grupo ALMACEN, no código muerto) **nunca recibió Historial ni Reporte** (a diferencia de Compras/Vigilancia/Calidad); y que el campo "muelle" ya existe hoy a medias (`AppointmentSlot.dock` + parámetro `muelle` de `autorizar_almacen`), pero **sin ningún input real en la UI** que lo capture (`ticket_actions.js` solo envía `ticket_id`).

**Decisiones de diseño confirmadas por el usuario** (a partir del análisis, no cuestionadas):
1. El bloqueo de "no mezclar OC MP con no-MP en la misma solicitud" se implementará en el portal del proveedor (`apps.appointments`), al solicitar la cita — no en `confirmar_cita`. *(Pendiente de implementar, fase futura.)*
2. `muelle` se captura en `Ticket` (campo nuevo), no reutilizando `AppointmentSlot.dock`.
3. `tipo_flujo` se reemplaza por 2 señales independientes: el tipo de OC (ya existente, `u_mss_tdb`/`es_materia_prima`) para el ruteo ALMACEN vs. MATERIA_PRIMA, y un campo nuevo `Ticket.requiere_calidad` (booleano, capturado al "Iniciar Recepción") para decidir si pasa a CALIDAD.
4. `TicketLineInspection.etapa_actual` mantiene un solo valor `'ALMACEN'` para "recepción en curso", sin importar si la ejecuta el grupo ALMACEN o MATERIA_PRIMA — evita tocar `TicketLineInspection.ETAPA_CHOICES` y todos sus consumidores (`get_grouped_by_oc`, `registrar_calidad`, `ajax_registrar_inspeccion`).
5. Esta tanda de fases agrega también Historial + Reporte al panel de Almacén (cerrando la brecha encontrada en el análisis), en paralelo con el panel nuevo de Materia Prima.
6. Sobre el default del checkbox "Solicitar Inspección Calidad": confirmado como razonable que Materia Prima lo traiga marcado (con confirmación explícita) y Almacén desmarcado — coincide con el comportamiento histórico (SOLO_ALMACEN era la ruta por defecto para OCs comerciales); no es una restricción dura, sigue siendo editable por quien ejecuta la acción.

**Reset de datos de prueba (ejecutado, con confirmación explícita del usuario sobre el alcance):**
- Verificado antes de tocar nada que la conexión activa de Django apunta a `daryza_portal_db`/`127.0.0.1:5432` — confirmado además directamente contra Postgres (`SELECT current_database()`), no solo contra la config de Django.
- Conteos mostrados antes del borrado: 6 `Appointment`, 6 `Ticket`, 19 `TicketStage`, 117 `TicketLineInspection`, 5 `TicketLineCOA`, 2 `TicketDatosIngreso`.
- Ejecutado dentro de `transaction.atomic()`: `Appointment.objects.all().delete()` (aprovechando que `Ticket.appointment` es `CASCADE`, y las 4 tablas del ciclo son `CASCADE` sobre `Ticket` — una sola operación cascada automáticamente todo) — 164 filas borradas en total (incluida la tabla intermedia M2M `appointments_appointment_purchase_orders`, 9 filas). Aserciones de post-condición (conteos en cero, y `AppointmentSlot`/`PurchaseOrder`/`PurchaseOrderLine` sin cambios) verificadas **dentro** del bloque atómico antes de dejar hacer commit.
- `AppointmentSlot` (51) y `PurchaseOrder`/`PurchaseOrderLine` (9/42, incluidas las 5 OCs reales sincronizadas de SAP y las 4 de prueba QA) quedaron intactos, tal como se decidió — `PurchaseOrderLine.po_line` en `TicketLineInspection` es `PROTECT`, así que nunca estuvieron en riesgo de cascada aunque se hubiera querido.
- Se corrigieron también los 2 `AppointmentSlot` con `is_full_override=True` inconsistente (`#37`, con solo 1/2 cupo real ocupado; `#41`, con 0 citas) → `False`.
- Verificado tras el commit: relectura fresca confirma 0 en las 6 tablas del ciclo, 51/9/42 sin cambios en las demás, y las 9 OCs libres de nuevo (`Appointment.objects.filter(purchase_orders=po).exists()` → `False` para las 9).

**Fase 1 (modelo) — implementada:**
- `Ticket.muelle` (`CharField`, `max_length=50`, `blank=True`, `default=''`) y `Ticket.requiere_calidad` (`BooleanField`, `default=False`) agregados en `apps/operations/models.py`, con `help_text` explicando su rol frente a `tipo_flujo` (que **no se toca ni se elimina** en esta fase — sigue siendo la fuente real hasta que las fases de servicio/vistas migren sus ~10 puntos de lectura).
- Migración `apps/operations/migrations/0007_ticket_muelle_ticket_requiere_calidad.py`: un solo archivo, 2 `AddField`, **sin `RunPython`** (no hacía falta backfill — la tabla quedó vacía tras el reset, que se ejecutó como prerrequisito explícito de esta fase, justo para evitar el problema de traducir los 3 tickets mixtos a un valor único de `requiere_calidad`).

**Validación realizada:**
- `makemigrations --check --dry-run` antes de generar la migración: confirmó exactamente los 2 campos esperados, nada más.
- `migrate operations`: aplicada sin errores. `makemigrations --check --dry-run` después: limpio. `manage.py check`: limpio. `manage.py test apps.operations`: **5/5 OK** (sin cambios de lógica de servicio/vista todavía, esperado).
- Defaults verificados en dos niveles: por ORM (instancia `Ticket()` en memoria, sin guardar — `muelle=''`, `requiere_calidad=False`, `tipo_flujo`/`etapa_actual` sin cambios) y por columna real en Postgres (`information_schema.columns` sobre `operations_ticket`: `muelle` varchar NOT NULL, `requiere_calidad` boolean NOT NULL, `tipo_flujo` intacto).

**Fuera de alcance de esta sesión (confirmado explícitamente por el usuario, para fases posteriores):** no se tocó `tipo_flujo`, `services.py`, `views.py`, templates, permisos (`apps/base/decorators.py`), `redirect_by_role`, ni la bifurcación de `grupo_requerido_por_etapa`. El bloqueo de OCs mixtas en el portal del proveedor (decisión 1) tampoco se implementó todavía — sigue pendiente. Grupo Django `MATERIA_PRIMA`, panel nuevo, e Historial/Reporte para Almacén y Materia Prima: fases futuras, según el plan de ~6-8 sesiones propuesto en el análisis.

### 2026-08-06 (sesión 28) — Decisión 1 del rediseño Materia Prima: bloqueo de OCs mixtas (MP + comercial) en el portal del proveedor

Implementa la decisión 1 confirmada en la sesión 27 ("el bloqueo de no mezclar OC tipo MP con OC no-MP se implementa en el portal del proveedor, al solicitar la cita — no en `confirmar_cita`"). Alcance explícitamente acotado por el usuario: solo `apps.appointments`, nada de `apps.operations`.

**Investigación previa (sin tocar código), según lo pedido:**
- Flujo completo mapeado: picker `templates/appointments/modal_agendar.html` (checkboxes `.oc-checkbox`, uno por OC en `ocs_pendientes`, ya mostraba el badge MP/Comercial vía `oc.u_mss_tdb`) → JS `static/js/panels/portal_proveedor.js::enviarSolicitud()` (recolecta `.oc-checkbox:checked`, arma `oc_ids` en el `FormData`) → vista `apps/appointments/views.py::solicitar_cita_ajax` (wrapper delgado) → `AppointmentService.solicitar_cita_borrador` (`apps/appointments/services.py:55`), donde ya vivía la única validación existente (OC ya en uso por otra cita activa).
- Cómo se listan las OCs hoy: `PortalProveedorView.get_context_data` filtra solo por `card_code=ruc_proveedor` y `status__in=['O','PENDIENTE']` — **sin excluir OCs ya atadas a una cita activa** (esa validación solo corre en el backend al enviar, no en el listado). Confirmado también que **no existía ningún deshabilitado dinámico en el picker por ningún motivo previo** — era una lista estática de checkboxes.

**Capa A — UX proactiva (`templates/appointments/modal_agendar.html` + `static/js/panels/portal_proveedor.js`):**
- Cada checkbox `.oc-checkbox` ahora trae `data-tipo="MP"` o `data-tipo="COM"` (derivado de `oc.u_mss_tdb` en el template, mismo criterio que el badge que ya se mostraba).
- Nueva función `actualizarDisponibilidadOCs()`, enganchada al evento `change` de cada checkbox: si hay al menos una marcada, deshabilita (`disabled=true` + clase `opacity-50` en el `<label>`) todas las del tipo contrario al de la primera marcada; si se desmarcan todas, quita el deshabilitado de todas. Es solo UX — no reemplaza la validación real.

**Capa B — validación dura (`apps/appointments/services.py::AppointmentService.solicitar_cita_borrador`):**
- Nuevo paso "2b", inmediatamente después de la validación existente de "OC ya en uso" y antes de crear el `Appointment` (dentro del mismo `transaction.atomic()`, así que si falla no se guarda nada): `tipos_seleccionados = {po.es_materia_prima for po in PurchaseOrder.objects.filter(id__in=oc_ids)}` — si `len(tipos_seleccionados) > 1` (hay tanto `True` como `False` entre las OCs seleccionadas), lanza `ValidationError("No se puede combinar Materia Prima con OC comerciales en la misma solicitud.")`. Usa la property `es_materia_prima` (booleano derivado de `u_mss_tdb`), no el string crudo, mismo criterio que el resto del código.

**Validación realizada** (contra la BD real, `Client` de pruebas, usando el proveedor QA `P20266614803` que tiene 2 OCs MP y 2 comerciales libres tras el reset de la sesión 27; sin dejar datos permanentes — los `Appointment` de prueba se eliminaron al cerrar):
- `manage.py check` y `makemigrations --check --dry-run` limpios (no hay cambios de modelo, como se esperaba). `manage.py test apps.operations`: **5/5 OK**.
- Picker renderizado: los 4 `data-tipo` (`MP`/`COM`/`MP`/`COM`) aparecen correctos en el HTML, alineados con el tipo real de cada OC.
- **Combinación MP + MP**: `POST /appointments/api/solicitar-cita/` con 2 OCs Materia Prima → `200`, `Appointment` creado — sigue funcionando sin cambios.
- **Combinación comercial + comercial**: mismo resultado, `200`, `Appointment` creado — sigue funcionando sin cambios.
- **Combinación mixta (MP + comercial)**: `400`, `{'status': 'error', 'msg': 'No se puede combinar Materia Prima con OC comerciales en la misma solicitud.'}` — mensaje exacto, y se confirmó por conteo (`Appointment.objects.count()` antes/después) que **no se creó ningún registro**.
- Capa A (deshabilitado dinámico en el picker) verificada por revisión de código, no de forma interactiva en navegador — la extensión de Chrome no estaba conectada en este entorno en el momento de la validación. La lógica es autocontenida y de bajo riesgo (un solo `change` listener, sin estado async), pero queda pendiente una verificación visual en vivo si el usuario quiere confirmarla manualmente.
- Sin residuos al cerrar: `Appointment.objects.count()` en `0`, las 9 OCs (incluidas las QA) confirmadas libres de nuevo.
- Barrido de `{#` multilínea (regla permanente desde la sesión 26): **0 instancias** en los archivos tocados.

**Fuera de alcance de esta sesión (confirmado explícitamente por el usuario):** `confirmar_cita`, `tipo_flujo`, `grupo_requerido_por_etapa`, y cualquier cosa de `apps.operations` — sin tocar. El resto del plan de fases del rediseño Materia Prima (grupo Django, panel nuevo, bifurcación de `autorizar_almacen`, Historial/Reporte de Almacén) sigue pendiente.

### 2026-08-06 (sesión 29) — Rediseño Materia Prima: Fase 2, grupo Django + panel de aterrizaje mínimo

Implementa la Fase 2 del plan estimado en la sesión 27: el objetivo explícito es que una cuenta del grupo `MATERIA_PRIMA` pueda loguearse y aterrizar en un panel propio — sin tocar todavía `grupo_requerido_por_etapa`, `autorizar_almacen` ni el modelo `Ticket` (eso es la Fase 4).

**Grupo Django `MATERIA_PRIMA`**: creado directamente contra la BD real (`Group.objects.get_or_create`), mismo mecanismo que los 5 grupos existentes (`ALMACEN`/`CALIDAD`/`VIGILANCIA`/`COMPRAS`/`PROVEEDORES`) — ninguno de ellos está creado por migración ni fixture en el código (confirmado por búsqueda: `0001_initial.py` solo usa `'ALMACEN'` como string de choice, no crea el `Group` real), así que no se introdujo un mecanismo nuevo para este.

**Permisos (`apps/base/decorators.py`)**: nuevo `materia_prima_required = user_passes_test(en_grupo('MATERIA_PRIMA'), ...)`, mismo patrón que los 5 decoradores existentes. `'MATERIA_PRIMA'` agregado a las 2 listas combinadas identificadas en el análisis de la sesión 27: `es_staff_interno` (protege `ajax_registrar_inspeccion`) y `es_staff_o_proveedor` (protege `detalle_ticket`/`trazabilidad_ticket`) — verificado por invocación directa de ambos predicados contra el usuario de prueba, `True` en los dos.

**`redirect_by_role`** (`apps/base/views.py`): nueva rama `elif user.groups.filter(name='MATERIA_PRIMA').exists(): return redirect('operations:panel_materia_prima')`, en el mismo lugar y con el mismo patrón que las 4 ramas existentes, antes del fallback a `/admin/`.

**Panel nuevo mínimo**:
- `apps/operations/views.py::panel_materia_prima` (`@materia_prima_required`): renderiza el template sin ningún queryset de tickets — a propósito, esa lógica depende de la bifurcación de `grupo_requerido_por_etapa` que todavía no existe (Fase 4).
- Ruta `operations:panel_materia_prima` → `/operations/materia-prima/` (`apps/operations/urls.py`), en su propia sección junto a Panel Almacén.
- Template nuevo `apps/operations/templates/operations/panel_materia_prima.html`: header + una tarjeta de placeholder explicando que la recepción llega en una fase posterior — mismo patrón visual (`page-header`, `dz-card`) que el resto de paneles, sin ningún `{# #}` multilínea (verificado con el barrido de la sesión 26).

**Adicional, no pedido explícitamente pero necesario para que el aterrizaje sea usable** (mismo criterio que los "ripple fixes" de sesiones anteriores — documentado, no oculto): el sidebar (`templates/base/partials/sidebar.html`) tiene un `{% if grupo == ... %}` por cada rol para la franja de color y el menú lateral; sin una rama para `MATERIA_PRIMA` habría caído al `{% else %}` (color de proveedor) y sin ninguna sección de navegación. Se agregó: una 6ª rama en las 3 cadenas `if/elif` de color (franja superior, punto y label del grupo) y una sección nueva "Recepción Materia Prima" con un único link al panel — mismo patrón que la sección de Almacén, sin el link a `tickets_lista` (ya señalado como posible código muerto en análisis previos, no se replicó para el panel nuevo). Color nuevo `--group-materia-prima` (`#ec4899`, rosa — el único tono libre entre los 5 ya usados) agregado a `static/css/vbs-design-system.css` junto con su variante `-dim` y la clase de badge `.group-MATERIA_PRIMA`, seguiendo exactamente el patrón de los 5 grupos existentes.

**Cuenta de prueba**: `umateriaprima`, contraseña `Prueba2026!` (mismo estándar de todas las cuentas de prueba desde la sesión 7b), grupo `MATERIA_PRIMA` únicamente.

**Validación realizada** (contra la BD real, `Client` de pruebas):
- `manage.py check` y `makemigrations --check --dry-run` limpios (sin cambios de modelo, tal como se pidió). `manage.py test apps.operations`: **5/5 OK**.
- Login real de `umateriaprima` + `GET /home/` con `follow=True`: redirige a `/operations/materia-prima/` (`302` → `200`), el HTML renderizado contiene "Panel Materia Prima".
- `ualmacen` y `ucalidad` intentando acceder directo a `/operations/materia-prima/`: **`302`** (bloqueados, redirigidos a login) — el decorador nuevo no abre el panel a otros roles.
- `es_staff_interno(umateriaprima)` y `es_staff_o_proveedor(umateriaprima)`: `True` en ambos casos, confirmando que el grupo nuevo ya puede alcanzar `detalle_ticket`/`trazabilidad_ticket`/`ajax_registrar_inspeccion` cuando exista lógica que lo requiera (aunque esa lógica en sí — la bifurcación de a quién le toca el turno — sigue sin existir hasta la Fase 4).
- Barrido de `{#` multilínea: **0 instancias** en los archivos tocados.

**Fuera de alcance de esta sesión (confirmado explícitamente por el usuario, para la Fase 4):** no se tocó `grupo_requerido_por_etapa`, `autorizar_almacen`, `ajax_autorizar_almacen`, `puede_autorizar_almacen` en `detalle_ticket`, ni el modelo `Ticket`. El panel no lista todavía ningún ticket pendiente — no hay ninguna forma determinística de saber "qué le toca a Materia Prima" hasta que esa bifurcación exista. No se agregó Historial/Reporte al panel nuevo (ni se cerró la misma brecha para Almacén, señalada en el análisis de la sesión 27) — queda para una fase posterior, según lo estimado.

### 2026-08-06 (sesión 30) — Fase 3+4 combinadas: bifurcación ALMACEN/MATERIA_PRIMA en "Iniciar Recepción" (solo backend)

Con el grupo `MATERIA_PRIMA` y el panel de aterrizaje ya en pie (Fase 2, sesión 29), y `Ticket.muelle`/`Ticket.requiere_calidad` ya creados (Fase 1, sesión 27), esta sesión implementó la bifurcación real de a quién le toca ejecutar la recepción física del ticket, según el pedido explícito del usuario (4 puntos numerados) — **solo backend**, sin tocar templates/JS (Fase 5) ni Historial/Reporte (Fase 6).

**1. `OperationsService.grupo_requerido_por_etapa` bifurcado (`apps/operations/services.py`):**
- Nuevo helper `_grupo_actor_recepcion(ticket)`: devuelve `'MATERIA_PRIMA'` si `ticket.appointment.purchase_orders.filter(u_mss_tdb='MP').exists()`, si no `'ALMACEN'`. Determinístico porque, desde la Decisión 1 (sesión 28), el portal del proveedor ya bloquea mezclar OCs MP y comerciales en una misma solicitud — no hay tickets con OCs mixtas.
- El `mapa` interno de `grupo_requerido_por_etapa` cambia en 2 puntos: `ETAPA_VIGILANCIA_INGRESO` ya no es fijo `'ALMACEN'`, ahora llama `_grupo_actor_recepcion(ticket)` (pedido explícito, punto 1). Y `ETAPA_ALMACEN` (el siguiente turno, "¿a quién le toca cerrar/pasar a Calidad?") pasa de `'CALIDAD' if tipo_flujo == 'CON_CALIDAD' else 'ALMACEN'` a `'CALIDAD' if ticket.requiere_calidad else _grupo_actor_recepcion(ticket)` — cambio no pedido explícitamente pero inevitable: si `registrar_calidad` (punto 3d) pasa a bifurcar sobre `requiere_calidad`, el candado de permiso que valida "a quién le toca" debe leer exactamente la misma señal, o quedarían desalineados (un actor que el candado bloquea aunque el servicio lo aceptaría, o viceversa).

**2. `TicketLineInspection.etapa` se mantiene `'ALMACEN'` fijo** (punto 2, confirmado): ningún cambio en `ETAPA_CHOICES` ni en el `get_or_create` de `autorizar_almacen` — la etapa de recepción se sigue escribiendo con ese único valor sin importar si el actor real es `ALMACEN` o `MATERIA_PRIMA`.

**3. `OperationsService.autorizar_almacen` ("Iniciar Recepción") reescrito** (mismo nombre de método, ver justificación de endpoint más abajo):
- Nueva firma: `autorizar_almacen(ticket_id, usuario, muelle='', requiere_calidad=None, confirmado=False)` (antes `usuario_almacen`, sin los 2 últimos parámetros).
- `es_materia_prima_actor = (_grupo_actor_recepcion(ticket) == 'MATERIA_PRIMA')`.
- Si `requiere_calidad is None`: default `= es_materia_prima_actor` (marcado para Materia Prima, desmarcado para Almacén — punto 3c).
- Si `es_materia_prima_actor and not confirmado`: `raise ValidationError(...)` **antes de cualquier escritura** — rechaza con mensaje claro, sin tocar `TicketStage`/`TicketLineInspection`/`Ticket` (punto 3c, gate de confirmación explícita solo para Materia Prima).
- `muelle` ahora se escribe en `ticket.muelle` (campo de la Fase 1), **ya no** en `AppointmentSlot.dock` — corrige el único punto del método que todavía tocaba el slot compartido, según la decisión de diseño original de la Fase 1 ("no reutiliza `AppointmentSlot.dock`") que había quedado sin aplicar en este método concreto.
- El resto del método (cierre de `VIGILANCIA_ENTRADA`, apertura de `ALMACEN_RECEPCION`, creación de las filas `TicketLineInspection` etapa=`ALMACEN`) se mantiene sin cambios de lógica, solo renombrando `usuario_almacen`→`usuario`.
- `ticket.save(update_fields=['muelle', 'requiere_calidad', 'etapa_actual'])`.

**3d. `registrar_calidad` y `registrar_salida` actualizados para leer `requiere_calidad` en vez de `tipo_flujo`** (consecuencia directa de 3d, no opcional — ver razonamiento del punto 1): `es_mp = ticket.requiere_calidad` (antes `ticket.tipo_flujo == 'CON_CALIDAD'`) en `registrar_calidad`; `etapa_esperada` y la rama de qué `TicketStage` cerrar (`CALIDAD_INSPECCION` vs. `ALMACEN_FINALIZADO`) en `registrar_salida` cambian del mismo modo. **No se tocó** `iniciar_ingreso_planta` (el chequeo de COA obligatorio) ni `calcular_coa_completo` — ambos siguen en `tipo_flujo` a propósito: conciernen el requisito de COA en el ingreso (previo y ajeno a la decisión de Calidad que se toma después, en "Iniciar Recepción"). Tampoco se tocó `confirmar_cita` (`apps/appointments/services.py`), que sigue fijando `tipo_flujo` sin cambios — sigue existiendo como señal legacy, ahora desacoplada de la ruta operativa real.

**3a/3b/3c. `ajax_autorizar_almacen` reescrito** (`apps/operations/views.py`):
- Decorador cambiado de `@almacen_required` a `@staff_interno_required` (ya importado) — acepta cualquier grupo interno, y dentro de la vista se valida el grupo específico contra `OperationsService.grupo_requerido_por_etapa(ticket)`, exactamente el mismo patrón ya usado por `ajax_registrar_inspeccion` desde la Fase 3 original (sesión 5): si el grupo del usuario no coincide (y no es superusuario), `403` sin invocar el servicio.
- Lee `requiere_calidad`/`confirmado` del body JSON (ambos opcionales) y los pasa tal cual al servicio. El `ValidationError` que lanza el servicio cuando falta la confirmación de Materia Prima ya es capturado por `_safe_post` (envuelve todo en `try/except ValidationError`) y se traduce automáticamente a `400` — no hizo falta ningún manejo especial en la vista para el punto 3c.

**Endpoint(s) usados — reescrito el existente, no se creó uno nuevo:** se mantuvo `OperationsService.autorizar_almacen` / `ajax_autorizar_almacen` / `POST /operations/api/autorizar-almacen/` (mismo nombre, misma URL) en vez de crear un endpoint separado para Materia Prima. Motivo: `muelle`/`requiere_calidad`/`confirmado` son todos opcionales con default de negocio resuelto en el servicio, así que el botón "Iniciar Recepción" ya existente (hoy solo lo usa Almacén, sigue enviando `{ticket_id, muelle}`) sigue funcionando sin ningún cambio — la Fase 5 solo necesitará exponer el checkbox/confirmación en la UI para Materia Prima, no una llamada a un endpoint distinto. Duplicar el endpoint habría significado mantener 2 copias del mismo candado de permiso y de la misma lógica de creación de `TicketLineInspection`.

**`apps/operations/tests.py` actualizado** (imprescindible: los cambios de firma/comportamiento rompían la suite existente si no se ajustaba):
- Grupo y usuario `MATERIA_PRIMA`/`materia_prima_test` agregados a `setUpTestData` de la clase original; `_crear_ticket_en_etapa_almacen` ahora llama `autorizar_almacen(usuario=..., confirmado=...)` eligiendo el actor correcto según `u_mss_tdb` (antes `usuario_almacen=self.u_almacen` fijo, que ya no compila con la firma nueva y además habría fallado con `ValidationError` para el caso `'MP'` al no confirmar).
- Nueva clase `AutorizarAlmacenBifurcacionMateriaPrimaTests` (14 tests nuevos, propio `setUpTestData` con usuarios `_test2` para no chocar con la clase existente): bifurcación de `grupo_requerido_por_etapa` en `VIGILANCIA_INGRESO` (MP→`MATERIA_PRIMA`, comercial→`ALMACEN`); rechazo `400`/`ValidationError` de Materia Prima sin `confirmado`; default de `requiere_calidad` (`True` para Materia Prima, `False` para Almacén); `ticket.muelle` se escribe y `AppointmentSlot.dock` NO cambia; **`requiere_calidad` desacoplado de `tipo_flujo`** (un ticket MP con `requiere_calidad=False` explícito salta Calidad igual, aunque `tipo_flujo` siga siendo `CON_CALIDAD` — la prueba que más directamente valida el punto 3d); y 4 tests del endpoint AJAX real (`400` sin confirmar, `200` confirmando, `403` con el grupo equivocado, `200` para Almacén sin necesidad de confirmar).

**Validación realizada:**
- `manage.py check` y `makemigrations --check --dry-run` limpios (sin cambios de modelo, como se pidió).
- `manage.py test apps.operations`: **16/16 OK** (5 originales + 3 nuevos de setup implícito + los 14 nuevos... conteo real: 2 clases, 16 tests totales, todos pasan).
- Verificación adicional vía `Client` real contra una BD de prueba con rollback forzado (fuera de `manage.py test`, HTTP real end-to-end): usuario `MATERIA_PRIMA` sin `confirmado` → `400` con el mensaje esperado, sin tocar el ticket; con `confirmado=True` → `200`, `ticket.muelle == 'M-1'`, `ticket.requiere_calidad == True` (default), `etapa_actual == ALMACEN`, y `AppointmentSlot.dock` sin cambiar; un intento posterior de cerrar la etapa (`ajax_registrar_inspeccion`) con el mismo usuario `MATERIA_PRIMA` fue rechazado con `403` — **correcto**, porque al quedar `requiere_calidad=True` por default, el candado ahora exige el grupo `CALIDAD` para ese paso, no `MATERIA_PRIMA`, confirmando que `grupo_requerido_por_etapa` y `registrar_calidad` quedaron perfectamente alineados sobre la misma señal.
- Barrido de `{#` multilínea: N/A (no se tocó ningún `.html` esta sesión).

**Fuera de alcance de esta sesión (explícitamente, según lo pedido — Fase 5/6 pendientes):**
- **Templates/JS**: el botón "Iniciar Recepción" en `detalle_ticket.html`/`ticket_actions.js` sigue mostrándose solo para Almacén — `acciones['puede_autorizar_almacen']` (`apps/operations/views.py`) sigue hardcodeado a `es_almacen`, y `acciones['puede_registrar_almacen']` (gate de "Mi Sesión") sigue comparando `grupo_etapa_activa == 'ALMACEN'` literal — para un ticket enrutado a `MATERIA_PRIMA`, ambos flags dan `False` hoy, así que ese rol **no ve ningún botón todavía** aunque el backend ya lo acepte vía API directa (confirmado en la validación funcional de esta sesión). No hay checkbox "Solicitar Inspección Calidad" ni input de muelle en ningún formulario — ambos existen solo a nivel de API. Todo esto es exactamente lo que corresponde a la Fase 5, que el usuario pidió no tocar aquí.
- Historial/Reporte para Materia Prima (Fase 6) — el panel `panel_materia_prima.html` sigue siendo solo el aterrizaje de la sesión 29, sin listar tickets pendientes ni historial.
- No se tocó `panel_materia_prima` (view) más allá de lo ya hecho en la sesión 29 — su docstring todavía referencia la bifurcación como "pendiente de conectar", lo cual sigue siendo cierto desde el punto de vista de la UI (la bifurcación en sí ya existe en el backend, pero el panel no la consume todavía).

### 2026-08-06 (sesión 30b) — Bug real encontrado antes de Fase 5: `panel_calidad` seguía filtrando por `tipo_flujo`, ticket podía quedar atascado

El usuario pidió confirmar puntualmente si `panel_calidad` (`apps/operations/views.py`, pestaña "Pendientes") había quedado actualizado tras la bifurcación de la sesión 30, ya que su queryset filtraba directo por `tipo_flujo='CON_CALIDAD'` — un punto del análisis de impacto original (sesión 27) que no se había revisado en la sesión 30.

**Confirmado: seguía sin actualizar, y el riesgo era real, no solo teórico.** `registrar_calidad`/`registrar_salida`/`grupo_requerido_por_etapa` ya bifurcan sobre `ticket.requiere_calidad` (capturado en "Iniciar Recepción", sesión 30) — pero `panel_calidad` (`apps/operations/views.py:707`, antes de este fix) seguía filtrando por `tipo_flujo` (legacy, fijado en `confirmar_cita` según el tipo de OC, antes de que exista ningún actor de recepción). Ambas señales pueden divergir: `autorizar_almacen` ya acepta `requiere_calidad` como parámetro explícito e independiente del actor/tipo de OC (confirmado con un test de la sesión 30, `test_requiere_calidad_explicito_desacoplado_de_tipo_flujo`, para el caso inverso). Un actor `ALMACEN` (OC comercial, `tipo_flujo='SOLO_ALMACEN'` fijado en `confirmar_cita`) que marque explícitamente `requiere_calidad=True` al "Iniciar Recepción" — algo ya posible hoy vía API directa, sin necesitar ninguna UI de la Fase 5 — deja el ticket con `etapa_actual=CALIDAD` y `grupo_requerido_por_etapa(ticket)=='CALIDAD'`, pero **nunca aparecía en la bandeja de Pendientes de Calidad** (filtrada por `tipo_flujo='CON_CALIDAD'`, que sigue siendo `'SOLO_ALMACEN'`) — un ticket atascado, sin ruptura de datos pero sin ningún punto de entrada operativo para que Calidad lo vea y lo cierre.

**Fix**: `apps/operations/views.py::panel_calidad`, el filtro `tipo_flujo='CON_CALIDAD'` del queryset `tickets_para_calidad` se reemplazó por `requiere_calidad=True` — misma señal que ya usa el resto de la cadena de permiso/estado desde la sesión 30. Sin riesgo de orden de escritura: el queryset ya exige `stages__etapa='ALMACEN_RECEPCION'`, etapa que solo existe después de que `autorizar_almacen` ya fijó `requiere_calidad` explícitamente — no hay ninguna ventana en la que el ticket pudiera filtrarse con un `requiere_calidad` todavía no decidido. Se actualizó también el comentario del docstring del método (antes describía el filtro como "`tipo_flujo=CON_CALIDAD`").

**Validación realizada:**
- `manage.py check` y `makemigrations --check --dry-run` limpios (sin cambios de modelo). `manage.py test apps.operations`: **16/16 OK** (sin cambios de comportamiento en los tests existentes — ninguno ejercitaba este escenario específico de divergencia).
- Verificación funcional dedicada vía `Client` real con rollback forzado: ticket de OC comercial (`tipo_flujo='SOLO_ALMACEN'` confirmado), actor `ALMACEN`, `autorizar_almacen(..., requiere_calidad=True)` explícito → `ticket.etapa_actual == 'ALMACEN'`, `grupo_requerido_por_etapa(ticket) == 'CALIDAD'`, y **el ticket sí aparece** en `panel_calidad`'s `context['tickets']` para un usuario `CALIDAD` real (antes del fix habría quedado fuera, confirmado por lectura del filtro original).

**Fuera de alcance de esta sesión (confirmado, no revisado ni tocado — señalado como riesgo pendiente de evaluar, no como bug confirmado):** se detectó al barrer el proyecto por `tipo_flujo` que `detalle_ticket` (`apps/operations/views.py:843`, `es_flujo_calidad = (ticket.tipo_flujo == 'CON_CALIDAD')`, usado en el cálculo de `puede_registrar_salida`) y 3 plantillas (`detalle_ticket.html`, `panel_vigilancia.html`, `trazabilidad_ticket.html`) también condicionan su UI/lógica directamente sobre `ticket.tipo_flujo` en vez de `ticket.requiere_calidad` — mismo patrón de riesgo de divergencia que el corregido aquí, pero **no confirmado como bug reproducible** (no se investigó a fondo, fuera del alcance puntual pedido: "confirma este punto y, si aplica, corrige panel_calidad"). Queda pendiente de que el usuario decida si se revisa antes o durante la Fase 5.
