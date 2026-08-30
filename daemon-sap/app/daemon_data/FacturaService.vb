' DAL (Data Access Layer) / FacturaService.vb
'
' Sesión 97, Etapa 5: creación/reconciliación/cancelación del Preliminar
' de Factura en SAP vía Service Layer. Vive en daemon_data, junto a
' GrpoService.vb, mismo criterio exacto de esa clase: la llamada real a
' Service Layer (POST /Drafts, GET /PurchaseInvoices?$filter=...,
' DELETE /Drafts(id)) vive acá, nunca en la orquestación.
'
' Consume SapServiceLayerClient por composición (recibe la instancia ya
' lista por constructor, la misma sesión de todo el ciclo) — nunca lo
' extiende. Deliberadamente sin referencia de proyecto a daemon_entity,
' mismo criterio ya establecido por GrpoService: los tipos de parámetro
' de este archivo (FacturaPreliminarCabecera/FacturaPreliminarLinea) son
' propios y mínimos, el mapeo desde los DTOs reales del Portal
' (FacturaPreliminarDTO/FacturaLineaPreliminarDTO, daemon_entity) es
' responsabilidad exclusiva de la capa de orquestación (FacturaSyncService,
' daemon_logical) — único punto del proyecto donde ambos vocabularios se
' cruzan.
'
' Nunca lanza una excepción sin capturar — mismo criterio que
' GrpoService/SapServiceLayerClient: cualquier fallo (SAP rechaza el
' documento, error de red, respuesta no interpretable) se traduce a un
' resultado con Exitoso/Encontrado=False y el mensaje REAL, nunca
' inventado. Tampoco loggea directamente (mismo motivo que las otras 2
' clases de esta capa: Logger vive en daemon_logical) — el llamador
' decide qué loggear.
Imports Newtonsoft.Json
Imports Newtonsoft.Json.Linq

''' <summary>
''' Una línea del Preliminar a crear — BaseEntry es el DocEntry REAL del
''' GRPO ya confirmado (doc_entry_definitivo del lado Portal, nunca el
''' borrador) del cual se copia esta línea. Cantidad/Precio son los
''' valores REALES que el proveedor facturó (FacturaLinea.cantidad/
''' precio), no necesariamente el total recibido en el GRPO — de ahí que
''' se envíen explícitos en vez de dejar que SAP copie la cantidad
''' completa del GRPO por default.
''' </summary>
Public Class FacturaPreliminarLinea
    Public Property BaseEntry As Integer
    Public Property BaseLine As Integer
    Public Property Cantidad As Decimal
    Public Property Precio As Decimal
End Class

''' <summary>
''' Datos de cabecera del Preliminar a crear — un tipo propio de esta
''' capa (no el DTO del Portal), mismo criterio que GrpoLineaEntrada en
''' GrpoService.vb. Nombres de propiedad en PascalCase .NET normal (a
''' diferencia de los DTOs del Portal, que son snake_case a propósito
''' para calzar 1:1 con el JSON de Django) — esta clase ya vive del
''' lado SAP del cruce de vocabularios.
''' </summary>
Public Class FacturaPreliminarCabecera
    Public Property CardCode As String
    Public Property NumAtCard As String
    Public Property DocCurrency As String
    Public Property TaxDate As Date?
    Public Property DocDueDate As Date?
    Public Property UdfFPC As String
    Public Property UdfFNC As String
    Public Property UdfTOP As String
    Public Property UdfCBS As Integer
    ''' <summary>
    ''' Factura.id del Portal — se escribe tal cual en el UDF
    ''' U_MSSL_PORTAL_ID al crear el documento (mecanismo de
    ''' reconciliación, confirmado sesión 86). Enviado como STRING en el
    ''' JSON (ToString() en CrearPreliminarAsync) — decisión explícita,
    ''' no confirmada contra el tipo real de dato del UDF en SAP (podría
    ''' ser Alfanumérico o Numérico; Alfanumérico es el default más
    ''' común para un UDF custom no declarado explícitamente como
    ''' Numérico). Si la prueba integral real revela que SAP lo rechaza
    ''' por tipo, es el primer punto a ajustar — ver nota en
    ''' ARQUITECTURA_SAP.md sección 5.
    ''' </summary>
    Public Property PortalId As Integer
End Class

Public Class FacturaPreliminarResult
    Public Property Exitoso As Boolean
    Public Property DocEntry As Integer?
    Public Property MensajeError As String
End Class

''' <summary>
''' Resultado de buscar el documento definitivo en SAP (OPCH) por
''' U_MSSL_PORTAL_ID — Encontrado=False sin MensajeError significa
''' "todavía no existe en SAP" (el caso normal, esperado, mientras
''' Contabilidad no lo haya creado — NO es un error). Encontrado=False
''' CON MensajeError sí es un fallo real (SAP no respondió, respuesta
''' no interpretable).
''' </summary>
Public Class FacturaReconciliacionResult
    Public Property Encontrado As Boolean
    Public Property DocEntry As Integer?
    Public Property MensajeError As String
End Class

Public Class FacturaCancelacionResult
    Public Property Exitoso As Boolean
    Public Property MensajeError As String
End Class

Public Class FacturaService
    Private ReadOnly _cliente As SapServiceLayerClient

    Public Sub New(cliente As SapServiceLayerClient)
        _cliente = cliente
    End Sub

    ''' <summary>
    ''' Crea el Preliminar (Draft) de Factura en SAP — POST /Drafts con
    ''' DocObjectCode="oPurchaseInvoices" (confirmado contra documentación
    ''' real de Service Layer, no asumido — ver ARQUITECTURA_SAP.md
    ''' sección 5), "Copy From" cada línea del GRPO correspondiente.
    '''
    ''' BaseType=20 (Purchase Delivery Note / GRPO), NO 22 (Purchase
    ''' Order) — confirmado empíricamente contra un ejemplo real de
    ''' Service Layer (POST /PurchaseInvoices con BaseType 20 y
    ''' BaseEntry igual al DocEntry del GRPO) antes de escribir este
    ''' código, no asumido por analogía con GrpoService (que copia de
    ''' la OC, BaseType=22 — un origen distinto). Este es el dato más
    ''' importante de sanity check de toda la Etapa 5.
    '''
    ''' Precondición NO validada acá (responsabilidad del llamador, ver
    ''' docstring del módulo): cada línea debe tener un BaseEntry real
    ''' (el GRPO correspondiente ya confirmado como doc_entry_definitivo
    ''' del lado Portal) — esta función asume que `lineas` ya viene
    ''' filtrada/validada, no comprueba nada acá.
    ''' </summary>
    Public Async Function CrearPreliminarAsync(
        cabecera As FacturaPreliminarCabecera,
        lineas As List(Of FacturaPreliminarLinea)
    ) As Task(Of FacturaPreliminarResult)
        Dim documentLines = lineas.Select(
            Function(l) New Dictionary(Of String, Object) From {
                {"BaseType", 20},
                {"BaseEntry", l.BaseEntry},
                {"BaseLine", l.BaseLine},
                {"Quantity", l.Cantidad},
                {"UnitPrice", l.Precio}
            }
        ).ToList()

        Dim body As New Dictionary(Of String, Object) From {
            {"DocObjectCode", "oPurchaseInvoices"},
            {"CardCode", cabecera.CardCode},
            {"NumAtCard", cabecera.NumAtCard},
            {"DocCurrency", cabecera.DocCurrency},
            {"U_MSSL_FPC", cabecera.UdfFPC},
            {"U_MSSL_FNC", cabecera.UdfFNC},
            {"U_MSSL_TOP", cabecera.UdfTOP},
            {"U_MSSL_CBS", cabecera.UdfCBS},
            {"U_MSSL_PORTAL_ID", cabecera.PortalId.ToString()},
            {"DocumentLines", documentLines}
        }
        ' TaxDate/DocDueDate son opcionales del lado Portal (nullable) —
        ' se omiten del payload por completo si no vienen, en vez de
        ' mandar "TaxDate": null (más seguro: deja que SAP aplique su
        ' propio default, en vez de forzar un null explícito que algunas
        ' instalaciones de Service Layer podrían rechazar en un campo
        ' de fecha).
        If cabecera.TaxDate.HasValue Then
            body("TaxDate") = cabecera.TaxDate.Value.ToString("yyyy-MM-dd")
        End If
        If cabecera.DocDueDate.HasValue Then
            body("DocDueDate") = cabecera.DocDueDate.Value.ToString("yyyy-MM-dd")
        End If

        Dim bodyJson = JsonConvert.SerializeObject(body)
        Dim resultado = Await _cliente.PostAsync("Drafts", bodyJson)

        If Not resultado.Exitoso Then
            Return New FacturaPreliminarResult With {
                .Exitoso = False,
                .MensajeError = resultado.Mensaje
            }
        End If

        Try
            Dim json = JsonConvert.DeserializeObject(Of Dictionary(Of String, Object))(resultado.Contenido)
            If json.ContainsKey("DocEntry") Then
                Return New FacturaPreliminarResult With {
                    .Exitoso = True,
                    .DocEntry = Convert.ToInt32(json("DocEntry"))
                }
            End If
            Return New FacturaPreliminarResult With {
                .Exitoso = False,
                .MensajeError = $"SAP respondió {resultado.StatusCode} pero sin DocEntry reconocible en el body: {resultado.Contenido}"
            }
        Catch ex As Exception
            Return New FacturaPreliminarResult With {
                .Exitoso = False,
                .MensajeError = $"SAP respondió {resultado.StatusCode} pero el body no se pudo interpretar: {ex.Message} - {resultado.Contenido}"
            }
        End Try
    End Function

    ''' <summary>
    ''' Busca en SAP (OPCH, colección PurchaseInvoices — el documento
    ''' DEFINITIVO real, nunca /Drafts) el documento cuyo UDF
    ''' U_MSSL_PORTAL_ID coincida con el Factura.id del Portal — filtro
    ''' OData ($filter=U_MSSL_PORTAL_ID eq '{portalId}'), $select=DocEntry
    ''' para no traer el documento completo. Tratado como Alfanumérico
    ''' (valor entre comillas simples en el filtro) — mismo criterio y
    ''' misma incertidumbre ya documentada en FacturaPreliminarCabecera.
    ''' PortalId.
    ''' </summary>
    Public Async Function BuscarDefinitivoAsync(portalId As Integer) As Task(Of FacturaReconciliacionResult)
        Dim filtro = $"U_MSSL_PORTAL_ID eq '{portalId}'"
        Dim url = $"PurchaseInvoices?$filter={Uri.EscapeDataString(filtro)}&$select=DocEntry"
        Dim resultado = Await _cliente.GetAsync(url)

        If Not resultado.Exitoso Then
            Return New FacturaReconciliacionResult With {
                .Encontrado = False,
                .MensajeError = resultado.Mensaje
            }
        End If

        Try
            Dim json = JsonConvert.DeserializeObject(Of JObject)(resultado.Contenido)
            Dim valores = TryCast(json("value"), JArray)
            If valores IsNot Nothing AndAlso valores.Count > 0 Then
                Dim docEntry = valores(0)("DocEntry").ToObject(Of Integer)()
                Return New FacturaReconciliacionResult With {
                    .Encontrado = True,
                    .DocEntry = docEntry
                }
            End If
            ' Consulta exitosa, sin resultados — SAP todavía no tiene el
            ' documento definitivo. Caso normal, NO es un error.
            Return New FacturaReconciliacionResult With {.Encontrado = False}
        Catch ex As Exception
            Return New FacturaReconciliacionResult With {
                .Encontrado = False,
                .MensajeError = $"Respuesta de SAP no se pudo interpretar: {ex.Message} - {resultado.Contenido}"
            }
        End Try
    End Function

    ''' <summary>
    ''' Anula el Preliminar en SAP — DELETE /Drafts(DocEntry). Un Draft
    ''' (a diferencia de un documento real ya posteado) se anula
    ''' borrándolo directamente, no vía un endpoint de "Cancel" (ese
    ''' patrón aplica a documentos ya confirmados, no a borradores).
    ''' </summary>
    Public Async Function CancelarPreliminarAsync(docEntryPreliminar As Integer) As Task(Of FacturaCancelacionResult)
        Dim resultado = Await _cliente.DeleteAsync($"Drafts({docEntryPreliminar})")
        If resultado.Exitoso Then
            Return New FacturaCancelacionResult With {.Exitoso = True}
        End If
        Return New FacturaCancelacionResult With {
            .Exitoso = False,
            .MensajeError = resultado.Mensaje
        }
    End Function
End Class
