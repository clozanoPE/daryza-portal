' Proyecto: daemon_entity / FacturaDTO.vb
'
' Sesión 97, Etapa 5: DTOs para los 3 endpoints de Factura del Portal —
' forma exacta del JSON real (verificada contra apps/invoicing/
' serializers.py, no adivinada — ver detalle por clase abajo), snake_case
' 1:1 con los nombres reales del payload. Mismo patrón que
' PurchaseOrderDTO.vb/EntradaMercaderiaDTO.vb: solo forma de datos, sin
' lógica ni llamadas — daemon_entity no tiene ninguna dependencia de
' otros proyectos ni de Newtonsoft.Json (no hace falta: Newtonsoft
' deserializa por nombre de propiedad público sin ningún atributo).

''' <summary>
''' Una línea del payload de GET facturas-pendientes-preliminar/
''' (FacturaLineaPreliminarSerializer). `cantidad`/`precio` llegan como
''' STRING en el JSON real (DRF serializa DecimalField como string por
''' default, confirmado empíricamente: {"cantidad":"10.0000",
''' "precio":"118.0500",...}) — Newtonsoft.Json coerce ese string a
''' Decimal sin problema al deserializar (mismo mecanismo ya usado y
''' confirmado funcionando en EntradaMercaderiaLineaDTO.cantidad, sesión
''' 57/97). `grpo_doc_entry` es nullable a propósito — el GRPO
''' referenciado puede seguir en 'L'/'B' (no confirmado como
''' `doc_entry_definitivo` todavía) cuando esta línea se consulta; el
''' Portal expone el dato tal cual, sin filtrar (ver docstring del
''' serializer real), así que el daemon debe decidir qué hacer si viene
''' Nothing (ver FacturaService).
''' </summary>
Public Class FacturaLineaPreliminarDTO
    Public Property item_code As String
    Public Property base_line As Integer
    Public Property cantidad As Decimal
    Public Property precio As Decimal
    Public Property grpo_doc_entry As Integer?
    Public Property grpo_estado_sap As String
End Class

''' <summary>
''' Payload completo de GET facturas-pendientes-preliminar/
''' (FacturaPreliminarSerializer) — una Factura con estado=
''' APROBADA_COMPRAS y estado_sap='L', lista para que el daemon cree su
''' Preliminar en SAP. `tax_date`/`doc_due_date` llegan como string ISO
''' 8601 ("YYYY-MM-DD") o null (DateField de Django, formato default de
''' DRF, confirmado empíricamente) — tipados acá como Date? para que
''' Newtonsoft.Json los parsee directo; `tipo_cambio` nullable porque
''' hoy SIEMPRE viaja en null (campo sin ningún consumidor real todavía
''' del lado Django, documentado desde la sesión 93/CLAUDE.md raíz).
''' `id` es Factura.pk — el valor que el daemon debe escribir en el UDF
''' U_MSSL_PORTAL_ID al crear el Preliminar (mecanismo de
''' reconciliación, confirmado sesión 86; ver FacturaService).
''' </summary>
Public Class FacturaPreliminarDTO
    Public Property id As Integer
    Public Property card_code As String
    Public Property sede_codigo As String
    Public Property sede_sap_whs_code As String
    Public Property doc_cur As String
    Public Property tax_date As Date?
    Public Property doc_due_date As Date?
    Public Property num_at_card As String
    Public Property U_MSSL_FPC As String
    Public Property U_MSSL_FNC As String
    Public Property U_MSSL_TOP As String
    Public Property U_MSSL_CBS As Integer
    Public Property tipo_cambio As Decimal?
    Public Property lineas As New List(Of FacturaLineaPreliminarDTO)
End Class

''' <summary>
''' Payload de GET facturas-preliminares/ (FacturaReconciliacionSerializer)
''' — Facturas con estado_sap='B' (Preliminar ya confirmado en SAP),
''' esperando que Contabilidad cree el documento definitivo directamente
''' en SAP (fuera del Portal) y el daemon lo detecte y confirme.
''' `doc_entry_preliminar` es CharField del lado Django (no Integer,
''' a diferencia de EntradaMercaderia.doc_entry_*) — tipado String acá,
''' consistente con el modelo real.
''' </summary>
Public Class FacturaReconciliacionDTO
    Public Property id As Integer
    Public Property card_code As String
    Public Property doc_entry_preliminar As String
    Public Property estado_sap As String
End Class

''' <summary>
''' Payload de GET facturas-pendientes-cancelacion/
''' (FacturaCancelacionSerializer) — Facturas con estado=CANCELADO y
''' estado_sap='B' (ya tenían un Preliminar en SAP que ahora hay que
''' anular). Misma forma exacta que FacturaReconciliacionDTO del lado
''' Django (mismos 4 campos) — DTOs separados igual, uno por endpoint,
''' mismo criterio ya establecido en el resto del proyecto (un DTO por
''' contrato de API, aunque 2 contratos coincidan hoy en forma).
''' </summary>
Public Class FacturaCancelacionDTO
    Public Property id As Integer
    Public Property card_code As String
    Public Property doc_entry_preliminar As String
    Public Property estado_sap As String
End Class
