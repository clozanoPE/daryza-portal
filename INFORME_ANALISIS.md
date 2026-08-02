# Informe de Análisis — Daryza Portal (Portal de Citas de Proveedores)

**Fecha del análisis:** 2026-07-30
**Alcance:** Lectura completa del código fuente (sin modificaciones), verificación de migraciones contra el modelo actual (`makemigrations --check`, `showmigrations`), y consulta de datos reales en la base de datos Postgres configurada.
**Nota:** Este informe es de solo diagnóstico. No se modificó ni creó ningún archivo de código.

---

## 1. Estado actual por módulo

| Módulo | Estado | Notas |
|---|---|---|
| `apps.base` | **Completo**, con deuda menor | Decoradores de rol y router funcionan correctamente. `utils.py` (OneDriveClient) tiene código muerto y prints de debug. |
| `apps.sap_sync` | **Completo** | Modelo simple y estable, sincronizado 1:1 con migraciones. Sin `views.py`/`urls.py` propios (correcto: la vista vive en `api/`). |
| `apps.appointments` | **Completo, con un bug de bajo impacto** | Flujo de solicitud de cita y carga de COA funcional. Comentario desactualizado sobre una migración ya aplicada. Notificación al proveedor no implementada realmente (solo `print`). |
| `apps.operations` | **Funcional, con un bug crítico activo** | El núcleo del flujo operativo (Vigilancia/Almacén/Calidad) está implementado y es el módulo más complejo. Contiene el bug de `coa_url.url` (ver §2) y una carpeta de plantillas/JS huérfanos de una iteración de UI anterior. |
| `apps.scheduling` | **Completo** | Generación de slots desde plantillas semanales, bien documentado. Permisos restringidos solo a `COMPRAS`, en contradicción con su propia documentación interna. |
| `api/` (SAP) | **Completo, mínimo** | Un solo endpoint (`sync-oc`), con upsert idempotente. Sin manejo de reintentos/logs de sincronización fallida. |
| `services/` (raíz) | **Código muerto / roto** | No se usa en ningún punto del proyecto. `services/services.py` tiene `NameError` garantizado si se ejecutara. |
| `templates/` + `static/` | **Dos sistemas de diseño conviviendo** | El vigente (`base.html` + Bootstrap) está en uso; el segundo (`base/base.html` + `vbs-design-system.css`) está huérfano junto con 6 plantillas y 2 archivos JS. |
| Migraciones | **Al día** | `makemigrations --check --dry-run` no reporta cambios pendientes; `showmigrations` confirma que todas las migraciones existentes están aplicadas en la BD configurada. |
| Tests | **Pendiente / inexistente** | No hay `tests.py` en ninguna app. `test_api.py` (raíz) es un script manual, no una suite. |
| Admin de Django | **Pendiente** | No existe `admin.py` en ninguna app; el panel `/admin/` no gestiona ningún modelo de negocio. |

---

## 2. Inconsistencias detectadas

### 🔴 CRÍTICO

1. **Bug activo en producción/datos reales — `TicketLineInspection.coa_url.url`**
   `apps/operations/services.py` (`OperationsService.get_resumen_ticket`, ~línea 412):
   ```python
   'coa_url': insp.coa_url.url if insp.coa_url else '',
   ```
   `coa_url` está definido como `models.URLField` (`apps/operations/models.py`), es decir, un **string plano**, no un `FileField`. Los strings no tienen atributo `.url` → `AttributeError` garantizado cuando `insp.coa_url` no está vacío.
   **Ya verificado contra la BD real: existen 25 registros de `TicketLineInspection` con `coa_url` no vacío.** Esto significa que el endpoint `operations:ajax_get_ticket_json` (usado para el resumen del ticket) probablemente ya está fallando o fallará en cuanto se invoque sobre esos tickets.

   > ✅ **RESUELTO Y ELIMINADO — sesión 18 (fix) / sesión 19 (eliminación), 2026-08-02.** En la sesión 18 se corrigió a `insp.coa_url or ''` (el campo ya es un string plano, no hace falta `.url`) tras confirmar que el endpoint (`ajax_get_ticket_json`/`get_resumen_ticket`, `/operations/api/ticket/<pk>/json/`) seguía registrado pero **ningún template ni archivo JS del proyecto lo llamaba** — reemplazado de facto por `detalle_ticket`/`trazabilidad_ticket` (que usan `get_estado_actual_por_oc`, corregido desde la sesión 9). En la sesión 19, con una búsqueda final que no encontró ninguna referencia nueva, el usuario confirmó eliminarlo del todo: se borró la vista `ajax_get_ticket_json` (`apps/operations/views.py`), su ruta (`apps/operations/urls.py`) y el método `OperationsService.get_resumen_ticket` (`apps/operations/services.py`) — el bug ya no aplica como corrección porque el código que lo contenía **dejó de existir**. Verificado: la ruta responde `404` (antes `200` con el bug ya corregido), `manage.py check` y la suite de tests (`5/5 OK`) limpios tras el borrado. Ver `CLAUDE.md`, sesión 19, para el hash del commit correspondiente.

2. **Uso incorrecto de `evidencia_url` (FileField) para URLs externas de OneDrive**
   En `apps/operations/services.py` (`iniciar_ingreso_planta` y `registrar_coa_proveedor`), se asigna una URL externa de OneDrive (string `https://...`) directamente a `evidencia_url`, que es un `models.FileField`. Un `FileField` espera un archivo o una ruta relativa a `MEDIA_ROOT`, no una URL externa completa. Esto puede producir rutas rotas al intentar acceder a `evidencia_url.url` o `.path` en cualquier lugar que lo trate como archivo real.

   > ✅ **RESUELTO — sesión 3 (2026-07-30).** El nuevo modelo `TicketLineCOA` guarda el link de OneDrive en `coa_url` (`URLField`, el campo correcto); `evidencia_url` (`FileField`) quedó reservado solo para un archivo local opcional, nunca para la URL externa. `registrar_coa_proveedor` e `iniciar_ingreso_planta`, reescritos en esa sesión, ya no reproducen el bug. Verificado en la sesión 17: `evidencia_url` no se asigna en ningún punto del código actual, solo se declara en las definiciones de modelo.

3. **Credenciales y configuración insegura hardcodeadas en `core/settings.py`**
   - `SECRET_KEY` con fallback inseguro visible en el propio código fuente: `os.getenv('DJANGO_SECRET_KEY', 'django-insecure-default-key-change-this')`.
   - Contraseña de PostgreSQL con fallback hardcodeado en texto plano: `os.getenv('DB_PASSWORD', 'root1234')` (línea 91), a pesar de existir un `.env`.
   - `DEBUG` por defecto `'True'` si la variable de entorno no está definida — riesgo si se despliega sin configurar explícitamente `DEBUG=False`.
   - `ALLOWED_HOSTS = ['localhost', '127.0.0.1']` hardcodeado (no lee de entorno) — bloquea cualquier despliegue real hasta editar el código fuente directamente.

   > ✅ **RESUELTO — sesión 18 (2026-08-02).** `SECRET_KEY` y `DB_PASSWORD` ya no tienen fallback en texto plano: si `DJANGO_SECRET_KEY`/`DB_PASSWORD` no están definidas en `core/.env`, `settings.py` lanza `ImproperlyConfigured` con un mensaje claro al arrancar, en vez de seguir silenciosamente con un valor inseguro. `DEBUG` ahora usa `'False'` como default (antes `'True'`) si la variable no está definida. `ALLOWED_HOSTS` ahora se lee de la variable de entorno `ALLOWED_HOSTS` (lista separada por comas), con `localhost,127.0.0.1` como default si no se define (preserva el comportamiento local actual sin requerir cambios en `.env`). Verificado: `core/.env` ya tenía `DJANGO_SECRET_KEY`/`DB_PASSWORD`/`DEBUG` definidos antes del cambio (confirmada su presencia, sin exponer los valores), así que el entorno de trabajo actual no se vio afectado; `manage.py check` sigue limpio.

4. **Token de autenticación real expuesto en texto plano**
   `test_api.py` (raíz del proyecto) contiene un token DRF hardcodeado (`TOKEN = "7f799b11...706d0 [valor real redactado en sesión 19 antes del primer commit — ver .env local o rotar]"`). Si el repositorio se comparte o se sube a un control de versiones, el token queda comprometido y debería revocarse/rotarse cuanto antes.

   > ✅ **RESUELTO — sesión 18 (2026-08-02).** El token ya no está hardcodeado: `test_api.py` ahora lo lee de la variable de entorno `SAP_SYNC_TOKEN` (vía `core/.env`) o lo pide por `input()` si no está definida. **Pendiente de acción del usuario, fuera del alcance de este cambio de código**: el token real que estaba expuesto en el archivo (`7f799b11...706d0 [valor real redactado en sesión 19 antes del primer commit — ver .env local o rotar]`) sigue siendo válido en el sistema DRF hasta que se rote manualmente — Claude no tiene forma de rotarlo.

5. **`requirements.txt` codificado en UTF-16LE con BOM (no UTF-8)**
   Verificado con inspección binaria del archivo. `pip install -r requirements.txt` puede fallar o comportarse de forma inconsistente según el entorno/SO (especialmente en Linux/CI), ya que la mayoría de herramientas esperan texto UTF-8/ASCII.

   > ✅ **RESUELTO — sesión 14 (2026-08-02).** Re-codificado a UTF-8 real sin BOM (el primer intento con la herramienta de edición habitual solo quitó el BOM pero mantuvo 2 bytes por carácter; se forzó la reescritura vía `.NET` `UTF8Encoding($false)` para obtener UTF-8 genuino). Verificado con `pip install -r requirements.txt` contra el entorno virtual del proyecto: las 12 líneas (incluida la nueva `xhtml2pdf==0.2.17`, agregada en la misma sesión) resuelven correctamente, sin errores de parseo.

6. **Carpeta `services/` en la raíz: código muerto y roto, con riesgo de confusión futura**
   - `services/services.py::TicketService.advance_stage` referencia `Ticket` y `timezone` **sin importarlos** → `NameError` garantizado si se llegara a ejecutar.
   - `services/operations_service.py::OperationsService` es una versión antigua/duplicada de `apps/operations/services.py::OperationsService`, con lógica desincronizada (por ejemplo, valida `appointment.coa_pdf` en vez del flujo real de `TicketLineInspection`).
   - Confirmado por búsqueda en todo el proyecto: **nada importa `services.services` ni `services.operations_service`**. Es responsabilidad de una versión anterior del proyecto, no de la actual.

   > ✅ **RESUELTO — sesión 18 (2026-08-02).** Re-verificado por tercera vez (informe original, sesión 17, y esta sesión) que nada en el proyecto importa `services.services` ni `services.operations_service` — cero coincidencias en todo el árbol de archivos `.py`. Con la confirmación explícita del usuario, se **eliminó la carpeta `services/` completa** (`services.py`, `operations_service.py`, `__init__.py`). El proyecto no tiene control de versiones (`git`) inicializado, así que esta eliminación no es recuperable por esa vía — se verificó `manage.py check` y la suite de tests (`5/5 OK`) inmediatamente después para confirmar que nada dependía de ella.

### 🟠 MEDIO

7. **Dos sistemas de plantillas/diseño conviviendo, uno de ellos huérfano**
   - Vigente y usado por todas las vistas activas: `templates/base.html` (Bootstrap 5 + `daryza_style.css`).
   - Huérfano (ninguna vista lo renderiza): `templates/base/base.html` + `templates/base/partials/sidebar.html` + `templates/base/partials/ticket_timeline.html` (`vbs-design-system.css`).
   - El sidebar huérfano referencia nombres de URL que **no existen**: `operations:solicitudes_lista`, `operations:gestion_horarios`, `operations:tickets_lista` (no están en `apps/operations/urls.py`). Si alguna vista futura reactivara este template sin corregirlo, causaría `NoReverseMatch`.
   - `templates/base/partials/ticket_timeline.html` referencia `ticket.requiere_coa`, campo que **no existe** en el modelo `Ticket` (existe `requiere_coa` en `PurchaseOrderLine`/`TicketLineInspection`, no en `Ticket`) — de reactivarse, el condicional siempre sería falso silenciosamente.

8. **6 plantillas huérfanas en `apps/operations/templates/operations/`**
   `ticket_detalle.html`, `tickets_lista.html`, `solicitudes_lista.html`, `gestion_horarios.html`, `aprobar_cita.html`, `_oc_card_grouped.html` — confirmado por búsqueda que ninguna es referenciada por `render()`/`template_name` en `views.py`. Nótese el riesgo de confusión entre `detalle_ticket.html` (activo) y `ticket_detalle.html` (huérfano, nombre casi idéntico invertido).
   Adicionalmente, `templates/registration/login.html` es inalcanzable: `core/urls.py` fuerza `LoginView.as_view(template_name='login.html')`, que resuelve a `templates/login.html`, no a `templates/registration/login.html`.

9. **2 archivos JS huérfanos**
   `static/js/operations/ticket_actions.js` (duplicado sin referencias; el vigente es `static/js/panels/ticket_actions.js`, usado por los 5 templates activos de `operations/`) y `static/js/calendar_handler.js` (no incluido en ninguna plantilla).

10. **Notificación al proveedor no implementada realmente**
    `apps/appointments/services.py::AppointmentService._notificar_proveedor_qr` solo hace `print()` a consola; el bloque de envío de email está comentado y sin activar (no hay integración de WhatsApp tampoco). Además construye `url_qr = f"/appointments/ticket/{appointment.token_qr}/"`, una ruta que **no existe** en `apps/appointments/urls.py` (el detalle real del ticket vive en `operations:detalle_ticket`). Esto es una brecha directa contra el flujo de negocio descrito: el proveedor no recibe ninguna notificación real tras la confirmación de su cita.

11. **Permisos de `apps.scheduling` en contradicción con su propia documentación**
    Los comentarios de cabecera en `apps/scheduling/urls.py` y `views.py` dicen "Todas las rutas requieren... `is_staff`", pero la implementación real usa `@compras_required` (solo grupo `COMPRAS` + superusuarios) en **todas** las vistas del panel de horarios. Ni `ALMACEN` ni un `is_staff` genérico pueden gestionar horarios/muelles, aunque conceptualmente esa gestión suele ser responsabilidad de Almacén en el flujo de negocio descrito.

12. **Confirmación de cita duplicada entre Compras y Almacén**
    Existen dos endpoints AJAX distintos que confirman la misma cita llamando al mismo `AppointmentService.confirmar_cita`: `operations:ajax_confirmar_cita_compras` (panel Compras) y `operations:ajax_confirmar_cita` (panel Almacén, marcado como "legacy/alternativo" en su propio comentario). No queda claro cuál es el paso "oficial" vigente del flujo (el enunciado de negocio dice "confirmación de almacén", pero Compras también puede confirmar).

13. **`PurchaseOrder.status` con valor por defecto que nunca ocurre en la práctica**
    El modelo define `default='O'` y el código filtra por `status__in=['O', 'PENDIENTE']`, pero la consulta directa a la BD muestra que el único valor real sincronizado desde SAP es `'PENDIENTE'`. `'O'` es vestigial.

14. **`django-cors-headers` a medio configurar**
    El middleware está activo en `MIDDLEWARE`, pero no existe `CORS_ALLOWED_ORIGINS` (ni `CORS_ALLOW_ALL_ORIGINS`) en `core/settings.py`. La integración no tiene efecto real tal como está.

15. **Sin `admin.py` en ninguna app**
    Ningún modelo de negocio (`Appointment`, `Ticket`, `PurchaseOrder`, etc.) está registrado en el admin de Django. El fallback de `redirect_by_role` para superusuarios/usuarios sin grupo envía a `/admin/`, donde hoy no hay nada útil que gestionar.

16. **Fallback de COA sobre línea de OC nunca funcional**
    `apps/operations/views.py::detalle_ticket` intenta `getattr(line, 'coa_url', None)` como "Prioridad 2" para determinar si el COA está completo, pero `PurchaseOrderLine` (`apps/sap_sync/models.py`) no tiene ningún campo `coa_url`. El `getattr` con default evita el crash, pero esa rama de fallback nunca puede aportar un valor — es código muerto disfrazado de lógica funcional.

    > ✅ **RESUELTO (efecto colateral) — sesión 6 (2026-07-31).** Al reescribir por completo el cálculo de `coa_completo`/`acciones` en `detalle_ticket` (nuevo `OperationsService.calcular_coa_completo()`/`get_coa_status_por_oc()`, que leen `TicketLineCOA`, el modelo correcto desde la sesión 3), este fallback muerto sobre `PurchaseOrderLine.coa_url` desapareció por completo. No fue el objetivo explícito de esa sesión, pero quedó eliminado como consecuencia directa. Verificado en la sesión 17: `getattr(line, 'coa_url'` no aparece en ningún punto del código actual.

17. **`django-extensions` sin usar**
    Está en `requirements.txt` pero no en `INSTALLED_APPS`.

### 🟡 BAJO

18. Función `__init__` huérfana a nivel de módulo en `apps/base/utils.py` (líneas ~178-181), fuera de la clase `OneDriveClient` — código muerto, nunca se ejecuta, parece un error de copiado/pegado.
19. Comentario desactualizado en `apps/appointments/models.py` (`AppointmentSlot.Meta`) que referencia una migración pendiente `0003_appointmentslot_unique_date_starttime.py`; en realidad ya está incluida en `0001_initial.py` (confirmado con `makemigrations --check`). Es solo documentación desincronizada, no un bug real.
20. Múltiples `print()` de debug dejados en código "productivo" (`apps/base/utils.py::OneDriveClient`, notificación de `apps/appointments/services.py`, línea comentada de debug en `apps/operations/services.py::get_grouped_by_oc`).
21. Bloques extensos de código comentado dejados en varios archivos (`core/settings.py`, `apps/base/decorators.py` con el comentario "# POR esto:", `apps/operations/services.py`, `apps/appointments/services.py`).
22. Cabecera de archivo desincronizada con su ubicación real: `api/sap_api.py` tiene el comentario `# apps/sap_sync/sap_api.py`, pero el archivo vive físicamente en `api/` (fuera de `apps/`), indicando que fue movido sin actualizar su propio encabezado.
23. Sin suite de pruebas automatizadas (`tests.py`); `test_api.py` es un script manual de humo (ver también CRÍTICO #4).

    > 🟡 **PARCIALMENTE RESUELTO — sesión 5 (2026-07-30).** Se creó `apps/operations/tests.py` — el primer archivo de tests del proyecto — con `RegistrarInspeccionPermisoPorEtapaTests` (5 casos), cubriendo el candado de permiso por grupo según la etapa activa (`ajax_registrar_inspeccion`/`grupo_requerido_por_etapa`). Cobertura acotada a ese punto puntual; el resto del proyecto (`apps.appointments`, `apps.scheduling`, `api/`, el resto de `apps.operations`) sigue sin ninguna prueba automatizada.

---

## 3. Brechas frente al flujo funcional descrito

Flujo declarado por el negocio:
`registro de OC → solicitud de cita → confirmación de almacén → carga de COA → registro en vigilancia → autorización de ingreso → muestreo de calidad → descarga → salida`

| Paso del flujo | Implementación encontrada | Brecha |
|---|---|---|
| Registro de OC | `api/v1/sync-oc/` (SAPIntegrationViewSet) → `PurchaseOrder`/`PurchaseOrderLine`, upsert idempotente por `doc_entry` | Sin endpoint de consulta/log de sincronizaciones fallidas; si el demonio VB.NET envía datos inválidos, solo se ve en la respuesta HTTP, no queda registro persistente |
| Solicitud de cita | `AppointmentService.solicitar_cita_borrador` | OK |
| Confirmación de almacén | `AppointmentService.confirmar_cita`, invocado desde **dos** paneles (Compras y Almacén) | Ambigüedad de proceso (ver MEDIO #12) |
| Carga de COA | Dos mecanismos coexistentes: OneDrive por línea (`subir_coa_linea_ajax`) y legacy a nivel de cita completa (`subir_coa_ajax`, `Appointment.coa_pdf`) | Correcto por diseño (documentado como "legacy"), pero aumenta la superficie de mantenimiento |
| Registro en vigilancia / autorización de ingreso | `OperationsService.iniciar_ingreso_planta` (mismo paso) | Ver CRÍTICO #1 y #2 (bugs en el manejo de `coa_url`/`evidencia_url` en esta misma función) |
| Muestreo de calidad | `OperationsService.registrar_calidad`, condicionado a `tipo_flujo == 'CON_CALIDAD'` | Correcto y bien documentado; el caso `SOLO_ALMACEN` omite Calidad intencionalmente |
| Descarga | No existe un estado/etapa explícito de "descarga física" separado | Aparentemente fusionado dentro de `ALMACEN_RECEPCION`; si el negocio necesita medir el tiempo de descarga por separado, falta un `TicketStage` dedicado |
| Salida | `OperationsService.registrar_salida` | OK, calcula `tiempo_total_planta` |
| Notificación al proveedor (QR) | Solo `print()` a consola | Brecha real: el proveedor no recibe el QR por ningún canal automatizado (ver MEDIO #10) |

---

## 4. Próximos pasos propuestos (priorizados)

1. **[Urgente]** Corregir `apps/operations/services.py::get_resumen_ticket` — usar `insp.coa_url` directamente (es un string), no `.url`. Bug activo con datos reales en BD.
2. **[Urgente]** Revisar el uso de `evidencia_url` (FileField) para URLs externas de OneDrive: decidir si debe pasar a `URLField` o si `evidencia_url` debe reservarse para archivos locales reales.
3. **[Urgente/Seguridad]** Rotar el token expuesto en `test_api.py`; eliminar fallbacks inseguros de `SECRET_KEY`/`DB_PASSWORD` en `settings.py`; confirmar `DEBUG=False` y `ALLOWED_HOSTS` correctos antes de cualquier despliegue.
4. **[Alta]** Re-codificar `requirements.txt` a UTF-8 y validar `pip install -r requirements.txt` en un entorno limpio (Linux/CI incluido).
5. **[Alta]** Decidir el destino del clúster de plantillas/JS huérfanos (`base/base.html`, `sidebar.html`, `ticket_timeline.html`, los 6 templates de `operations/`, `registration/login.html`, los 2 JS huérfanos): archivarlos o eliminarlos para reducir confusión y riesgo de edición accidental.
6. **[Alta]** Resolver la carpeta `services/` de la raíz (código muerto/roto): eliminarla o documentar explícitamente por qué se conserva.
7. **[Media]** Implementar la notificación real (email y/o WhatsApp) al proveedor tras la confirmación de la cita, o registrar formalmente esta limitación como deuda técnica aceptada.
8. **[Media]** Registrar los modelos de negocio en `admin.py` de cada app para permitir soporte/corrección manual.
9. **[Media]** Aclarar y unificar el flujo de confirmación de cita (Compras vs. Almacén): definir el dueño real del paso según el proceso de negocio vigente.
10. **[Media]** Añadir pruebas automatizadas, priorizando las transiciones de estado `Appointment → Ticket → TicketStage` y el bug de `coa_url` para evitar regresiones.
11. **[Baja]** Limpiar `print()` de debug, comentarios muertos y el `__init__` huérfano de `apps/base/utils.py`; actualizar el comentario desincronizado de `AppointmentSlot.Meta`.

---

## 5. Diagnóstico del flujo de etapas (Ticket → TicketLineInspection)

**Fecha de este diagnóstico:** 2026-07-30 (sesión de seguimiento, sin modificar código). Alcance: trazar el ciclo de vida completo de `TicketLineInspection` a través de las etapas del `Ticket`, con verificación contra datos reales de los 3 `Ticket` existentes en la BD. Se excluyó explícitamente del foco la carga/validación de COA (ya confirmada funcional), aunque aparece como evidencia de apoyo en el punto 5.1.

### 5.1 Puntos de creación/actualización de `TicketLineInspection`

Existen **5 puntos de escritura** — 4 en `apps/operations/services.py` (los que pidió el análisis) y **1 previo, fuera de ese archivo**, que es el que realmente crea la primera fila de cada línea:

| # | Método | Archivo | Operación | Lookup (antes de `defaults`) | ¿Incluye `etapa`? |
|---|---|---|---|---|---|
| 0 | `AppointmentService.confirmar_cita` | `apps/appointments/services.py` (~L169-183) | `get_or_create` | `ticket, po_line, etapa='ALMACEN'` | Sí — crea el **esqueleto inicial**, una fila por línea, al confirmar la cita (antes de que Vigilancia/Almacén/Calidad actúen) |
| 1 | `OperationsService.iniciar_ingreso_planta` (Vigilancia — entrada) | `apps/operations/services.py` (~L66-81) | `update_or_create` | `ticket, po_line, etapa='VIGILANCIA'` | Sí — crea una fila **nueva y distinta** de la de Almacén |
| 2 | `OperationsService.autorizar_almacen` (Almacén — recepción) | `apps/operations/services.py` (~L117-128) | `get_or_create` | `ticket, po_line, etapa='ALMACEN'` | Sí, pero la fila **ya existe** desde el punto 0 → esto es efectivamente un no-op (`get_or_create` no actualiza en caso de match) |
| 3 | `OperationsService.registrar_calidad` (Calidad **o** cierre de Almacén) | `apps/operations/services.py` (~L171-187) | `update_or_create` | `ticket, po_line, etapa=etapa_linea`, con `etapa_linea = 'CALIDAD' if tipo_flujo=='CON_CALIDAD' else 'ALMACEN'` | Sí, pero el valor es **dinámico** según `tipo_flujo` |
| 4 | `OperationsService.registrar_coa_proveedor` (carga de COA) | `apps/operations/services.py` (~L275-289) | `update_or_create` | `ticket, po_line, etapa='ALMACEN'` | Sí — pero **siempre** apunta a `'ALMACEN'`, sin verificar la etapa real del ticket |

**Riesgo de sobreescritura entre etapas (punto 1c):**

- **Tickets `CON_CALIDAD`**: el punto 3 escribe en `etapa='CALIDAD'` (fila nueva). La fila `etapa='ALMACEN'` del punto 0 **nunca vuelve a tocarse por el cierre de flujo** → queda congelada con su estado inicial (`PENDIENTE`, cantidades de SAP sin ajustar).
- **Tickets `SOLO_ALMACEN`**: el punto 3 escribe en `etapa='ALMACEN'` — **el mismo lookup exacto** que los puntos 0 y 2 → `update_or_create` **sobreescribe en el sitio** la fila original de Almacén con los datos de cierre (estado, cantidad, comentario). No se pierden filas (sigue habiendo 1 por etapa), pero se pierde la distinción entre "lo que Almacén registró al recibir" y "lo que se registró al cerrar el flujo".
- El punto 4 (carga de COA) **también** escribe siempre en `etapa='ALMACEN'`, sin verificar la etapa real del ticket. **Confirmado con datos reales**: en el Ticket 2, las líneas 33/35/36 tienen `usuario_id=8` (cuenta de un proveedor) y `coa_url` cargado en su fila `ALMACEN`, mientras que las líneas hermanas sin COA siguen con `usuario_id=9` (`ucompras`, quien creó el esqueleto en la confirmación). Es decir: la carga de COA del proveedor ya está sobreescribiendo campos (`usuario`, `cantidad_sap`, `cantidad_modificada`, `estado`) de la misma fila que después usan Almacén/Calidad — comparte la misma clave de búsqueda entre actores y momentos distintos del flujo, aunque no sea el foco de este análisis.

### 5.2 ¿Existe algún control que impida modificar una etapa ya cerrada?

**No existe ningún control de ese tipo.** Verificado explícitamente:

- No hay ningún campo `etapa_actual` (ni equivalente) en `Ticket`, ni ninguna comparación contra él antes de guardar un `TicketLineInspection`.
- El único "gate" existente opera sobre **`Ticket.estado`** (`OperationsService._get_ticket_en_planta` exige `estado='EN_PLANTA'`) y sobre la **existencia** de `TicketStage` (`etapas_completadas` en `detalle_ticket`, que solo comprueba si la fila de esa etapa **existe**, sin importar si tiene `fecha_fin` — no distingue "etapa abierta" de "etapa cerrada").
- **Hallazgo adicional, directamente relevante para este punto**: el endpoint que ejecuta el punto 3 (`operations:ajax_registrar_inspeccion`, usado tanto por Almacén como por Calidad) está protegido con `@staff_interno_required` (`apps/base/decorators.py`), que permite **cualquier** grupo interno (`ALMACEN`, `CALIDAD`, `VIGILANCIA`, `COMPRAS`) — no está restringido al rol relevante para esa etapa. La restricción por rol (`es_calidad`/`es_almacen`) solo existe para **mostrar u ocultar el botón en el template** (`apps/operations/views.py::detalle_ticket`, diccionario `acciones`), no en el backend/servicio. **Confirmado con datos reales**: las filas `etapa='CALIDAD'` de los Tickets 1 y 2 tienen `usuario_id=4`, que corresponde al usuario `ualmacen` (grupo `ALMACEN`), no a `ucalidad` (grupo `CALIDAD`, id 6). El paso de Calidad fue ejecutado con una cuenta de Almacén — sea por datos de prueba/seed o por uso real, el código no lo impide en ningún nivel.

### 5.3 ¿Cómo arma `detalle_ticket.html` (y su vista) los datos que muestra?

La vista sí filtra por etapa: `apps/operations/views.py::detalle_ticket` calcula **tres** conjuntos separados vía `OperationsService.get_grouped_by_oc(pk, etapa=...)`: `ocs_agrupadas_almacen`, `ocs_agrupadas_calidad`, `ocs_agrupadas_vigilancia`.

Sin embargo, **el template activo (`detalle_ticket.html`) solo usa `ocs_agrupadas_almacen`** (confirmado por búsqueda textual: `ocs_agrupadas_calidad` y `ocs_agrupadas_vigilancia` no aparecen en ningún `{{ }}`/`{% %}` del archivo). La única tabla de inspección ("Detalle de OC — Inspección") se renderiza siempre desde las filas `etapa='ALMACEN'`, y se reutiliza tanto para que Almacén como para que Calidad editen: mismo atributo `data-inspeccion-id`, mismo JS (`guardarInspeccion()` en `static/js/panels/ticket_actions.js`), mismo endpoint (`operations:ajax_registrar_inspeccion`).

**No hay ninguna distinción entre "lo que hizo mi etapa" y "estado más reciente de la línea".** Consecuencia práctica confirmada con datos reales: en el Ticket 1 (`CON_CALIDAD`), Calidad ya registró `RECHAZADO` para las líneas 1, 3 y 19 (filas `etapa='CALIDAD'`, con `cantidad_modificada` ajustada), pero como el template solo muestra `ocs_agrupadas_almacen`, **esas líneas se siguen viendo como `PENDIENTE` con la cantidad original de SAP** en la pantalla de detalle — el resultado real de Calidad queda guardado correctamente en BD pero es invisible en la UI.

### 5.4 Verificación con datos reales (Tickets 1, 2 y 3)

Conteo de filas de `TicketLineInspection` agrupadas por `(ticket, po_line)`:

| Ticket | tipo_flujo | estado | Líneas | Etapas con fila | Filas totales | Filas por línea |
|---|---|---|---|---|---|---|
| 1 | CON_CALIDAD | EN_PLANTA (CALIDAD_INSPECCION abierta, sin `fecha_fin`) | 24 | ALMACEN, CALIDAD, VIGILANCIA | 72 | **3** (1 por etapa) |
| 2 | CON_CALIDAD | FINALIZADO (todas las etapas cerradas) | 5 | ALMACEN, CALIDAD, VIGILANCIA | 15 | **3** (1 por etapa) |
| 3 | SOLO_ALMACEN | EN_PLANTA (ALMACEN_RECEPCION abierta; aún no llega a `registrar_calidad`) | 6 | ALMACEN, VIGILANCIA (sin CALIDAD, correcto por diseño) | 12 | **2** (1 por etapa) |

**Corrección a la hipótesis inicial**: los datos reales muestran **1 fila por etapa-línea en los tres tickets**, no una sola fila fusionada por línea. La sobreescritura entre etapas **no se ha materializado (todavía) como pérdida de filas** en los datos actuales.

- El Ticket 3 —el único `SOLO_ALMACEN` de la BD— aún no alcanza el paso de cierre (`registrar_calidad`), por lo que el riesgo de sobreescritura de la fila `ALMACEN` descrito en 5.1 está **confirmado a nivel de código, pero todavía no observado empíricamente**.
- Lo que sí está confirmado empíricamente con los tickets `CON_CALIDAD` existentes es el problema de "congelamiento" (5.1/5.3): la fila `ALMACEN` nunca refleja el resultado final de Calidad, y ya se evidenció sobreescritura cruzada de esa misma fila vía la carga de COA del proveedor (usuario y cantidades cambian de "quien confirmó la cita" a "el proveedor que subió el COA").

### 5.5 Síntesis

El problema no es (todavía) pérdida de filas por sobreescritura entre etapas — el modelo de datos sí crea una fila por etapa-línea correctamente en los tres tickets existentes. Los problemas reales y confirmados son:

1. Para tickets `CON_CALIDAD`, la fila `etapa='ALMACEN'` queda congelada en su estado inicial para siempre, y **la UI activa (`detalle_ticket.html`) solo muestra esa fila congelada**, nunca la fila `etapa='CALIDAD'` con el resultado real — brecha de visibilidad, no de datos.
2. Para tickets `SOLO_ALMACEN`, el código de `registrar_calidad` reutiliza la misma clave (`ticket, po_line, etapa='ALMACEN'`) que la creación inicial, lo que sobreescribirá en el sitio los datos de recepción con los de cierre en cuanto el primer ticket de este tipo llegue a ese paso — no observado aún, pero inevitable dado el código actual.
3. No existe ningún control de "etapa cerrada" a nivel de modelo o servicio; el único control real es de autenticación de grupo, y es más permisivo (`staff_interno_required`) de lo que sugieren los comentarios del código — confirmado con datos reales donde una cuenta de Almacén registró resultados de Calidad.

> **Nota (sesión 17):** los 3 puntos de esta síntesis fueron resueltos por el plan de 5 fases construido en las sesiones 3-7 — candado de orden (`Ticket.etapa_actual`, sesión 4), candado de rol (`grupo_requerido_por_etapa`, sesión 5), y separación "Mi Sesión"/"Estado Actual" en la UI (sesión 6). Ver la sección 6 de este informe y `CLAUDE.md` para el detalle completo; no se anotó punto por punto aquí porque el alcance de esta actualización (pedido explícito del usuario) fue la sección 2, no la 5 — la sección 5 ya tiene su propio cierre narrativo en `CLAUDE.md`, sesión 7 ("Cierre del plan de 5 fases").

---

## 6. Funcionalidades agregadas post-análisis inicial (Fases 1-13, sesiones 3-16)

A diferencia de la sección 2 (hallazgos del diagnóstico original, con su estado de resolución anotado arriba), esto es **funcionalidad nueva** construida sobre el sistema después del análisis inicial — no corrige ningún hallazgo de este informe, así que se documenta aparte para que la sección 2 siga siendo una fotografía fiel del estado en 2026-07-30. Detalle completo de cada punto (incluida la validación realizada) en `CLAUDE.md`, sección "Historial de sesiones".

- **`TicketLineCOA`** (sesión 3): modelo que desacopla el COA por línea de OC de las etapas operativas — el proveedor lo carga/corrige independientemente de la etapa en curso del ticket.
- **`Ticket.etapa_actual`** (sesión 4): máquina de estados explícita a nivel de servicio (`OperationsService._validar_etapa`), con candado de orden que impide ejecutar una etapa fuera de secuencia.
- **Permiso por grupo según la etapa activa** (`OperationsService.grupo_requerido_por_etapa`, sesión 5): candado de rol — exige que el grupo del usuario coincida con la etapa activa del ticket, no solo con pertenecer a "algún" grupo interno.
- **"Mi Sesión" vs. "Estado Actual"** (`get_mi_sesion`/`get_estado_actual_por_oc`, sesión 6): separa "lo que yo puedo editar ahora" de "el estado real más reciente de cada línea" en `detalle_ticket.html`.
- **`trazabilidad_ticket` + enrutamiento de `scan_qr` por rol** (sesión 7): vista de solo lectura para quien no tiene turno pendiente; el QR como punto de entrada único que enruta según a quién le toca actuar.
- **Historiales de solo lectura por rol** (sesiones 8 y 11): Compras, Vigilancia, Calidad y el Portal del Proveedor pueden consultar cualquier ticket/cita pasado (no solo los activos del día), cada uno enlazando a `trazabilidad_ticket`.
- **Panel lateral de citas en Administración de Horarios** (sesión 10): detalle de los proveedores citados (con link a `trazabilidad_ticket`) al hacer click en un slot ocupado de la grilla.
- **Filtro de período (mes/año)** en los 4 historiales de solo lectura (sesión 12), con mes actual por defecto y un componente compartido (`apps/base/filters.py::resolver_periodo`, `templates/partials/campo_filtro_periodo.html`).
- **`TicketDatosIngreso`** (sesión 13): placa del vehículo, DNI/CE y nombre del conductor, completados por el proveedor antes del ingreso — informativo para Vigilancia, no bloqueante.
- **Generación de PDF ("Imprimir Cargo")** (sesión 14): vía `xhtml2pdf`, cargo de entrega imprimible con los mismos datos que `trazabilidad_ticket` (ticket, OC(s), proveedor, sede, fecha/hora, trazabilidad de etapas).

Además, durante esta construcción se encontraron y corrigieron varios bugs **no listados en el diagnóstico original** (no eran parte de este informe): el bug de visibilidad de los resultados de Calidad en `detalle_ticket.html` (sesión 6), el bug de COA vacío en la vista del proveedor por leer la fuente incorrecta (sesión 9), la pérdida de acceso del proveedor a su propio ticket finalizado (sesiones 11 y 15), y la falta de feedback de error visible en `login.html` (sesión 16). Detalle completo de cada uno en `CLAUDE.md`.

---

*Este informe no modificó ningún archivo en su versión original (2026-07-30). Actualizado en la sesión 17 (2026-08-02) para anotar el estado de resolución de los hallazgos de la sección 2 y agregar la sección 6 — actualización puramente documental, tampoco modificó ningún archivo de código.*
