' Entities/PurchaseOrderDTO.vb
Public Class PurchaseOrderDTO
    Public Property doc_entry As Integer
    Public Property doc_num As Integer
    Public Property card_code As String
    Public Property card_name As String
    Public Property e_mail As String
    Public Property status As String
    Public Property u_mss_tdb As String

    ' Sesión 93: DocCur de SAP — moneda del documento (texto directo,
    ' ej. 'USD'/'SOL'), sin mapeo a ningún catálogo. Mismo nombre
    ' snake_case que espera PurchaseOrderSerializer del lado Django.
    Public Property doc_cur As String

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

    ' Sesión 99b: OITM.ManBtchNum de SAP — True si el artículo se gestiona
    ' por lote. El Portal lo usa para exigir el número de lote SOLO en las
    ' líneas que lo requieren (pantalla de Entrada de Mercadería). Mismo
    ' nombre snake_case que espera PurchaseOrderLineSerializer.
    Public Property gestionado_por_lote As Boolean
End Class
