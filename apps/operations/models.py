# apps/operations/models.py
from django.db import models
from django.contrib.auth.models import User
from apps.sap_sync.models import PurchaseOrderLine
from apps.appointments.models import Appointment


class Ticket(models.Model):
    """
    Representa el proceso operativo de una cita desde que llega a puerta
    hasta que se retira de la planta.

    FLUJO DINÁMICO según tipo_flujo:
      CON_CALIDAD  : PROGRAMADO → EN_PLANTA → (Vigilancia) → (Almacén) → (Calidad) → FINALIZADO
      SOLO_ALMACEN : PROGRAMADO → EN_PLANTA → (Vigilancia) → (Almacén) → FINALIZADO
                     (el paso de Calidad se omite completamente)

    El campo tipo_flujo se establece en confirmar_cita() según si alguna OC
    de la cita tiene u_mss_tdb == 'MP' y al menos una línea con requiere_coa=True.
    """
    ESTADOS_TICKET = [
        ('PROGRAMADO', 'Programado'),
        ('EN_PLANTA', 'En Planta'),
        ('FINALIZADO', 'Finalizado'),
        ('CANCELADO', 'Cancelado'),
    ]

    TIPO_FLUJO_CHOICES = [
        ('CON_CALIDAD',   'Con Calidad (Materia Prima)'),
        ('SOLO_ALMACEN',  'Solo Almacén (Comercial)'),
    ]

    # ── MÁQUINA DE ESTADOS DE ETAPA (candado a nivel de servicio) ───────────
    # Cada valor representa la última acción operativa completada sobre el ticket.
    # Cadena CON_CALIDAD:  PENDIENTE_INGRESO -> VIGILANCIA_INGRESO -> ALMACEN -> CALIDAD -> VIGILANCIA_SALIDA -> FINALIZADO
    # Cadena SOLO_ALMACEN: PENDIENTE_INGRESO -> VIGILANCIA_INGRESO -> ALMACEN -> VIGILANCIA_SALIDA -> FINALIZADO
    #                      (CALIDAD se omite; ALMACEN cierra directo a VIGILANCIA_SALIDA)
    # OperationsService valida esta cadena al inicio de cada método de etapa y la
    # avanza al final, una vez guardados los cambios de esa etapa.
    ETAPA_PENDIENTE_INGRESO  = 'PENDIENTE_INGRESO'
    ETAPA_VIGILANCIA_INGRESO = 'VIGILANCIA_INGRESO'
    ETAPA_ALMACEN            = 'ALMACEN'
    ETAPA_CALIDAD            = 'CALIDAD'
    ETAPA_VIGILANCIA_SALIDA  = 'VIGILANCIA_SALIDA'
    ETAPA_FINALIZADO         = 'FINALIZADO'

    ETAPA_ACTUAL_CHOICES = [
        (ETAPA_PENDIENTE_INGRESO,  'Pendiente de Ingreso (Vigilancia)'),
        (ETAPA_VIGILANCIA_INGRESO, 'Ingreso Registrado (Vigilancia)'),
        (ETAPA_ALMACEN,            'Recepción en Almacén'),
        (ETAPA_CALIDAD,            'Inspección de Calidad'),
        (ETAPA_VIGILANCIA_SALIDA,  'Salida Registrada (Vigilancia)'),
        (ETAPA_FINALIZADO,         'Finalizado'),
    ]

    appointment = models.OneToOneField(
        Appointment,
        on_delete=models.CASCADE,
        related_name='ticket',
        help_text="Vinculación directa con la cita agendada"
    )
    estado = models.CharField(max_length=20, choices=ESTADOS_TICKET, default='PROGRAMADO')
    codigo_qr = models.CharField(
        max_length=100, unique=True, null=True, blank=True,
        help_text='Código QR único. Igual que Appointment.token_qr.'
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    # Calculado automáticamente al finalizar (KPI de Lead Time)
    tiempo_total_planta = models.DurationField(null=True, blank=True)

    # ── CAMPO CENTRAL DE RUTEO ──────────────────────────────────────────────
    # Se determina en confirmar_cita() y NO cambia durante la ejecución.
    # CON_CALIDAD  → al menos una línea de OC-MP tiene requiere_coa=True
    # SOLO_ALMACEN → todas las OCs son comerciales (ninguna línea con coa requerido)
    tipo_flujo = models.CharField(
        max_length=20,
        choices=TIPO_FLUJO_CHOICES,
        default='CON_CALIDAD',
        help_text=(
            "Determina si el ticket pasa por Calidad (CON_CALIDAD) "
            "o va directo a Almacén (SOLO_ALMACEN). "
            "Se calcula al confirmar la cita según las OCs vinculadas."
        )
    )

    etapa_actual = models.CharField(
        max_length=20,
        choices=ETAPA_ACTUAL_CHOICES,
        default=ETAPA_PENDIENTE_INGRESO,
        help_text=(
            "Última etapa operativa completada. OperationsService la valida "
            "antes de cada acción y la avanza al terminarla (candado de servicio)."
        )
    )

    # ── Rediseño de flujo Materia Prima (Fase 1 — solo modelo, sin lógica todavía) ──
    # tipo_flujo se mantiene sin cambios (sigue siendo la fuente real hasta que las
    # fases de servicio/vistas migren a estos 2 campos y se retire).
    muelle = models.CharField(
        max_length=50, blank=True, default='',
        help_text=(
            "Muelle/puerta de descarga real, capturado al 'Iniciar Recepción' "
            "por Almacén o Materia Prima. Campo propio del Ticket — no reutiliza "
            "AppointmentSlot.dock (ese es el muelle asignado por Programación/"
            "Compras al horario; este es el muelle real usado en la recepción "
            "de este Ticket específico)."
        )
    )

    requiere_calidad = models.BooleanField(
        default=False,
        help_text=(
            "Decide si el ticket pasa a CALIDAD tras la recepción. Reemplaza a "
            "tipo_flujo como señal operativa: se captura explícitamente al "
            "'Iniciar Recepción' (Almacén o Materia Prima), no se infiere "
            "automáticamente de las OCs vinculadas."
        )
    )

    @property
    def es_materia_prima(self):
        """
        True si alguna OC vinculada a la cita es tipo Materia Prima
        (PurchaseOrder.es_materia_prima, vía el UDF de SAP u_mss_tdb).

        Hecho ESTRUCTURAL sobre las OCs de la cita — fijo desde que se
        solicita (las OCs vinculadas no cambian después), a diferencia de
        requiere_calidad (decisión OPERATIVA, capturada explícitamente al
        "Iniciar Recepción", que puede o no coincidir con el tipo de OC).
        Reemplaza a tipo_flujo=='CON_CALIDAD' como señal para la UI/rutas
        que describen el TIPO de OC (sesión 31) — matemáticamente
        equivalente hoy (confirmar_cita calcula tipo_flujo con la misma
        condición), pero desacoplado del campo legacy.

        Usa .all() sobre el M2M ya prefetched cuando esté disponible
        (mismo patrón que el resto del proyecto) en vez de .filter().exists(),
        que siempre dispara una query nueva sin importar el prefetch.
        """
        return any(po.es_materia_prima for po in self.appointment.purchase_orders.all())

    def __str__(self):
        return f"Ticket {self.id} [{self.tipo_flujo}] - Cita: {self.appointment.id}"


class TicketStage(models.Model):
    """
    Registra cada fase del flujo con su timestamp de inicio y fin.
    Esencial para el cálculo de Lead Times por área.

    NOTA: Para tickets SOLO_ALMACEN, la etapa CALIDAD_INSPECCION nunca se crea.
    La salida de Vigilancia se habilita cuando ALMACEN_RECEPCION está completa.
    """
    ETAPAS = [
        ('VIGILANCIA_ENTRADA', 'Vigilancia - Ingreso'),
        ('ALMACEN_RECEPCION',  'Almacén - Recepción'),
        ('CALIDAD_INSPECCION', 'Calidad - Inspección'),
        ('VIGILANCIA_SALIDA',  'Vigilancia - Salida'),
    ]

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name='stages')
    etapa = models.CharField(max_length=30, choices=ETAPAS)
    fecha_inicio = models.DateTimeField(auto_now_add=True)
    fecha_fin = models.DateTimeField(null=True, blank=True)
    usuario = models.ForeignKey(
        User, on_delete=models.PROTECT,
        help_text="Responsable que ejecutó/cerró la etapa"
    )

    def __str__(self):
        return f"Ticket {self.ticket_id} - {self.etapa}"


class TicketLineInspection(models.Model):
    """
    Registro de inspección por línea de OC en cada etapa del flujo.

    Ciclo de vida de registros:
      1. VIGILANCIA  → esqueleto creado al autorizar ingreso (estado=PENDIENTE)
      2. ALMACEN     → Almacén completa la recepción física
      3. CALIDAD     → Solo si ticket.tipo_flujo == 'CON_CALIDAD' y línea.requiere_coa=True

    El campo coa_url almacena el link de OneDrive del COA subido por el proveedor
    (Fase 3 del flujo). Si la línea no requiere COA, el campo queda vacío.
    """
    ticket = models.ForeignKey('Ticket', on_delete=models.CASCADE, related_name='inspections')
    po_line = models.ForeignKey(PurchaseOrderLine, on_delete=models.PROTECT)

    # NUEVO CAMPO: Referencia directa al número de SAP
    doc_num = models.IntegerField(help_text="Número de la Orden de Compra de SAP (ej. 16089783).")

    ETAPA_CHOICES = [
        ('VIGILANCIA', 'Vigilancia'),
        ('ALMACEN',    'Almacén'),
        ('CALIDAD',    'Calidad'),
    ]
    etapa = models.CharField(max_length=20, choices=ETAPA_CHOICES)
    usuario = models.ForeignKey(User, on_delete=models.PROTECT)

    ESTADO_CHOICES = [
        ('PENDIENTE',   'Pendiente'),
        ('EN_PROCESO',  'En Proceso'),
        ('CONFORME',    'Conforme'),
        ('RECHAZADO',   'Rechazado'),
    ]
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='PENDIENTE')

    # Cantidades: SAP es inmutable (solo referencia), modificada es la real recibida
    cantidad_sap = models.DecimalField(max_digits=18, decimal_places=4, editable=False)
    cantidad_modificada = models.DecimalField(max_digits=18, decimal_places=4)

    # Copia local de si esta línea requería COA al momento de la inspección
    # (refleja PurchaseOrderLine.requiere_coa ajustado por Compras)
    requiere_coa = models.BooleanField(
        default=False,
        help_text="Copia del flag requiere_coa de la línea de OC al momento de crear la inspección."
    )

    # URL del COA en OneDrive (Fase 3: el proveedor sube el archivo)
    coa_url = models.URLField(
        max_length=500,
        blank=True,
        null=True,
        help_text="Link de acceso al COA en OneDrive M365. Se guarda tras la carga del proveedor."
    )

    comentario = models.TextField(blank=True, null=True)
    evidencia_url = models.FileField(
        upload_to='evidencias/inspeccion/%Y/%m/%d/',
        null=True, blank=True
    )
    fecha_registro = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Un registro único por Ticket, Línea de OC y Etapa
        unique_together = ('ticket', 'po_line', 'etapa')

    def __str__(self):
        return (
            f"Ticket {self.ticket_id} | OC {self.po_line.purchase_order.doc_num} "
            f"L{self.po_line.line_num} | {self.etapa}"
        )


class TicketLineCOA(models.Model):
    """
    Certificado de Análisis (COA) cargado por el proveedor para una línea de OC.

    Independiente de las etapas operativas (TicketLineInspection): se crea/actualiza
    cuando el proveedor sube el COA en el portal (Fase 3), típicamente mientras el
    Ticket sigue en PROGRAMADO, antes de que Vigilancia autorice el ingreso a planta.

    Vigilancia y el resto del flujo consultan esta tabla (no TicketLineInspection)
    para validar si el COA de una línea ya fue cargado.
    """
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name='coas')
    po_line = models.ForeignKey(PurchaseOrderLine, on_delete=models.PROTECT)

    coa_url = models.URLField(
        max_length=500,
        help_text="Link de acceso al COA en OneDrive M365."
    )
    evidencia_url = models.FileField(
        upload_to='documentos/coa_lineas/%Y/%m/%d/',
        null=True, blank=True,
        help_text="Archivo local del COA, si aplica (independiente del link de OneDrive)."
    )
    subido_por = models.ForeignKey(
        User, on_delete=models.PROTECT,
        help_text="Usuario que cargó el COA (normalmente el proveedor)."
    )
    fecha_carga = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('ticket', 'po_line')
        verbose_name = "COA de Línea"
        verbose_name_plural = "COAs de Línea"

    def __str__(self):
        return f"COA Ticket {self.ticket_id} | OC {self.po_line.purchase_order.doc_num} L{self.po_line.line_num}"


class TicketDatosIngreso(models.Model):
    """
    Placa del vehículo y datos del conductor para la visita, completados por
    el proveedor. Independiente de las etapas operativas — mismo principio
    que TicketLineCOA (Fase 1): un solo registro por Ticket (no por línea,
    a diferencia del COA), que el proveedor puede crear/corregir en
    cualquier momento mientras el Ticket sigue PROGRAMADO.

    Vigilancia los consulta como parte de su verificación previa a
    autorizar el ingreso (ver detalle_ticket.html), pero NO los edita, y el
    sistema NO bloquea la autorización de ingreso si este registro no
    existe o está incompleto (decisión de negocio explícita, sesión 13:
    informativo, no bloqueante).
    """
    ticket = models.OneToOneField(
        Ticket, on_delete=models.CASCADE, related_name='datos_ingreso'
    )

    placa_vehiculo = models.CharField(max_length=15, blank=True)
    conductor_dni = models.CharField(
        max_length=20, blank=True,
        verbose_name="DNI/CE del conductor",
        help_text="DNI o Carné de Extranjería del conductor."
    )
    conductor_nombre = models.CharField(
        max_length=150, blank=True,
        verbose_name="Nombre y apellidos del conductor"
    )

    actualizado_por = models.ForeignKey(
        User, on_delete=models.PROTECT,
        help_text="Usuario que registró/corrigió estos datos (normalmente el proveedor)."
    )
    fecha_actualizacion = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Datos de Ingreso"
        verbose_name_plural = "Datos de Ingreso"

    def __str__(self):
        return f"Datos de ingreso Ticket {self.ticket_id}"


class EntradaMercaderia(models.Model):
    """
    Entrada de Mercadería (Goods Receipt PO) para SAP. Se genera
    automáticamente cuando Vigilancia registra la salida (Ticket →
    FINALIZADO — ver OperationsService.registrar_salida), pero nace en
    estado PENDIENTE: Almacén / Materia Prima todavía debe completar el
    número de LOTE por línea (y opcionalmente fechas de vencimiento/
    fabricación) en el Portal antes de enviarla al daemon (sesión 99 —
    ver apps/operations/services_entrada.py). Un solo registro por Ticket,
    con sus líneas en EntradaMercaderiaLinea.

    DOS campos de estado, mismo patrón que Factura (estado de negocio +
    estado de sincronización), a propósito separados:

      estado (negocio, esta sesión):
        PENDIENTE   Recién generada al finalizar el Ticket — Almacén/MP
                    la está preparando en el Portal (falta el LOTE).
        ENVIADO     Almacén/MP la envió (services_entrada.enviar_a_sap):
                    LOTE completo en todas las líneas, estado_sap='L', el
                    daemon la creará en SAP B1 en el próximo ciclo.
        CREADO_SAP  GRPO real creado en SAP B1 (doc_entry_definitivo /
                    doc_num_sap ya asignados por el daemon) — fin del ciclo.

      estado_sap (sincronización con SAP, SIN CAMBIOS — los choices se
      preservan porque el serializer/DTO del daemon dependen de ellos):
        ''  Todavía no enviada al daemon (estado=PENDIENTE).
        'L' Enviada, pendiente de que el daemon cree el GRPO en SAP.
        'B' confirmar-borrador ya llamado por el daemon.
        'Y' confirmar-definitivo ya llamado por el daemon — GRPO real listo.

    Mapa estado ↔ estado_sap: PENDIENTE↔'', ENVIADO↔{'L','B'},
    CREADO_SAP↔'Y'. El daemon sigue el MISMO patrón de upsert retry-safe
    ya usado para la sincronización de OC (apps/sap_sync/serializers.py,
    sesión 49): puede reintentar cualquiera de los 2 POST de confirmación
    sin que un reintento accidental rompa nada.
    """
    ESTADO_SAP_CHOICES = [
        ('', 'Sin generar'),
        ('L', 'Pendiente de confirmar en SAP'),
        ('B', 'Borrador confirmado en SAP'),
        ('Y', 'Definitivo confirmado en SAP'),
    ]

    ESTADO_PENDIENTE = 'PENDIENTE'
    ESTADO_ENVIADO = 'ENVIADO'
    ESTADO_CREADO_SAP = 'CREADO_SAP'
    ESTADO_CHOICES = [
        (ESTADO_PENDIENTE, 'Pendiente (falta completar el lote en el Portal)'),
        (ESTADO_ENVIADO, 'Enviado a SAP B1'),
        (ESTADO_CREADO_SAP, 'Creado en SAP B1'),
    ]

    ticket = models.OneToOneField(
        Ticket, on_delete=models.CASCADE, related_name='entrada_mercaderia'
    )
    estado = models.CharField(
        max_length=12, choices=ESTADO_CHOICES, default=ESTADO_PENDIENTE,
        help_text="Estado de negocio: PENDIENTE → ENVIADO → CREADO_SAP (ver docstring del modelo)."
    )
    estado_sap = models.CharField(
        max_length=1, choices=ESTADO_SAP_CHOICES, default='', blank=True
    )
    doc_entry_borrador = models.IntegerField(
        null=True, blank=True,
        help_text="DocEntry del borrador en SAP, asignado por el daemon al confirmar (estado_sap='B')."
    )
    doc_entry_definitivo = models.IntegerField(
        null=True, blank=True,
        help_text="DocEntry del documento definitivo en SAP, asignado por el daemon (estado_sap='Y')."
    )
    doc_num_sap = models.CharField(
        max_length=30, null=True, blank=True,
        help_text="DocNum visible del GRPO en SAP B1 (número humano), devuelto por el daemon junto con el DocEntry."
    )
    error_mensaje = models.TextField(
        blank=True, default='',
        help_text="Mensaje de error reportado por el daemon si SAP rechaza la entrada (diagnóstico)."
    )

    # Timestamps por transición — fecha_generada coincide con la creación del
    # registro (siempre se crea directamente en estado=PENDIENTE / estado_sap='').
    fecha_generada = models.DateTimeField(auto_now_add=True)
    fecha_borrador_confirmado = models.DateTimeField(null=True, blank=True)
    fecha_definitivo_confirmado = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Entrada de Mercadería"
        verbose_name_plural = "Entradas de Mercadería"

    def __str__(self):
        return f"Entrada Mercadería Ticket {self.ticket_id} [{self.estado}]"


class EntradaMercaderiaLinea(models.Model):
    """
    Línea de la Entrada de Mercadería — una por PurchaseOrderLine recibida.
    `cantidad` es la cantidad REAL final inspeccionada (la fila más
    reciente de TicketLineInspection por línea, sea cual sea la etapa que
    la generó — mismo criterio que OperationsService.get_estado_actual_
    por_oc, sesión 6/9), NUNCA PurchaseOrderLine.quantity_sap.

    po_line usa on_delete=PROTECT, mismo criterio que TicketLineInspection/
    TicketLineCOA (sesión 49): una línea de OC nunca se borra físicamente
    (solo se desactiva, PurchaseOrderLine.activa=False), así que esta FK
    nunca debería toparse con una eliminación real — protegida de todos
    modos por consistencia con el resto del proyecto.
    """
    entrada = models.ForeignKey(
        EntradaMercaderia, on_delete=models.CASCADE, related_name='lineas'
    )
    po_line = models.ForeignKey(PurchaseOrderLine, on_delete=models.PROTECT)
    cantidad = models.DecimalField(max_digits=18, decimal_places=4)

    # Sesión 99: LOTE del artículo, capturado por Almacén / Materia Prima
    # en el Portal antes de enviar la Entrada al daemon. Regla de negocio
    # confirmada: UN solo lote por línea de OC (nunca varios en la misma
    # línea). El daemon lo envía a SAP como BatchNumbers=[{BatchNumber,
    # Quantity, ExpiryDate?, ManufactureDate?}] SOLO cuando numero_lote no
    # está vacío (un ítem NO gestionado por lote con BatchNumbers también
    # hace que SAP rechace el documento). Las 2 fechas son opcionales: se
    # envían a SAP únicamente si el artículo las exige (materia prima
    # química/alimenticia) y Almacén las completa.
    numero_lote = models.CharField(
        max_length=60, blank=True, default='',
        help_text="Número de lote del artículo recibido (un solo lote por línea)."
    )
    fecha_vencimiento_lote = models.DateField(
        null=True, blank=True,
        help_text="Fecha de vencimiento del lote — opcional, solo si el artículo la requiere en SAP."
    )
    fecha_fabricacion_lote = models.DateField(
        null=True, blank=True,
        help_text="Fecha de fabricación del lote — opcional, solo si el artículo la requiere en SAP."
    )

    class Meta:
        unique_together = ('entrada', 'po_line')
        verbose_name = "Línea de Entrada de Mercadería"
        verbose_name_plural = "Líneas de Entrada de Mercadería"

    def __str__(self):
        return (
            f"Entrada Mercadería Ticket {self.entrada.ticket_id} | "
            f"OC {self.po_line.purchase_order.doc_num} L{self.po_line.line_num} | {self.cantidad}"
        )