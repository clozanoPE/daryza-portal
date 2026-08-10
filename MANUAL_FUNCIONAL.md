# Manual Funcional — Portal de Recepción Daryza

Guía para las personas que usan el sistema en el día a día: proveedores que entregan mercadería y personal de Daryza que gestiona esa recepción. No requiere conocimientos técnicos.

---

## 1. ¿Qué hace este sistema?

El Portal de Recepción Daryza organiza todo el proceso de entrega de mercadería de un proveedor a una planta de Daryza, de principio a fin:

1. El **proveedor** solicita un horario (cupo) para entregar la mercadería de una o varias Órdenes de Compra (OC).
2. **Compras** revisa la solicitud y la confirma o la rechaza. Al confirmarla, el sistema genera un **Ticket** con un código QR único para esa entrega.
3. Si la mercadería es Materia Prima, el proveedor carga el **Certificado de Análisis (COA)** de cada línea que lo requiera, antes de presentarse en planta.
4. El día de la cita, **Vigilancia** escanea el QR en la puerta y autoriza el ingreso del vehículo a planta.
5. **Almacén** (mercadería comercial) o **Materia Prima** (insumos/materias primas) recibe físicamente la carga y registra las cantidades reales entregadas.
6. Si corresponde, **Calidad** hace una inspección adicional antes de dar el visto bueno final.
7. **Vigilancia** registra la salida del vehículo — el Ticket queda **Finalizado**.

El sistema mantiene un historial completo de cada entrega: quién hizo qué, en qué momento, y con qué resultado — reemplazando el papel y las planillas sueltas.

## 2. Cómo se accede

Cada persona ingresa con su usuario y contraseña en la pantalla de inicio de sesión. El sistema reconoce automáticamente a qué área pertenece (Proveedor, Compras, Almacén, Materia Prima, Calidad o Vigilancia) y lo lleva directo a su panel de trabajo — no hay que elegir nada manualmente.

Cada persona solo ve las acciones y la información que le corresponden a su rol. Todos los roles de Daryza, además, pueden **consultar** (sin poder editar) el detalle de cualquier entrega ya en curso o finalizada — útil para hacer seguimiento aunque no sea el turno de esa área.

---

## 3. Rol: Proveedor

El proveedor es quien entrega la mercadería. Su pantalla principal es el **Portal de Recepción**, con dos zonas: la agenda de cupos disponibles y el listado de sus propias solicitudes.

### 3.1 Solicitar una cita

1. En la **Agenda de Cupos Semanal**, se ven los próximos 6 días con los horarios disponibles. Cada horario muestra cuántos cupos quedan libres:
   - Verde = disponible.
   - Rojo = lleno.
   - Gris = bloqueado por Daryza, o la hora ya pasó (si es el día de hoy).
2. Se hace clic sobre un horario disponible. Se abre una ventana para elegir qué Orden(es) de Compra se van a entregar en esa cita, de la lista de OCs pendientes asignadas a ese proveedor.
3. **Importante**: no se pueden combinar en una misma solicitud una OC de Materia Prima con una OC comercial — el sistema avisa si se intenta y hay que elegir un horario aparte para cada tipo.
4. Se confirma la solicitud. Queda en estado **Solicitado**, a la espera de que Compras la revise.

### 3.2 Cargar el Certificado de Análisis (COA)

Una vez que Compras confirma la cita (queda en estado **Confirmada**, con su QR generado):

- Si alguna línea de la OC requiere Certificado de Análisis, aparece un panel "Cargar COA por Ítem" en la tarjeta de esa cita.
- El proveedor sube el archivo correspondiente a cada línea que lo pida. Se puede hacer en cualquier momento antes de que el vehículo llegue a planta.
- Mientras falte algún COA obligatorio, **Vigilancia no podrá autorizar el ingreso** del vehículo — así que conviene completarlo con anticipación, no el mismo día de la entrega.

### 3.3 Registrar datos del vehículo y conductor (opcional)

En la misma tarjeta de la cita confirmada, el proveedor puede indicar la placa del vehículo y los datos del conductor (DNI y nombre) que hará la entrega. Es informativo — Vigilancia lo ve como referencia, pero **no es obligatorio** para poder ingresar a planta.

### 3.4 Seguimiento y comprobantes

- **"Ver mi Ticket"**: desde la tarjeta de la cita, o desde el historial, se accede al detalle completo de la entrega — estado actual, código QR, y el resultado de cada línea una vez que Almacén/Materia Prima/Calidad la revisan.
- **"Imprimir Cargo"**: genera un comprobante en PDF de la entrega, disponible incluso después de que el Ticket ya quedó Finalizado.
- **Mi Historial**: listado completo y paginado de todas las citas del proveedor (solicitadas, confirmadas, rechazadas, finalizadas), con filtros de búsqueda y exportación a Excel/PDF.
- El panel principal también muestra un resumen ("Expediente de Citas") acotado al mes en curso, con un filtro de período para revisar meses anteriores.

---

## 4. Rol: Compras

Compras es quien valida y aprueba las solicitudes de cita que llegan de los proveedores.

### 4.1 Revisar solicitudes pendientes

En la pestaña **"Pendientes"** del panel de Compras aparece cada solicitud con: proveedor (razón social), OC(s) involucradas, y fecha/hora solicitada. Se puede buscar por proveedor, número de OC o Ticket, y filtrar por fecha.

### 4.2 Configurar qué líneas requieren Certificado de Análisis

Antes de aprobar, Compras revisa la lista de líneas de cada OC y marca cuáles necesitan Certificado de Análisis (COA) obligatorio:

- Para OCs de **Materia Prima**, el sistema ya sugiere automáticamente qué líneas probablemente lo necesitan (Compras puede ajustar esa sugerencia libremente antes de guardar).
- Para OCs comerciales, ninguna línea viene marcada por defecto — Compras decide caso por caso.
- Una vez guardada la configuración de una cita, el sistema ya no la vuelve a sugerir automáticamente — respeta lo que Compras decidió, incluso si se reabre la pantalla más tarde.

### 4.3 Confirmar o rechazar la cita

- **Confirmar**: genera el Ticket con su código QR único, y notifica al proveedor. La cita pasa a **Confirmada**.
- **Rechazar**: la cita queda marcada como **Rechazada**, liberando el cupo para que otro proveedor lo use.

### 4.4 Historial y Reporte

La pestaña **"Historial"** muestra todos los Tickets del sistema (no solo los que confirmó este usuario), con su estado y etapa actual, filtrable por período (mes/año). Desde ahí se puede:

- Hacer clic en **"Ver"** para consultar el detalle de trazabilidad completo de cualquier Ticket (solo lectura).
- Usar el botón **"Reporte"** para descargar el listado ya filtrado en pantalla, en Excel o PDF.

---

## 5. Rol: Vigilancia

Vigilancia controla el ingreso y la salida física de los vehículos en la puerta de planta.

### 5.1 Autorizar el ingreso

1. Al llegar el vehículo, Vigilancia escanea el código QR de la cita (o lo busca manualmente por su código).
2. El sistema muestra el detalle de la cita: OC(s), proveedor, y el estado de los Certificados de Análisis por línea (si aplica).
3. Si falta algún COA obligatorio, el sistema **no permite** autorizar el ingreso — hay que indicarle al proveedor que lo complete primero.
4. Si todo está en regla, Vigilancia autoriza el ingreso. El Ticket pasa a **"En Planta"** y queda a la espera de que Almacén o Materia Prima reciba la carga.

Vigilancia también puede ver (sin poder editar) los datos de vehículo/conductor que el proveedor haya registrado, como referencia.

### 5.2 Registrar la salida

Una vez que Almacén/Materia Prima (y Calidad, si correspondía) terminaron su parte, el vehículo puede retirarse. Vigilancia registra la salida — el Ticket queda **Finalizado**, y el sistema calcula automáticamente cuánto tiempo estuvo el vehículo dentro de planta.

### 5.3 Paneles y seguimiento

El panel de Vigilancia organiza las citas del día en tres columnas: **Programados hoy**, **En Planta ahora** (con un cronómetro en vivo del tiempo transcurrido) y **Finalizados hoy**. Una pestaña de **Historial** permite consultar cualquier Ticket de cualquier fecha, con filtro de período y exportación a Excel/PDF.

---

## 6. Rol: Almacén

Almacén recibe físicamente la mercadería de OCs **comerciales** (no Materia Prima).

### 6.1 Iniciar la recepción

Cuando el vehículo ya fue autorizado a ingresar por Vigilancia, aparece en el panel de Almacén como pendiente de recepción. Almacén hace clic en **"Iniciar Recepción"**, indica el muelle donde se va a descargar y confirma. Opcionalmente puede marcar la casilla "Solicitar Inspección Calidad" si, aunque sea una OC comercial, este envío en particular necesita que Calidad lo revise también.

### 6.2 Registrar la inspección propia

Almacén revisa cada línea de la OC y registra, para cada una:

- La **cantidad real** recibida (puede diferir de la cantidad de la OC).
- El **estado**: conforme, en proceso, rechazado, etc.
- Una **observación**, si hace falta explicar alguna diferencia o incidencia.

Si no se pidió inspección de Calidad, al guardar ("Continuar la Inspección sin Calidad") el Ticket queda listo para que Vigilancia registre la salida. Si sí se pidió, el Ticket pasa a la bandeja de Calidad para su revisión adicional.

### 6.3 Historial y Reporte

Igual que en los demás paneles internos, hay una pestaña **"Historial"** con todos los Tickets del sistema, filtro de período, y exportación a Excel/PDF.

---

## 7. Rol: Materia Prima

Es el equivalente de Almacén, pero exclusivo para OCs de tipo **Materia Prima** — un rol separado porque estos insumos suelen requerir un control más estricto (Certificado de Análisis, inspección de Calidad).

### 7.1 Pendientes

El panel de Materia Prima muestra los Tickets que están esperando su acción: los que acaban de ser autorizados a ingresar por Vigilancia ("Iniciar Recepción" pendiente) y los que ya iniciaron la recepción pero aún no se cerraron. También incluye un buscador de QR propio, igual que Vigilancia.

### 7.2 Iniciar la recepción

Al iniciar la recepción de un ticket de Materia Prima, el sistema **exige una confirmación explícita adicional** antes de continuar — una medida de seguridad extra para este tipo de insumos. Además, la casilla "Solicitar Inspección Calidad" viene **marcada por defecto** (a diferencia de Almacén), aunque se puede desmarcar si el caso no lo requiere.

### 7.3 Registrar la inspección propia

Mismo mecanismo que en Almacén: cantidad real, estado y observación por línea. Si se pidió Calidad, el Ticket pasa a esa bandeja; si no, queda listo para que Vigilancia registre la salida.

### 7.4 Historial y Reporte

Misma funcionalidad que en los demás paneles: pestaña de Historial con filtro de período y exportación a Excel/PDF.

---

## 8. Rol: Calidad

Calidad hace una inspección **adicional e independiente** de la de Almacén/Materia Prima, solo cuando fue solicitada explícitamente al iniciar la recepción.

### 8.1 Pendientes

El panel de Calidad muestra únicamente los Tickets que están esperando su inspección — es decir, aquellos donde Almacén o Materia Prima ya registraron su propia recepción y marcaron que este envío necesita revisión de Calidad.

Para ayudar en la revisión, el formulario de cada línea aparece **pre-cargado** con los valores que registró Almacén/Materia Prima (cantidad, estado, observación) — Calidad puede dejarlos igual o corregirlos si su propia inspección arroja un resultado distinto. Ambos registros (el de recepción y el de Calidad) quedan guardados por separado, sin que uno sobreescriba al otro — así queda constancia de ambas revisiones.

### 8.2 Finalizar la inspección de Calidad

Al terminar y guardar, el Ticket queda listo para que Vigilancia registre la salida del vehículo.

### 8.3 Historial y Reporte

Misma funcionalidad que en los demás paneles: pestaña de Historial con filtro de período y exportación a Excel/PDF.

---

## 9. Resumen de estados de una entrega

Para entender rápidamente en qué punto está una entrega, el Ticket pasa por estos estados (visibles en cualquier pantalla de detalle o trazabilidad):

| Estado | Significado |
|---|---|
| Pendiente de Ingreso | El Ticket ya existe (cita confirmada), pero el vehículo todavía no llegó a planta. |
| Ingreso Registrado | Vigilancia ya autorizó el ingreso del vehículo. |
| Recepción en Almacén | Almacén o Materia Prima está recibiendo/ya recibió la carga. |
| Inspección de Calidad | Calidad está haciendo su revisión adicional (solo si fue solicitada). |
| Salida Registrada | La recepción terminó, falta que Vigilancia registre la salida del vehículo. |
| Finalizado | La entrega quedó completa — el vehículo ya salió de planta. |

Cualquier persona de Daryza puede consultar el detalle y la trazabilidad completa de un Ticket (todas sus etapas, con fecha y responsable de cada una) desde su propio panel de Historial, sin importar si le tocó actuar en esa entrega o no.
