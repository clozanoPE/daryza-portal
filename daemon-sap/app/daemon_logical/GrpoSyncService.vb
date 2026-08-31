' Proyecto: daemon_logical / GrpoSyncService.vb
'
' Sesión 97, Etapa 4: orquesta el ciclo de creación de GRPO (Entrada
' de Mercancía) — solo decide CUÁNDO y CON QUÉ DATOS llamar a cada
' capa de abajo, nunca arma el payload de SAP ni parsea la respuesta
' HTTP de Service Layer directamente (eso vive en GrpoService.vb,
' daemon_data — ver su docstring). El consumo del Portal (GET
' entradas-pendientes/POST confirmar-*/reportar-error) sigue el MISMO
' patrón HttpClient+Token ya usado por SyncService.vb para sync-oc —
' mismo campo Shared ReadOnly client, mismas claves de App.config
' (DaryzaApiToken/DaryzaApiBaseUrl, sin ninguna clave nueva), mismo
' criterio de reintento con backoff SOLO ante fallas transitorias
' (timeout/red/5xx) — un 4xx es un rechazo de negocio de Django, no se
' reintenta.
'
' Flujo de un solo paso (confirmado, sesión 86): el mismo DocEntry que
' SAP asigna al crear el GRPO se usa en confirmar-borrador Y
' confirmar-definitivo, en la misma corrida.
Imports System.Configuration
Imports System.Net.Http
Imports System.Net.Http.Headers
Imports System.Text
Imports daemon_data
Imports daemon_entity
Imports Newtonsoft.Json

''' <summary>
''' Resultado de una llamada al Portal (no a Service Layer — ver
''' ServiceLayerResult en daemon_data para ese caso distinto; se
''' declara un tipo aparte a propósito, para no mezclar vocabulario de
''' 2 capas distintas aunque la forma sea idéntica).
''' </summary>
Public Class PortalResult
    Public Property Exitoso As Boolean
    Public Property StatusCode As Integer
    Public Property Contenido As String
End Class

Public Class GrpoSyncService
    Private Shared ReadOnly client As New HttpClient() With {
        .Timeout = TimeSpan.FromSeconds(30)
    }
    Private Const MaxIntentosApi As Integer = 3
    Private Shared ReadOnly DelaysReintentoMs() As Integer = {2000, 5000}

    Private Shared ReadOnly GacToken As String = ConfigurationManager.AppSettings("DaryzaApiToken")
    Private Shared ReadOnly ApiBaseUrl As String = ConfigurationManager.AppSettings("DaryzaApiBaseUrl")
    Private Shared ReadOnly EntradasPendientesUrl As String = ApiBaseUrl.TrimEnd("/"c) & "/api/v1/entradas-pendientes/"

    ''' <summary>
    ''' Ciclo completo: trae las Entradas de Mercancía pendientes de
    ''' GRPO del Portal, y por cada una intenta crear el documento real
    ''' en SAP + confirmarlo de vuelta. Nunca lanza — cualquier error
    ''' general queda registrado y el proceso sigue vivo.
    ''' </summary>
    Public Async Function ExecuteGrpoSync() As Task
        Try
            Dim entradas = Await ObtenerEntradasPendientesAsync()
            If entradas Is Nothing Then
                Return ' el fallo ya quedó registrado adentro
            End If

            If entradas.Count > 0 Then
                Logger.Escribir($"[GRPO-INICIO] Se encontraron {entradas.Count} Entrada(s) de Mercancía pendientes de GRPO.")
            End If

            ' Una sola sesión de Service Layer para TODO el ciclo (ver
            ' ARQUITECTURA_SAP.md/sesión 85) — se crea acá, se reutiliza
            ' para cada entrada, se libera al final del ciclo completo.
            Dim cliente As New SapServiceLayerClient()
            Try
                Dim grpoService As New GrpoService(cliente)
                For Each entrada In entradas
                    Await ProcesarEntradaAsync(entrada, grpoService)
                Next
            Finally
                cliente.Dispose()
            End Try
        Catch ex As Exception
            Logger.Escribir($"[GRPO-CRÍTICO] Error general en ExecuteGrpoSync: {ex.Message}")
        End Try
    End Function

    Private Async Function ProcesarEntradaAsync(entrada As EntradaMercaderiaDTO, grpoService As GrpoService) As Task
        Logger.Escribir($"[GRPO-PROCESANDO] Entrada #{entrada.id} (Ticket #{entrada.ticket_id}, {entrada.card_code}) — {entrada.lineas.Count} línea(s).")

        ' Mapeo DTO del Portal (daemon_entity) -> tipo que SAP necesita
        ' (daemon_data.GrpoLineaEntrada) — el ÚNICO lugar del proyecto
        ' donde se cruzan ambos mundos, y es solo una asignación de
        ' campos, sin lógica de negocio ni de HTTP.
        Dim lineasSap = entrada.lineas.Select(
            Function(l) New GrpoLineaEntrada With {
                .BaseEntry = l.doc_entry_oc,
                .BaseLine = l.base_line,
                .Cantidad = l.cantidad,
                .ItemCode = l.item_code,
                .NumeroLote = l.numero_lote,
                .FechaVencimientoLote = l.fecha_vencimiento_lote,
                .FechaFabricacionLote = l.fecha_fabricacion_lote
            }
        ).ToList()

        Dim resultado = Await grpoService.CrearGrpoAsync(entrada.card_code, lineasSap)

        If Not resultado.Exitoso Then
            Logger.Escribir($"[GRPO-FAIL] Entrada #{entrada.id}: SAP rechazó la creación del GRPO — {resultado.MensajeError}")
            Await ReportarErrorAsync(entrada.id, resultado.MensajeError)
            Return
        End If

        Logger.Escribir($"[GRPO-ÉXITO] Entrada #{entrada.id}: GRPO creado en SAP — DocEntry={resultado.DocEntry}, DocNum={resultado.DocNum}.")

        Dim borradorOk = Await ConfirmarAsync(entrada.id, "confirmar-borrador", "doc_entry_borrador", resultado.DocEntry.Value, resultado.DocNum)
        If Not borradorOk Then
            ' El GRPO SÍ se creó en SAP, pero el Portal no pudo
            ' confirmarlo — no se llama a confirmar-definitivo (no
            ' tendría sentido confirmar el paso 2 si el 1 no se
            ' registró), y tampoco se llama a reportar-error (el
            ' documento real SÍ existe en SAP; esto NO es un rechazo de
            ' SAP, es un problema de comunicación con el Portal — ya
            ' quedó registrado en el log por ConfirmarAsync). La
            ' entrada queda en 'L' y se reintentará en el próximo
            ' ciclo — CrearGrpoAsync volvería a intentar un "Copy
            ' From" de la misma OC; esto es un caso de borde conocido,
            ' documentado en ARQUITECTURA_SAP.md, no resuelto en esta
            ' sub-etapa (ver "Fuera de alcance").
            Return
        End If

        Dim definitivoOk = Await ConfirmarAsync(entrada.id, "confirmar-definitivo", "doc_entry_definitivo", resultado.DocEntry.Value, resultado.DocNum)
        If definitivoOk Then
            Logger.Escribir($"[GRPO-CONFIRMADO] Entrada #{entrada.id}: borrador y definitivo confirmados en el Portal (DocEntry={resultado.DocEntry}, DocNum={resultado.DocNum}).")
        End If
    End Function

    Private Async Function ObtenerEntradasPendientesAsync() As Task(Of List(Of EntradaMercaderiaDTO))
        Dim resultado = Await EnviarAlPortalConReintentoAsync(
            Function() New HttpRequestMessage(HttpMethod.Get, EntradasPendientesUrl),
            "GET entradas-pendientes"
        )
        If Not resultado.Exitoso Then Return Nothing
        Return JsonConvert.DeserializeObject(Of List(Of EntradaMercaderiaDTO))(resultado.Contenido)
    End Function

    Private Async Function ConfirmarAsync(entradaId As Integer, accion As String, campo As String, docEntry As Integer, docNum As Integer?) As Task(Of Boolean)
        Dim url = $"{EntradasPendientesUrl}{entradaId}/{accion}/"
        Dim payload As New Dictionary(Of String, Object) From {{campo, docEntry}}
        ' Sesión 99: doc_num opcional — el Portal lo guarda en doc_num_sap
        ' (número humano del GRPO). Si SAP no lo devolvió, no se envía.
        If docNum.HasValue Then payload("doc_num") = docNum.Value
        Dim body = JsonConvert.SerializeObject(payload)
        Dim resultado = Await EnviarAlPortalConReintentoAsync(
            Function()
                Dim req = New HttpRequestMessage(HttpMethod.Post, url)
                req.Content = New StringContent(body, Encoding.UTF8, "application/json")
                Return req
            End Function,
            $"POST {accion} (entrada #{entradaId})"
        )
        Return resultado.Exitoso
    End Function

    Private Async Function ReportarErrorAsync(entradaId As Integer, mensaje As String) As Task
        Dim url = $"{EntradasPendientesUrl}{entradaId}/reportar-error/"
        Dim body = JsonConvert.SerializeObject(New With {.error_mensaje = mensaje})
        Await EnviarAlPortalConReintentoAsync(
            Function()
                Dim req = New HttpRequestMessage(HttpMethod.Post, url)
                req.Content = New StringContent(body, Encoding.UTF8, "application/json")
                Return req
            End Function,
            $"POST reportar-error (entrada #{entradaId})"
        )
    End Function

    ''' <summary>
    ''' Mismo criterio de reintento ya establecido por SyncService.vb::
    ''' AwakeApiPost (sesión 85, Etapa 1) — hasta MaxIntentosApi veces
    ''' con espera progresiva, SOLO ante fallas transitorias (timeout,
    ''' red, 5xx). Un 4xx es un rechazo de negocio de Django (dato
    ''' inválido, etc.) — se retorna de inmediato sin reintentar.
    ''' Generalizado acá a un solo helper (en vez de repetir el loop 4
    ''' veces para GET/3×POST) para no arriesgar una copia con un bug
    ''' distinto en cada sitio.
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
                    Logger.Escribir($"[GRPO-API-FAIL] {descripcion} - Status: {response.StatusCode} - Detalle: {contenido} (rechazo de negocio, sin reintento)")
                    Return New PortalResult With {.Exitoso = False, .StatusCode = codigo, .Contenido = contenido}
                End If

                Logger.Escribir($"[GRPO-API-FAIL] {descripcion} - Status: {response.StatusCode} - Detalle: {contenido} (intento {intento}/{MaxIntentosApi})")
            Catch ex As TaskCanceledException
                Logger.Escribir($"[GRPO-TIMEOUT] {descripcion} - Sin respuesta en {client.Timeout.TotalSeconds}s (intento {intento}/{MaxIntentosApi})")
            Catch ex As HttpRequestException
                Logger.Escribir($"[GRPO-NETWORK-FAIL] {descripcion} - Error de conexión: {ex.Message} (intento {intento}/{MaxIntentosApi})")
            Catch ex As Exception
                Logger.Escribir($"[GRPO-NETWORK-FAIL] {descripcion} - Error inesperado: {ex.Message} (intento {intento}/{MaxIntentosApi})")
            End Try

            If intento < MaxIntentosApi Then
                Await Task.Delay(DelaysReintentoMs(intento - 1))
            End If
        Next

        Logger.Escribir($"[GRPO-API-FAIL-DEFINITIVO] {descripcion} - Se agotaron los {MaxIntentosApi} intentos.")
        Return New PortalResult With {.Exitoso = False, .StatusCode = 0, .Contenido = ""}
    End Function
End Class
