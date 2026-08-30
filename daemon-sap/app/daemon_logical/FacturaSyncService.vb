' Proyecto: daemon_logical / FacturaSyncService.vb
'
' Sesión 97, Etapa 5: orquesta los 3 ciclos del Preliminar de Factura —
' creación (5.1/5.2), reconciliación (5.3) y cancelación (5.4). Solo
' decide CUÁNDO y CON QUÉ DATOS llamar a cada capa de abajo — nunca
' arma el payload SAP ni parsea su respuesta HTTP directamente (eso vive
' en FacturaService.vb, daemon_data — ver su docstring). El consumo del
' Portal (GET .../POST confirmar-*/reportar-error) sigue el MISMO
' patrón HttpClient+Token+reintento-con-backoff ya usado por
' SyncService.vb (sync-oc) y GrpoSyncService.vb (GRPO) — no un
' mecanismo distinto; el helper de reintento se replica acá igual que
' ya se replicó en GrpoSyncService (mismo criterio ya establecido en
' este proyecto: cada orquestador de entidad tiene su propia copia
' pequeña, en vez de forzar una abstracción compartida entre 3 clases
' que no se pidió).
'
' Único punto del proyecto donde se cruzan el vocabulario del Portal
' (FacturaPreliminarDTO/FacturaLineaPreliminarDTO, daemon_entity) y el
' de SAP (FacturaPreliminarCabecera/FacturaPreliminarLinea, daemon_data)
' — el mapeo es solo asignación de campos, sin lógica de payload SAP.
Imports System.Configuration
Imports System.Net.Http
Imports System.Net.Http.Headers
Imports System.Text
Imports daemon_data
Imports daemon_entity
Imports Newtonsoft.Json

Public Class FacturaSyncService
    Private Shared ReadOnly client As New HttpClient() With {
        .Timeout = TimeSpan.FromSeconds(30)
    }
    Private Const MaxIntentosApi As Integer = 3
    Private Shared ReadOnly DelaysReintentoMs() As Integer = {2000, 5000}

    Private Shared ReadOnly GacToken As String = ConfigurationManager.AppSettings("DaryzaApiToken")
    Private Shared ReadOnly ApiBaseUrl As String = ConfigurationManager.AppSettings("DaryzaApiBaseUrl")
    Private Shared ReadOnly PreliminarUrl As String = ApiBaseUrl.TrimEnd("/"c) & "/api/v1/facturas-pendientes-preliminar/"
    Private Shared ReadOnly ReconciliacionUrl As String = ApiBaseUrl.TrimEnd("/"c) & "/api/v1/facturas-preliminares/"
    Private Shared ReadOnly CancelacionUrl As String = ApiBaseUrl.TrimEnd("/"c) & "/api/v1/facturas-pendientes-cancelacion/"

    ' Sub-etapa 5.3, punto 7 del pedido: la reconciliación debe correr
    ' con una frecuencia MENOR que el ciclo de GRPO/Preliminar (ya
    ' documentado así en el propio contrato del Portal). Gate en
    ' memoria — _ultimaReconciliacion es Nothing al arrancar cualquier
    ' proceso nuevo, así que una invocación manual (--sync-factura-
    ' reconciliar, el único camino que existe hoy — ver Service1.
    ' Designer.vb) SIEMPRE corre de inmediato la primera vez; el gate
    ' solo se vuelve relevante el día que este método se llame
    ' repetidamente desde un proceso de larga vida (WorkerThread, si se
    ' wirea en el futuro) — ahí sí evita golpear SAP/el Portal más
    ' seguido que IntervaloReconciliacionMinutos.
    Private Shared _ultimaReconciliacion As Date? = Nothing

    Private Shared ReadOnly IntervaloReconciliacionMinutos As Integer = LeerIntervaloReconciliacionMinutos()

    Private Shared Function LeerIntervaloReconciliacionMinutos() As Integer
        Dim crudo = ConfigurationManager.AppSettings("FacturaReconciliacionIntervaloMinutos")
        Dim valor As Integer
        If Integer.TryParse(crudo, valor) AndAlso valor > 0 Then
            Return valor
        End If
        ' Default conservador si la clave falta o es inválida en
        ' App.config — 30 minutos, claramente mayor que el ciclo de
        ' 17-90s de OC/GRPO, sin depender de que el archivo la tenga
        ' bien puesta para tener un comportamiento razonable.
        Return 30
    End Function

    ' ==================================================================
    ' Sub-etapa 5.1/5.2 — creación del Preliminar
    ' ==================================================================

    ''' <summary>
    ''' Ciclo completo: trae las Facturas pendientes de Preliminar del
    ''' Portal (APROBADA_COMPRAS + estado_sap='L') y por cada una
    ''' intenta crear el documento real en SAP + confirmarlo de vuelta.
    ''' Nunca lanza — cualquier error general queda registrado y el
    ''' proceso sigue vivo.
    ''' </summary>
    Public Async Function ExecutePreliminarSync() As Task
        Try
            Dim facturas = Await ObtenerListaAsync(Of FacturaPreliminarDTO)(PreliminarUrl, "GET facturas-pendientes-preliminar")
            If facturas Is Nothing Then Return

            If facturas.Count > 0 Then
                Logger.Escribir($"[FACTURA-PRELIMINAR-INICIO] Se encontraron {facturas.Count} Factura(s) pendientes de Preliminar.")
            End If

            Dim cliente As New SapServiceLayerClient()
            Try
                Dim facturaService As New FacturaService(cliente)
                For Each factura In facturas
                    Await ProcesarPreliminarAsync(factura, facturaService)
                Next
            Finally
                cliente.Dispose()
            End Try
        Catch ex As Exception
            Logger.Escribir($"[FACTURA-PRELIMINAR-CRÍTICO] Error general en ExecutePreliminarSync: {ex.Message}")
        End Try
    End Function

    Private Async Function ProcesarPreliminarAsync(factura As FacturaPreliminarDTO, facturaService As FacturaService) As Task
        Logger.Escribir($"[FACTURA-PRELIMINAR-PROCESANDO] Factura #{factura.id} ({factura.card_code}) — {factura.lineas.Count} línea(s).")

        ' Precondición de negocio (decisión de la capa de orquestación,
        ' no de FacturaService): cada línea necesita el GRPO
        ' correspondiente YA confirmado como documento definitivo en
        ' SAP (grpo_doc_entry no nulo — el Portal expone el dato tal
        ' cual, sin filtrar, ver docstring de FacturaLineaPreliminarDTO)
        ' antes de poder hacer "Copy From" sobre él. Si falta alguna,
        ' esto NO es un rechazo de SAP — es "todavía no está listo", se
        ' salta sin llamar a SAP ni a reportar-error, y se reintentará
        ' solo en el próximo ciclo (estado_sap sigue en 'L').
        Dim lineasSinGrpo = factura.lineas.Where(Function(l) Not l.grpo_doc_entry.HasValue).ToList()
        If lineasSinGrpo.Count > 0 Then
            Dim items = String.Join(", ", lineasSinGrpo.Select(Function(l) l.item_code))
            Logger.Escribir($"[FACTURA-PRELIMINAR-ESPERA] Factura #{factura.id}: {lineasSinGrpo.Count} línea(s) sin GRPO confirmado todavía ({items}) — se reintentará en un próximo ciclo, sin llamar a SAP.")
            Return
        End If

        Dim cabecera As New FacturaPreliminarCabecera With {
            .CardCode = factura.card_code,
            .NumAtCard = factura.num_at_card,
            .DocCurrency = factura.doc_cur,
            .TaxDate = factura.tax_date,
            .DocDueDate = factura.doc_due_date,
            .UdfFPC = factura.U_MSSL_FPC,
            .UdfFNC = factura.U_MSSL_FNC,
            .UdfTOP = factura.U_MSSL_TOP,
            .UdfCBS = factura.U_MSSL_CBS,
            .PortalId = factura.id
        }
        Dim lineasSap = factura.lineas.Select(
            Function(l) New FacturaPreliminarLinea With {
                .BaseEntry = l.grpo_doc_entry.Value,
                .BaseLine = l.base_line,
                .Cantidad = l.cantidad,
                .Precio = l.precio
            }
        ).ToList()

        Dim resultado = Await facturaService.CrearPreliminarAsync(cabecera, lineasSap)

        If Not resultado.Exitoso Then
            Logger.Escribir($"[FACTURA-PRELIMINAR-FAIL] Factura #{factura.id}: SAP rechazó la creación del Preliminar — {resultado.MensajeError}")
            Await ReportarErrorPreliminarAsync(factura.id, resultado.MensajeError)
            Return
        End If

        Logger.Escribir($"[FACTURA-PRELIMINAR-ÉXITO] Factura #{factura.id}: Preliminar creado en SAP — DocEntry={resultado.DocEntry}.")

        Dim body = JsonConvert.SerializeObject(New Dictionary(Of String, Integer) From {{"doc_entry_preliminar", resultado.DocEntry.Value}})
        Dim confirmado = Await EnviarAlPortalConReintentoAsync(
            Function()
                Dim req = New HttpRequestMessage(HttpMethod.Post, $"{PreliminarUrl}{factura.id}/confirmar-preliminar/")
                req.Content = New StringContent(body, Encoding.UTF8, "application/json")
                Return req
            End Function,
            $"POST confirmar-preliminar (Factura #{factura.id})"
        )
        If confirmado.Exitoso Then
            Logger.Escribir($"[FACTURA-PRELIMINAR-CONFIRMADO] Factura #{factura.id}: Preliminar confirmado en el Portal (DocEntry={resultado.DocEntry}).")
        End If
        ' Si confirmado falla: el Preliminar SÍ existe en SAP, pero el
        ' Portal no pudo confirmarlo — mismo caso de borde ya documentado
        ' en GrpoSyncService (problema de comunicación, no de SAP; la
        ' Factura queda en 'L' y se reintentará, lo que en el próximo
        ' ciclo volvería a intentar crear OTRO Preliminar en SAP para la
        ' misma Factura — caso de borde conocido, no resuelto en esta
        ' sub-etapa, mismo criterio que el equivalente de GRPO).
    End Function

    Private Async Function ReportarErrorPreliminarAsync(facturaId As Integer, mensaje As String) As Task
        Dim body = JsonConvert.SerializeObject(New With {.error_mensaje = mensaje})
        Await EnviarAlPortalConReintentoAsync(
            Function()
                Dim req = New HttpRequestMessage(HttpMethod.Post, $"{PreliminarUrl}{facturaId}/reportar-error/")
                req.Content = New StringContent(body, Encoding.UTF8, "application/json")
                Return req
            End Function,
            $"POST reportar-error (Factura #{facturaId})"
        )
    End Function

    ' ==================================================================
    ' Sub-etapa 5.3 — reconciliación (ciclo separado, menos frecuente)
    ' ==================================================================

    ''' <summary>
    ''' Ciclo de reconciliación: para cada Factura con Preliminar ya
    ''' confirmado en SAP (estado_sap='B'), busca en SAP si Contabilidad
    ''' ya creó el documento definitivo (por U_MSSL_PORTAL_ID) y, de
    ''' encontrarlo, lo confirma de vuelta al Portal. Gateado por
    ''' IntervaloReconciliacionMinutos (ver docstring del campo arriba)
    ''' — un ciclo llamado antes de tiempo se salta por completo, sin
    ''' tocar SAP ni el Portal.
    ''' </summary>
    Public Async Function ExecuteReconciliacionSync() As Task
        Try
            If _ultimaReconciliacion.HasValue Then
                Dim transcurrido = Date.Now - _ultimaReconciliacion.Value
                If transcurrido.TotalMinutes < IntervaloReconciliacionMinutos Then
                    Logger.Escribir($"[FACTURA-RECONCILIACIÓN-SALTADA] Última corrida hace {transcurrido.TotalMinutes:F1} min, intervalo mínimo {IntervaloReconciliacionMinutos} min — se salta este ciclo.")
                    Return
                End If
            End If
            _ultimaReconciliacion = Date.Now

            Dim facturas = Await ObtenerListaAsync(Of FacturaReconciliacionDTO)(ReconciliacionUrl, "GET facturas-preliminares")
            If facturas Is Nothing Then Return

            If facturas.Count > 0 Then
                Logger.Escribir($"[FACTURA-RECONCILIACIÓN-INICIO] Se encontraron {facturas.Count} Factura(s) con Preliminar pendientes de reconciliar.")
            End If

            Dim cliente As New SapServiceLayerClient()
            Try
                Dim facturaService As New FacturaService(cliente)
                For Each factura In facturas
                    Await ProcesarReconciliacionAsync(factura, facturaService)
                Next
            Finally
                cliente.Dispose()
            End Try
        Catch ex As Exception
            Logger.Escribir($"[FACTURA-RECONCILIACIÓN-CRÍTICO] Error general en ExecuteReconciliacionSync: {ex.Message}")
        End Try
    End Function

    Private Async Function ProcesarReconciliacionAsync(factura As FacturaReconciliacionDTO, facturaService As FacturaService) As Task
        Dim resultado = Await facturaService.BuscarDefinitivoAsync(factura.id)

        If Not resultado.Encontrado Then
            If String.IsNullOrEmpty(resultado.MensajeError) Then
                ' Caso normal — Contabilidad todavía no creó el
                ' documento definitivo en SAP. No es un error, no hay
                ' ningún endpoint reportar-error para esta etapa (el
                ' Portal no expone uno — confirmado en api/factura_api.py
                ' — el silencio es la respuesta correcta acá).
                Logger.Escribir($"[FACTURA-RECONCILIACIÓN-ESPERA] Factura #{factura.id}: documento definitivo todavía no existe en SAP.")
            Else
                Logger.Escribir($"[FACTURA-RECONCILIACIÓN-FAIL] Factura #{factura.id}: {resultado.MensajeError}")
            End If
            Return
        End If

        Dim body = JsonConvert.SerializeObject(New Dictionary(Of String, Integer) From {{"doc_entry_definitivo", resultado.DocEntry.Value}})
        Dim confirmado = Await EnviarAlPortalConReintentoAsync(
            Function()
                Dim req = New HttpRequestMessage(HttpMethod.Post, $"{ReconciliacionUrl}{factura.id}/confirmar-definitivo/")
                req.Content = New StringContent(body, Encoding.UTF8, "application/json")
                Return req
            End Function,
            $"POST confirmar-definitivo (Factura #{factura.id})"
        )
        If confirmado.Exitoso Then
            Logger.Escribir($"[FACTURA-RECONCILIACIÓN-CONFIRMADO] Factura #{factura.id}: documento definitivo confirmado en el Portal (DocEntry={resultado.DocEntry}).")
        End If
    End Function

    ' ==================================================================
    ' Sub-etapa 5.4 — cancelación
    ' ==================================================================

    ''' <summary>
    ''' Ciclo de cancelación: para cada Factura cancelada en el Portal
    ''' que ya tenía un Preliminar creado en SAP (estado=CANCELADO,
    ''' estado_sap='B'), anula ese Preliminar en SAP y lo confirma de
    ''' vuelta.
    ''' </summary>
    Public Async Function ExecuteCancelacionSync() As Task
        Try
            Dim facturas = Await ObtenerListaAsync(Of FacturaCancelacionDTO)(CancelacionUrl, "GET facturas-pendientes-cancelacion")
            If facturas Is Nothing Then Return

            If facturas.Count > 0 Then
                Logger.Escribir($"[FACTURA-CANCELACIÓN-INICIO] Se encontraron {facturas.Count} Factura(s) pendientes de cancelar en SAP.")
            End If

            Dim cliente As New SapServiceLayerClient()
            Try
                Dim facturaService As New FacturaService(cliente)
                For Each factura In facturas
                    Await ProcesarCancelacionAsync(factura, facturaService)
                Next
            Finally
                cliente.Dispose()
            End Try
        Catch ex As Exception
            Logger.Escribir($"[FACTURA-CANCELACIÓN-CRÍTICO] Error general en ExecuteCancelacionSync: {ex.Message}")
        End Try
    End Function

    Private Async Function ProcesarCancelacionAsync(factura As FacturaCancelacionDTO, facturaService As FacturaService) As Task
        Dim docEntry As Integer
        If Not Integer.TryParse(factura.doc_entry_preliminar, docEntry) Then
            Logger.Escribir($"[FACTURA-CANCELACIÓN-FAIL] Factura #{factura.id}: doc_entry_preliminar ('{factura.doc_entry_preliminar}') no es un DocEntry numérico válido — se omite.")
            Return
        End If

        Dim resultado = Await facturaService.CancelarPreliminarAsync(docEntry)
        If Not resultado.Exitoso Then
            ' Sin endpoint reportar-error para esta etapa (confirmado en
            ' api/factura_api.py) — se registra localmente y se
            ' reintenta en el próximo ciclo, estado_sap sigue en 'B'.
            Logger.Escribir($"[FACTURA-CANCELACIÓN-FAIL] Factura #{factura.id}: SAP rechazó la anulación del Preliminar (DocEntry={docEntry}) — {resultado.MensajeError}")
            Return
        End If

        Logger.Escribir($"[FACTURA-CANCELACIÓN-ÉXITO] Factura #{factura.id}: Preliminar anulado en SAP (DocEntry={docEntry}).")

        Dim confirmado = Await EnviarAlPortalConReintentoAsync(
            Function() New HttpRequestMessage(HttpMethod.Post, $"{CancelacionUrl}{factura.id}/confirmar-cancelacion/"),
            $"POST confirmar-cancelacion (Factura #{factura.id})"
        )
        If confirmado.Exitoso Then
            Logger.Escribir($"[FACTURA-CANCELACIÓN-CONFIRMADO] Factura #{factura.id}: cancelación confirmada en el Portal.")
        End If
    End Function

    ' ==================================================================
    ' Helpers compartidos por los 3 ciclos de arriba
    ' ==================================================================

    Private Async Function ObtenerListaAsync(Of T)(url As String, descripcion As String) As Task(Of List(Of T))
        Dim resultado = Await EnviarAlPortalConReintentoAsync(
            Function() New HttpRequestMessage(HttpMethod.Get, url),
            descripcion
        )
        If Not resultado.Exitoso Then Return Nothing
        Return JsonConvert.DeserializeObject(Of List(Of T))(resultado.Contenido)
    End Function

    ''' <summary>
    ''' Mismo criterio de reintento ya establecido por SyncService.vb::
    ''' AwakeApiPost (Etapa 1) y replicado en GrpoSyncService.vb (Etapa
    ''' 4) — hasta MaxIntentosApi veces con espera progresiva, SOLO ante
    ''' fallas transitorias (timeout, red, 5xx). Un 4xx es un rechazo de
    ''' negocio de Django — se retorna de inmediato sin reintentar.
    ''' </summary>
    Private Async Function EnviarAlPortalConReintentoAsync(
        construirRequest As Func(Of HttpRequestMessage),
        descripcion As String
    ) As Task(Of PortalResult)
        For intento As Integer = 1 To MaxIntentosApi
            Try
                Dim req = construirRequest()
                client.DefaultRequestHeaders.Clear()
                client.DefaultRequestHeaders.Authorization = New AuthenticationHeaderValue("Token", GacToken)
                Dim response = Await client.SendAsync(req)
                Dim contenido = Await response.Content.ReadAsStringAsync()

                If response.IsSuccessStatusCode Then
                    Return New PortalResult With {.Exitoso = True, .StatusCode = CInt(response.StatusCode), .Contenido = contenido}
                End If

                Dim codigo = CInt(response.StatusCode)
                If codigo >= 400 AndAlso codigo < 500 Then
                    Logger.Escribir($"[FACTURA-API-FAIL] {descripcion} - Status: {response.StatusCode} - Detalle: {contenido} (rechazo de negocio, sin reintento)")
                    Return New PortalResult With {.Exitoso = False, .StatusCode = codigo, .Contenido = contenido}
                End If

                Logger.Escribir($"[FACTURA-API-FAIL] {descripcion} - Status: {response.StatusCode} - Detalle: {contenido} (intento {intento}/{MaxIntentosApi})")
            Catch ex As TaskCanceledException
                Logger.Escribir($"[FACTURA-TIMEOUT] {descripcion} - Sin respuesta en {client.Timeout.TotalSeconds}s (intento {intento}/{MaxIntentosApi})")
            Catch ex As HttpRequestException
                Logger.Escribir($"[FACTURA-NETWORK-FAIL] {descripcion} - Error de conexión: {ex.Message} (intento {intento}/{MaxIntentosApi})")
            Catch ex As Exception
                Logger.Escribir($"[FACTURA-NETWORK-FAIL] {descripcion} - Error inesperado: {ex.Message} (intento {intento}/{MaxIntentosApi})")
            End Try

            If intento < MaxIntentosApi Then
                Await Task.Delay(DelaysReintentoMs(intento - 1))
            End If
        Next

        Logger.Escribir($"[FACTURA-API-FAIL-DEFINITIVO] {descripcion} - Se agotaron los {MaxIntentosApi} intentos.")
        Return New PortalResult With {.Exitoso = False, .StatusCode = 0, .Contenido = ""}
    End Function
End Class
