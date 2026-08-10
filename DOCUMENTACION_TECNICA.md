# Documentación Técnica — Daryza Portal (VBS · Vendor Booking System)

Guía de arquitectura para un desarrollador que se suma al proyecto. Este documento explica **qué es el sistema y cómo está construido**; para el detalle sesión por sesión de cada cambio, decisión de diseño y bug corregido, ver `CLAUDE.md` (historial completo del proyecto, actualizado al cierre de cada sesión de trabajo).

---

## 1. Qué es este sistema

Portal Django para que proveedores agenden citas de entrega a las plantas de Daryza, y para que el personal interno (Compras, Almacén/Materia Prima, Calidad, Vigilancia) gestione esa recepción de principio a fin — desde que el proveedor solicita el cupo hasta que el ticket queda `FINALIZADO` y sale de planta.

Flujo de negocio, a alto nivel:

```
Proveedor solicita cita (con OCs de SAP)
   → Compras/Almacén confirma (genera Ticket + QR)
   → Proveedor carga COA (Certificado de Análisis) por línea de OC, si aplica
   → Vigilancia escanea QR y autoriza ingreso a planta
   → Almacén o Materia Prima recibe físicamente ("Iniciar Recepción")
   → Calidad inspecciona (solo si el ticket lo requiere — ver §4)
   → Vigilancia registra salida → Ticket FINALIZADO
```

## 2. Stack técnico

| Capa | Tecnología |
|---|---|
| Backend | Django 6.0.3, Django REST Framework 3.17 |
| Base de datos | PostgreSQL (`psycopg2-binary`) |
| Auth de la API | `rest_framework.authtoken` (Token Authentication) — usado por el endpoint de sincronización SAP |
| Estáticos | WhiteNoise (`CompressedManifestStaticFilesStorage`) |
| CORS | `django-cors-headers`, middleware activo (sin `CORS_ALLOWED_ORIGINS` configurado a la fecha) |
| Archivos | Pillow; integración propia con **OneDrive/Microsoft Graph API** para almacenar COAs (`apps/base/utils.py::OneDriveClient`) |
| Reportes | `openpyxl` (Excel), `xhtml2pdf` (PDF) |
| Frontend | Django Templates server-rendered + Bootstrap 5 (CDN) + JS vanilla, AJAX (fetch) contra endpoints propios que devuelven JSON |
| i18n | `es-pe`, zona horaria `America/Lima` (`USE_TZ=True` — ver nota de zona horaria en §7) |

Dependencia listada pero no usada activamente: `django-extensions` está en `requirements.txt` pero no en `INSTALLED_APPS`.

## 3. Estructura de apps

```
apps/
  base/          # Infraestructura transversal, sin modelos concretos propios
  sap_sync/      # Espejo local de datos maestros de SAP
  appointments/  # Portal del proveedor: slots y solicitud/gestión de citas
  operations/    # Ciclo de vida operativo del Ticket
  scheduling/    # Administración de plantillas de horario semanal
api/             # Endpoint REST de sincronización SAP (fuera de apps/)
core/            # settings.py, urls.py raíz, .env
templates/       # Plantillas de nivel de proyecto (base.html, login, portal proveedor)
static/          # CSS/JS de nivel de proyecto
```

| App | Propósito | Modelos clave |
|---|---|---|
| `apps.base` | `TimeStampedModel` (abstracto), decoradores de permiso por grupo, router de home por rol (`redirect_by_role`), cliente OneDrive, `resolver_periodo` (filtro de mes/año compartido), `reporting.py` (exportación Excel/PDF compartida) | — |
| `apps.sap_sync` | Espejo local de OCs sincronizadas desde SAP (vía el endpoint de `api/`) | `PurchaseOrder`, `PurchaseOrderLine` |
| `apps.appointments` | Portal del proveedor: agenda de slots, solicitud de citas, carga de COA por línea, datos de vehículo/conductor | `AppointmentSlot`, `Appointment` |
| `apps.operations` | El corazón del sistema: ciclo de vida del `Ticket` (Compras → Vigilancia → Almacén/Materia Prima → Calidad → Vigilancia), paneles por rol | `Ticket`, `TicketStage`, `TicketLineInspection`, `TicketLineCOA`, `TicketDatosIngreso` |
| `apps.scheduling` | Plantillas de horario semanal (`ScheduleTemplate`/`WeeklySlotRule`) que generan `AppointmentSlot` en bloque | `ScheduleTemplate`, `WeeklySlotRule` |
| `api` (raíz) | Endpoint REST `/api/v1/sync-oc/`, consumido por un demonio VB.NET externo que sincroniza OCs desde SAP | usa modelos de `apps.sap_sync` |

No hay modelo `User` propio — los roles se implementan con **Django Groups** nativos: `PROVEEDORES`, `COMPRAS`, `ALMACEN`, `MATERIA_PRIMA`, `CALIDAD`, `VIGILANCIA`. El enrutamiento por rol tras el login ocurre en `apps/base/views.py::redirect_by_role`, que redirige a cada panel según el grupo del usuario (superusuarios y usuarios sin grupo caen a `/admin/`).

Namespacing de URLs (`core/urls.py`): `appointments:`, `operations:`, `scheduling:`.

### `services/` en la raíz

**No usar** — carpeta de código muerto/duplicado que existió temprano en el proyecto; fue eliminada por completo (ver `CLAUDE.md`, cierre de los hallazgos CRÍTICO originales). Si algo hace referencia a ella, es un rastro obsoleto, no una fuente válida.

## 4. Modelos principales y sus relaciones

```
PurchaseOrder (sap_sync) ──< PurchaseOrderLine
       │  (M2M)
       │
Appointment (appointments) ──> AppointmentSlot
       │  (OneToOne)
       ▼
    Ticket (operations)
       │
       ├──< TicketStage           (timestamps de inicio/fin por etapa)
       ├──< TicketLineInspection  (una fila por línea de OC × etapa ejecutada)
       ├──< TicketLineCOA         (COA cargado por el proveedor, 1 por línea)
       └──1─1 TicketDatosIngreso  (placa/conductor, 1 por Ticket)
```

- **`PurchaseOrder`** (`apps.sap_sync`): espejo local de una OC de SAP. `u_mss_tdb == 'MP'` marca una OC de **Materia Prima**; la property `es_materia_prima` es la fuente única de verdad para esa distinción en todo el código.
- **`PurchaseOrderLine`**: línea de una OC. `requiere_coa` (booleano, ajustable por Compras) decide si esa línea específica necesita Certificado de Análisis.
- **`AppointmentSlot`** (`apps.appointments`): un horario concreto (fecha + hora + muelle + capacidad) que el proveedor puede reservar. Generado en bloque desde `apps.scheduling` o manualmente.
- **`Appointment`**: la solicitud de cita de un proveedor, vinculada a un `AppointmentSlot` y a N `PurchaseOrder` (M2M). Ciclo: `SOLICITADO → CONFIRMADA (Compras/Almacén aprueba, genera el Ticket) → FINALIZADA`, con las ramas `RECHAZADO`/`CANCELADA`.
- **`Ticket`** (`apps.operations`): el objeto operativo real, uno por `Appointment` confirmada (`OneToOneField`). Trae la máquina de estados (`etapa_actual`, ver §5) y los 2 campos del rediseño Materia Prima: `muelle` (dato propio del ticket, no del slot compartido) y `requiere_calidad` (decisión operativa capturada al "Iniciar Recepción").
- **`TicketStage`**: historial de etapas con `fecha_inicio`/`fecha_fin`, para trazabilidad y cálculo de tiempos (`Lead Time`).
- **`TicketLineInspection`**: registro de inspección por línea de OC **y por etapa** (`VIGILANCIA`/`ALMACEN`/`CALIDAD`) — `unique_together = ('ticket', 'po_line', 'etapa')`. Cada etapa que toca una línea escribe su propia fila; nunca se sobrescriben entre sí (ver §5, paso 2 de Calidad).
- **`TicketLineCOA`**: el Certificado de Análisis cargado por el proveedor, **independiente** de las etapas operativas — vive aparte para que la carga del COA no interfiera con lo que Almacén/Calidad/Vigilancia escriben después. Es la única fuente de verdad para "¿está cargado el COA de esta línea?" en todo el sistema.
- **`TicketDatosIngreso`**: placa del vehículo y datos del conductor. Informativo — Vigilancia lo consulta pero el sistema **no bloquea** el ingreso si falta.

### Campo legacy `tipo_flujo`

`Ticket.tipo_flujo` (`CON_CALIDAD`/`SOLO_ALMACEN`) se sigue calculando en `confirmar_cita()` según el tipo de OC, pero **ninguna lógica operativa activa depende de él** desde el rediseño Materia Prima (ver §6) — fue reemplazado por `Ticket.es_materia_prima` (property, tipo de OC) y `Ticket.requiere_calidad` (decisión operativa). La única excepción documentada es el requisito de COA obligatorio al ingreso, que sigue leyendo `PurchaseOrderLine.requiere_coa` directamente (no `tipo_flujo`) — ver `iniciar_ingreso_planta` en `apps/operations/services.py`. No se elimina el campo porque desacoplarlo de raíz no forma parte del alcance ya cerrado; no debe usarse como fuente de verdad en código nuevo.

## 5. Máquina de estados: `Ticket.etapa_actual`

`etapa_actual` es un candado de servicio: cada método de `OperationsService` que ejecuta un paso del flujo (`apps/operations/services.py`) valida al inicio que el ticket esté en la etapa que le corresponde (`_validar_etapa`, lanza `TicketEtapaError` si no) y la avanza al final, dentro de la misma transacción atómica. Esto impide saltarse pasos, aunque alguien llame los endpoints AJAX fuera de orden.

En paralelo, `OperationsService.grupo_requerido_por_etapa(ticket)` responde **a quién le toca actuar** en cada etapa — es la contraparte "de permiso" del candado de orden: los endpoints AJAX comparan el grupo del usuario autenticado contra este valor (o exigen superusuario) antes de invocar el servicio.

### Diagrama de estados (con la bifurcación Materia Prima)

```
                    ┌─────────────────────┐
                    │  PENDIENTE_INGRESO  │  (default al crear el Ticket)
                    └──────────┬──────────┘
                               │ iniciar_ingreso_planta()      [turno: VIGILANCIA]
                               │ · valida COA obligatorio por línea (requiere_coa)
                               │ · crea TicketLineInspection etapa=VIGILANCIA
                               ▼
                    ┌─────────────────────┐
                    │ VIGILANCIA_INGRESO  │
                    └──────────┬──────────┘
                               │ autorizar_almacen()  a.k.a. "Iniciar Recepción"
                               │ [turno: ALMACEN o MATERIA_PRIMA, según
                               │  ticket.es_materia_prima → _grupo_actor_recepcion]
                               │ · MATERIA_PRIMA exige confirmado=True o rechaza
                               │ · captura muelle y requiere_calidad (default:
                               │   True si el actor es MATERIA_PRIMA, False si ALMACEN)
                               │ · crea TicketLineInspection etapa=ALMACEN
                               ▼
                    ┌─────────────────────┐
                    │       ALMACEN       │
                    └──────────┬──────────┘
                               │ registrar_calidad() — PASO 1: el actor de
                               │ recepción (ALMACEN/MATERIA_PRIMA) registra SU
                               │ PROPIA inspección línea por línea, SIEMPRE,
                               │ sin importar requiere_calidad. Escribe/actualiza
                               │ in situ la fila etapa=ALMACEN.
                               │
                     ┌─────────┴──────────┐
                     │                    │
        requiere_calidad=True     requiere_calidad=False
                     │                    │
                     ▼                    │
          ┌─────────────────┐             │
          │     CALIDAD     │             │
          └────────┬────────┘             │
                    │ registrar_calidad() │
                    │ — PASO 2 [turno:    │
                    │ CALIDAD]: inspección│
                    │ ADICIONAL e         │
                    │ independiente, fila │
                    │ NUEVA etapa=CALIDAD │
                    │ (no sobreescribe la │
                    │ del actor)          │
                    └────────┬────────────┘
                              │                     │
                              └──────────┬──────────┘
                                         ▼
                    ┌─────────────────────┐
                    │  VIGILANCIA_SALIDA  │  (ambos caminos convergen aquí)
                    └──────────┬──────────┘
                               │ registrar_salida()             [turno: VIGILANCIA]
                               │ · cierra CALIDAD_INSPECCION o ALMACEN_FINALIZADO
                               │ · calcula tiempo_total_planta
                               │ · Ticket.estado = FINALIZADO
                               │ · Appointment.status = FINALIZADA
                               ▼
                    ┌─────────────────────┐
                    │      FINALIZADO     │  (etapa terminal)
                    └─────────────────────┘
```

### Tabla resumen: precondición → acción → postcondición

| `etapa_actual` (antes) | Grupo que actúa | Método de `OperationsService` | `etapa_actual` (después) |
|---|---|---|---|
| `PENDIENTE_INGRESO` | `VIGILANCIA` | `iniciar_ingreso_planta` | `VIGILANCIA_INGRESO` |
| `VIGILANCIA_INGRESO` | `ALMACEN` o `MATERIA_PRIMA` (según tipo de OC) | `autorizar_almacen` | `ALMACEN` |
| `ALMACEN` | `ALMACEN` o `MATERIA_PRIMA` (el mismo actor de arriba) | `registrar_calidad` → paso 1 (`_registrar_inspeccion_recepcion`) | `CALIDAD` si `requiere_calidad`, si no `VIGILANCIA_SALIDA` |
| `CALIDAD` | `CALIDAD` | `registrar_calidad` → paso 2 (`_registrar_inspeccion_calidad`) | `VIGILANCIA_SALIDA` |
| `VIGILANCIA_SALIDA` | `VIGILANCIA` | `registrar_salida` | `FINALIZADO` |

Puntos clave de diseño a tener presentes al tocar este código:

- **`TicketLineInspection.etapa` se mantiene fijo en `'ALMACEN'`** para la recepción, sin importar si el actor real fue el grupo `ALMACEN` o `MATERIA_PRIMA` — ese campo representa la etapa del flujo, no el grupo Django que la ejecutó. Evita tener que tocar `ETAPA_CHOICES` y todos sus consumidores.
- **`requiere_calidad` está desacoplado de `es_materia_prima`/`tipo_flujo`**: es una decisión operativa explícita, capturada al "Iniciar Recepción", que puede divergir del tipo de OC (una OC comercial puede marcar `requiere_calidad=True` y pasar por Calidad igual). Cualquier vista/consulta que necesite saber "¿este ticket pasa por Calidad?" debe leer `ticket.requiere_calidad`, **no** `tipo_flujo` ni `es_materia_prima` — hubo un bug real (`panel_calidad` filtrando por `tipo_flujo`) que dejó un ticket sin bandeja de entrada visible para Calidad; hay un test de regresión dedicado (`RequiereCalidadForkTests`) para este caso exacto.
- **No hay tickets con OCs mixtas** (Materia Prima + comerciales en la misma solicitud) — bloqueado en el portal del proveedor al solicitar la cita (`AppointmentService.solicitar_cita_borrador`), lo que hace determinístico a `_grupo_actor_recepcion` (basta con verificar si *alguna* OC es MP).
- **El candado de permiso (`grupo_requerido_por_etapa`) y la lógica de negocio (`registrar_calidad`, etc.) deben leer siempre la misma señal.** Si se cambia el criterio de bifurcación en un lado sin actualizar el otro, quedan desalineados — ya ocurrió una vez (sesión 37) y quedó documentado con tests.

## 6. Convenciones de código

- **"Fat services, thin views"**: la lógica de negocio vive en clases estáticas por app — `AppointmentService`/`SlotService` (`apps/appointments/services.py`), `OperationsService` (`apps/operations/services.py`), `SchedulingService` (`apps/scheduling/services.py`). Las vistas casi no tienen lógica: parsean el request, llaman al servicio, devuelven JSON o renderizan.
- **Permisos por grupo, no por `Permission` de Django**: decoradores en `apps/base/decorators.py` — `proveedor_required`, `almacen_required`, `materia_prima_required`, `calidad_required`, `vigilancia_required`, `compras_required`, y los combinados `staff_interno_required` (cualquier rol interno) / `staff_o_proveedor_required` (rol interno o el proveedor dueño). Los superusuarios siempre pasan. Algunos endpoints (ej. `ajax_registrar_inspeccion`, `ajax_autorizar_almacen`) usan el decorador combinado como primer filtro y luego comparan `grupo_requerido_por_etapa(ticket)` contra los grupos del usuario dentro de la vista, para exigir el grupo *específico* que le toca a esa etapa.
- **Respuestas AJAX uniformes**: patrón `{'status': 'success'|'error', ...}` vía helpers `_json_ok`/`_json_err`, repetidos localmente en `apps/appointments/views.py` y `apps/operations/views.py` (no compartidos entre apps). El frontend (`static/js/modules/api.js`) trata cualquier respuesta no-2xx como error y muestra `data.msg` en un toast.
- **Choices en español, en mayúsculas** (`SOLICITADO`, `CONFIRMADA`, `PROGRAMADO`, `EN_PLANTA`, etc.), definidos como listas de tuplas dentro de cada modelo.
- **Trazabilidad por etapas**: `TicketStage` registra timestamps de inicio/fin por etapa; `TicketLineInspection` registra el detalle por línea de OC y por etapa.
- **Nombres de URL**: `panel_<area>` para dashboards, `ajax_<verbo>_<recurso>` para AJAX.
- **Sistema de plantillas único y vigente**: `templates/base.html` + `static/css/daryza_style.css`. Un segundo sistema (`templates/base/base.html` + `static/css/vbs-design-system.css`) existió como huérfano y **fue eliminado** — si algo referencia esos nombres, es un rastro obsoleto. Al agregar CSS nuevo, siempre en `daryza_style.css`.
- **Regla de comentarios en templates**: las notas de implementación **nunca** van como comentario dentro de un `.html` (ni `{# #}`, ni `{% comment %}`) — solo en `CLAUDE.md` o en el resumen entregado al usuario. Motivo técnico: el tokenizer de Django (`tag_re` en `django/template/base.py`, sin `re.DOTALL`) no reconoce un `{# ... #}` cuyo contenido cruza un salto de línea, y lo renderiza como texto literal en pantalla — esto ya ocurrió más de una vez en este proyecto. Antes de cerrar cualquier trabajo sobre `.html`, conviene barrer los archivos tocados en busca de este patrón.
- **Comentarios explicativos en Python**: abundantes, en español, explicando el *por qué* del flujo de negocio — consérvalos al editar. Puede haber comentarios obsoletos dejados in-place (ej. "AJUSTE QUIRÚRGICO") de parches anteriores; no asumas que un comentario describe el estado actual sin verificar contra el código.
- **Zona horaria**: `USE_TZ=True` + `TIME_ZONE='America/Lima'` implica que `timezone.now()` devuelve UTC crudo. Cualquier comparación contra "ahora" en hora local (ej. bloqueo de horarios ya pasados en la agenda del proveedor) debe usar `timezone.localtime(timezone.now())`, no `timezone.now()` directo — un desfase de hasta 5 horas si se olvida.

Para el detalle completo de cada decisión (por qué se eligió `xhtml2pdf` sobre WeasyPrint, por qué el COA vive en `TicketLineCOA` y no en `TicketLineInspection`, cada bug encontrado y su causa raíz, etc.), ver `CLAUDE.md`.

## 7. Entorno local

### Requisitos

- Python (entorno virtual ya versionado en `./env`)
- PostgreSQL accesible según las credenciales de `core/.env`

### Variables de entorno (`core/.env`, en la raíz de `core/`)

| Variable | Obligatoria | Notas |
|---|---|---|
| `DJANGO_SECRET_KEY` | **Sí** | Sin fallback — el arranque falla con `ImproperlyConfigured` si falta |
| `DB_PASSWORD` | **Sí** | Sin fallback — mismo comportamiento |
| `DB_NAME` | No | default `daryza_portal_db` |
| `DB_USER` | No | default `postgres` |
| `DB_HOST` | No | default `127.0.0.1` |
| `DB_PORT` | No | default `5432` |
| `DEBUG` | No | default `False` |
| `ALLOWED_HOSTS` | No | lista separada por comas, default `localhost,127.0.0.1` |
| `ONEDRIVE_CLIENT_ID` / `ONEDRIVE_TENANT_ID` / `ONEDRIVE_CLIENT_SECRET` / `ONEDRIVE_DRIVE_ID` | No (pero requeridas para que la carga de COA a OneDrive funcione) | Credenciales de la app registrada en Microsoft Graph |
| `SAP_SYNC_TOKEN` | No | Solo usada por `test_api.py`, el script manual de humo contra el endpoint de sincronización SAP |

### Levantar el entorno

```powershell
# Activar el entorno virtual (ya existe en ./env)
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

Los 6 grupos de rol (`PROVEEDORES`, `COMPRAS`, `ALMACEN`, `MATERIA_PRIMA`, `CALIDAD`, `VIGILANCIA`) no se crean por migración ni fixture — se crean directamente vía ORM (`Group.objects.get_or_create(name=...)`) o desde el admin de Django. Un usuario nuevo necesita pertenecer a uno de estos grupos para que `redirect_by_role` lo lleve a un panel; sin grupo, cae a `/admin/` (requiere `is_staff`).

### Correr los tests

```powershell
python manage.py test apps.operations
```

Única app con suite automatizada (`apps/operations/tests.py`) — cubre la máquina de estados completa: bifurcación de actor (Almacén vs. Materia Prima), la confirmación explícita exigida a Materia Prima, el fork de `requiere_calidad`, el candado de permiso por grupo (en ambas direcciones, para las 3 combinaciones de actor posibles), el candado de orden de etapas, los permisos exclusivos de Vigilancia, y la validación de COA obligatorio al ingreso. El resto del proyecto (`apps.appointments`, `apps.scheduling`, `apps.sap_sync`, `apps.base`) no tiene tests automatizados — la validación de esas áreas se hace de forma manual/funcional contra datos reales.

`test_api.py` (raíz del proyecto) es un script manual de humo contra el endpoint de sincronización SAP (`/api/v1/sync-oc/`), no parte de la suite de `manage.py test` — lee `SAP_SYNC_TOKEN` de `core/.env` o lo pide por `input()`.

## 8. Dónde seguir leyendo

- **`CLAUDE.md`** — historial completo de sesiones de trabajo: cada decisión de diseño, cada bug encontrado con su causa raíz y su fix, y las reglas operativas acumuladas del proyecto (comentarios en templates, verificación del sistema de plantillas vigente, etc.). Es la fuente de verdad para "¿por qué el código es así?".
- **`MANUAL_FUNCIONAL.md`** — qué puede hacer cada rol de negocio, sin jerga técnica.
