' Entities/PurchaseOrderDTO.vb
Public Class PurchaseOrderDTO
    Public Property doc_entry As Integer
    Public Property doc_num As Integer
    Public Property card_code As String
    Public Property card_name As String
    Public Property e_mail As String
    Public Property status As String
    Public Property u_mss_tdb As String
    Public Property lines As New List(Of PurchaseOrderLineDTO)
End Class

Public Class PurchaseOrderLineDTO
    Public Property line_num As Integer
    Public Property item_code As String
    Public Property description As String
    Public Property quantity_sap As Decimal
    Public Property und_medida As String
    Public Property requiere_coa As Boolean

    ' Sesión 92: precio unitario (PriceBefDi de SAP), total NETO de
    ' línea sin IGV (LineTotal de SAP), y código de impuesto (TaxCode de
    ' SAP: 'IGV' gravado / 'IGV_EXE' exonerado) — datos reales de SAP,
    ' no calculados por el demonio. Nombres de propiedad en snake_case
    ' para que coincidan tal cual con los campos que espera
    ' PurchaseOrderLineSerializer del lado Django (apps/sap_sync/
    ' serializers.py), igual criterio que el resto de este DTO.
    Public Property precio_unitario As Decimal
    Public Property precio_total_linea As Decimal
    Public Property tax_code As String
End Class
