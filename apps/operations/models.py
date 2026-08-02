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

    def close_ticket(self):
        """Calcula y guarda el tiempo total en planta al finalizar."""
        if self.estado == 'FINALIZADO':
            primera_etapa = self.stages.order_by('fecha_inicio').first()
            ultima_etapa = self.stages.order_by('-fecha_fin').first()
            if primera_etapa and ultima_etapa and ultima_etapa.fecha_fin:
                self.tiempo_total_planta = ultima_etapa.fecha_fin - primera_etapa.fecha_inicio
                self.save(update_fields=['tiempo_total_planta'])

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

    @property
    def duracion(self):
        if self.fecha_fin and self.fecha_inicio:
            return self.fecha_fin - self.fecha_inicio
        return None

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