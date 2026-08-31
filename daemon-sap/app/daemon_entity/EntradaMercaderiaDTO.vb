' Entities/EntradaMercaderiaDTO.vb
'
' Sesión 97, Etapa 4 (Sub-etapa 4.1): forma exacta del JSON que devuelve
' GET /api/v1/entradas-pendientes/ del Portal — nombres de propiedad en
' snake_case coincidiendo 1:1 con EntradaMercaderiaSerializer/
' EntradaMercaderiaLineaSerializer del lado Django (apps/operations/
' serializers.py), mismo criterio ya usado por PurchaseOrderDTO.vb.
'
' Pura forma de datos, sin ninguna lógica — igual que el resto de este
' archivo (daemon_entity no tiene ninguna dependencia de otros
' proyectos, y así se mantiene).
Public Class EntradaMercaderiaDTO
    Public Property id As Integer
    Public Property ticket_id As Integer
    Public Property card_code As String
    Public Property card_name As String
    Public Property estado_sap As String
    Public Property lineas As New List(Of EntradaMercaderiaLineaDTO)
End Class

Public Class EntradaMercaderiaLineaDTO
    ' doc_entry_oc/base_line son exactamente lo que Service Layer
    ' necesita para "Copy From" la OC original al crear el GRPO
    ' (BaseType=22/BaseEntry/BaseLine) — ver GrpoService.vb (daemon_data).
    Public Property doc_entry_oc As Integer
    Public Property doc_num_oc As Integer
    Public Property base_line As Integer
    Public Property item_code As String
    Public Property cantidad As Decimal

    ' Sesión 99: LOTE del artículo, completado por Almacén / Materia Prima
    ' en el Portal antes de que la Entrada quede disponible para el daemon
    ' (estado_sap='L'). numero_lote llega SIEMPRE poblado en las entradas
    ' que este endpoint devuelve (el Portal valida que todas las líneas lo
    ' tengan antes de permitir el envío — services_entrada.enviar_a_sap).
    ' Las 2 fechas son opcionales (null si el artículo no las requiere).
    Public Property numero_lote As String
    Public Property fecha_vencimiento_lote As Date?
    Public Property fecha_fabricacion_lote As Date?
End Class
