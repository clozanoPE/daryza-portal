' DAL (Data Access Layer) / GrpoService.vb
'
' Sesión 97, Etapa 4 (Sub-etapas 4.1/4.2/4.4): creación real del GRPO
' (Entrada de Mercancía) en SAP vía Service Layer — "Copy From" la OC
' original, flujo de un solo paso (confirmado, sesión 86).
'
' Vive en daemon_data, junto a SapServiceLayerClient — pedido
' explícito de que la llamada a POST /PurchaseDeliveryNotes viva en la
' capa de conexión a SAP, no dispersa en la orquestación. Esta clase
' CONSUME SapServiceLayerClient (composición, no herencia — recibe una
' instancia ya lista por el constructor, la misma que la orquestación
' usa para todo el ciclo, respetando "una sola sesión por ciclo") y le
' agrega el conocimiento específico de "cómo se ve un pedido de
' creación de GRPO" — SapServiceLayerClient en sí sigue sin saber nada
' de esto (genérico, reutilizable para Etapa 5 igual).
'
' Nunca lanza una excepción sin capturar — mismo criterio que
' SapServiceLayerClient: cualquier fallo (SAP rechaza el documento,
' error de red) se traduce a GrpoResult.Exitoso=False con el mensaje
' de error REAL de SAP, nunca inventado. Tampoco loggea directamente
' (mismo motivo que SapServiceLayerClient: evitar la dependencia
' circular con Logger, que vive en daemon_logical) — el llamador
' decide qué loggear.
Imports Newtonsoft.Json

Public Class GrpoLineaEntrada
    Public Property BaseEntry As Integer
    Public Property BaseLine As Integer
    Public Property Cantidad As Decimal
    Public Property ItemCode As String

    ' Sesión 99: LOTE. Si NumeroLote no está vacío, la línea del payload
    ' lleva BatchNumbers=[{BatchNumber, Quantity, ExpiryDate?}]. Si está
    ' vacío, NO se agrega BatchNumbers (un ítem NO gestionado por lote
    ' con BatchNumbers también hace que SAP rechace el documento).
    '
    ' Sesión 99c: ManufactureDate NO se envía — la config de lotes de
    ' BK_1808261700 no tiene "Fecha de fabricación" como atributo de
    ' lote habilitado, así que cualquier BatchNumbers que incluya
    ' ManufactureDate lo rechaza con -4014 ("Cannot add row without
    ' complete selection of batch/serial numbers"). ExpiryDate SÍ se
    ' acepta. FechaFabricacionLote se sigue recibiendo del Portal (para
    ' no romper el mapeo del DTO) pero se ignora al armar el payload.
    Public Property NumeroLote As String
    Public Property FechaVencimientoLote As Date?
    Public Property FechaFabricacionLote As Date?
End Class

Public Class GrpoResult
    Public Property Exitoso As Boolean
    Public Property DocEntry As Integer?
    ' DocNum visible del GRPO en SAP B1 (número humano). Se obtiene de la
    ' MISMA respuesta del POST /PurchaseDeliveryNotes — no hace falta
    ' buscarlo después (a diferencia de la reconciliación de Factura).
    Public Property DocNum As Integer?
    Public Property MensajeError As String
End Class

Public Class GrpoService
    Private ReadOnly _cliente As SapServiceLayerClient

    Public Sub New(cliente As SapServiceLayerClient)
        _cliente = cliente
    End Sub

    ''' <summary>
    ''' Crea el GRPO real en SAP, "Copy From" cada línea de la OC
    ''' original — BaseType=22 (Purchase Order) fijo, BaseEntry/BaseLine
    ''' identifican exactamente qué línea de qué OC se está recibiendo
    ''' (confirmado, sesión 86: el patrón estándar de SAP para este
    ''' "Copy From"). Devuelve el DocEntry real que SAP le asignó al
    ''' documento si lo acepta, o el mensaje de error real de SAP si lo
    ''' rechaza — nunca inventa ninguno de los dos.
    ''' </summary>
    Public Async Function CrearGrpoAsync(cardCode As String, lineas As List(Of GrpoLineaEntrada)) As Task(Of GrpoResult)
        Dim documentLines As New List(Of Dictionary(Of String, Object))()
        For Each l In lineas
            Dim linea As New Dictionary(Of String, Object) From {
                {"BaseType", 22},
                {"BaseEntry", l.BaseEntry},
                {"BaseLine", l.BaseLine},
                {"Quantity", l.Cantidad}
            }

            ' Sesión 99: BatchNumbers SOLO si la línea trae lote — un ítem
            ' NO gestionado por lote con BatchNumbers también hace que SAP
            ' rechace el documento (ya documentado en ARQUITECTURA_SAP.md).
            '
            ' Sesión 99c: se envía BatchNumber + Quantity (+ ExpiryDate si
            ' hay). ManufactureDate NO se envía — BK_1808261700 lo rechaza
            ' con -4014 (ver docstring de GrpoLineaEntrada). El dato sigue
            ' guardado en el Portal, solo no se propaga a SAP.
            If Not String.IsNullOrWhiteSpace(l.NumeroLote) Then
                Dim lote As New Dictionary(Of String, Object) From {
                    {"BatchNumber", l.NumeroLote},
                    {"Quantity", l.Cantidad}
                }
                If l.FechaVencimientoLote.HasValue Then
                    lote("ExpiryDate") = l.FechaVencimientoLote.Value.ToString("yyyy-MM-dd")
                End If
                linea("BatchNumbers") = New List(Of Object) From {lote}
            End If

            documentLines.Add(linea)
        Next

        Dim body = JsonConvert.SerializeObject(New With {
            .CardCode = cardCode,
            .DocumentLines = documentLines
        })

        Dim resultado = Await _cliente.PostAsync("PurchaseDeliveryNotes", body)

        If Not resultado.Exitoso Then
            Return New GrpoResult With {
                .Exitoso = False,
                .MensajeError = resultado.Mensaje
            }
        End If

        Try
            Dim json = JsonConvert.DeserializeObject(Of Dictionary(Of String, Object))(resultado.Contenido)
            If json.ContainsKey("DocEntry") Then
                Dim r As New GrpoResult With {
                    .Exitoso = True,
                    .DocEntry = Convert.ToInt32(json("DocEntry"))
                }
                ' DocNum viene en la misma respuesta — opcional, no
                ' invalida el éxito si por algún motivo faltara.
                If json.ContainsKey("DocNum") AndAlso json("DocNum") IsNot Nothing Then
                    Dim docNumParsed As Integer
                    If Integer.TryParse(json("DocNum").ToString(), docNumParsed) Then
                        r.DocNum = docNumParsed
                    End If
                End If
                Return r
            End If
            Return New GrpoResult With {
                .Exitoso = False,
                .MensajeError = $"SAP respondió {resultado.StatusCode} pero sin DocEntry reconocible en el body: {resultado.Contenido}"
            }
        Catch ex As Exception
            Return New GrpoResult With {
                .Exitoso = False,
                .MensajeError = $"SAP respondió {resultado.StatusCode} pero el body no se pudo interpretar: {ex.Message} - {resultado.Contenido}"
            }
        End Try
    End Function
End Class
