' Proyecto: daemon_logic
Imports System.Configuration
Imports System.Net.Http
Imports System.Net.Http.Headers ' Requisito para el Token
Imports System.Text
Imports System.Threading.Tasks
Imports daemon_data
Imports daemon_entity
Imports Newtonsoft.Json

Public Class SyncService
    Private ReadOnly _data As New SyncSAPB1()

    ' Sesión 85 (Etapa 1): Timeout explícito — antes HttpClient usaba su
    ' default (100s), que dejaba el WorkerThread bloqueado hasta 100s ante
    ' un problema de red antes de poder reintentar o pasar al siguiente ciclo.
    Private Shared ReadOnly client As New HttpClient() With {
        .Timeout = TimeSpan.FromSeconds(30)
    }

    ' Sesión 85 (Etapa 1): reintentos con backoff para AwakeApiPost — ver
    ' el método más abajo. 3 intentos en total, con espera entre el 1º/2º
    ' y el 2º/3º (sin espera tras el último, ya no tiene sentido).
    Private Const MaxIntentosApi As Integer = 3
    Private Shared ReadOnly DelaysReintentoMs() As Integer = {2000, 5000}

    ' Sesión 84: Token y URL de API ya NO están hardcodeados — se leen
    ' de App.config (<appSettings>), que NO se versiona (ver
    ' daemon-sap/.gitignore). Token rotado el 2026-08-27 (el valor
    ' anterior, expuesto en este mismo archivo, ya fue invalidado del
    ' lado del Portal — ver CLAUDE.md sesión 84).
    Private Shared ReadOnly GacToken As String = ConfigurationManager.AppSettings("DaryzaApiToken")
    Private Shared ReadOnly ApiBaseUrl As String = ConfigurationManager.AppSettings("DaryzaApiBaseUrl")

    Public Async Function ExecuteSync() As Task
        Try


            Dim headers As DataTable = _data.GetPendingHeaders()

            ' Log: Inicio de ciclo
            If headers.Rows.Count > 0 Then
                Logger.Escribir($"[INICIO] Se encontraron {headers.Rows.Count} OCs pendientes en SAP.")
            End If

            For Each row As DataRow In headers.Rows
                Dim docEntry As Integer = Convert.ToInt32(row("DocEntry"))
                Dim docNum As String = row("DocNum").ToString()

                Logger.Escribir($"[PROCESANDO] Iniciando mapeo de OC #{docNum} (DocEntry: {docEntry}).")

                ' 1. Armar el objeto Entity con Cabecera
                Dim requestObj As New PurchaseOrderDTO With {
                    .doc_entry = docEntry,
                    .doc_num = row("DocNum").ToString(),
                    .card_code = row("CardCode").ToString(),
                    .card_name = row("CardName").ToString(),
                    .e_mail = row("E_Mail").ToString(),
                    .status = "PENDIENTE",
                    .u_mss_tdb = row("U_MSS_TDB").ToString(),
                    .doc_cur = row("DocCur").ToString()
                }

                ' 2. Cargar las líneas desde PQT1 (Detalle)
                Dim linesTable As DataTable = _data.GetLinesByDocEntry(docEntry)
                Logger.Escribir($"[DETALLE] OC #{docNum}: Cargando {linesTable.Rows.Count} líneas.")

                For Each lineRow As DataRow In linesTable.Rows
                    requestObj.lines.Add(New PurchaseOrderLineDTO With {
                        .line_num = Convert.ToInt32(lineRow("LineNum")),
                        .item_code = lineRow("ItemCode").ToString(),
                        .description = lineRow("Dscription").ToString(),
                        .quantity_sap = Convert.ToDecimal(lineRow("Quantity")),
                        .und_medida = lineRow("UM").ToString(),
                        .requiere_coa = Convert.ToBoolean(lineRow("requiere_coa")),
                        .precio_unitario = Convert.ToDecimal(lineRow("Unit Price")),
                        .precio_total_linea = Convert.ToDecimal(lineRow("Line Total")),
                        .tax_code = lineRow("Tax Code").ToString()
                    })
                Next

                ' 3. Enviar a la API de Django (Uso de Await en lugar de .Result)
                Dim success As Boolean = Await AwakeApiPost(requestObj)

                ' 4. Solo si la API responde 201 Created, marcar en SAP
                If success Then
                    Logger.Escribir($"[ÉXITO] OC #{docNum} sincronizada y marcada en SAP.")
                    _data.MarkAsSynced(docEntry)
                Else
                    Logger.Escribir($"[ERROR] OC #{docNum} falló en la API. Se mantendrá pendiente.")
                End If
            Next
        Catch ex As Exception
            Logger.Escribir($"[CRÍTICO] Error general en ExecuteSync: {ex.Message}")
        End Try
    End Function

    ' Sesión 84: URL configurable (App.config, clave DaryzaApiBaseUrl)
    ' en vez de hardcodeada — antes fija a "URL de desarrollo local".
    Private Shared ReadOnly SyncOcUrl As String = ApiBaseUrl.TrimEnd("/"c) & "/api/v1/sync-oc/"

    ''' <summary>
    ''' Sesión 85 (Etapa 1): reintenta hasta MaxIntentosApi veces, con espera
    ''' progresiva entre intentos (DelaysReintentoMs), SOLO ante fallas
    ''' transitorias (timeout, error de red, 5xx). Un 4xx (400/404/etc.) es
    ''' un rechazo de negocio de Django (dato inválido, OC ya sincronizada
    ''' con otro estado, etc.) — reintentarlo no cambia el resultado, así
    ''' que se retorna False de inmediato sin gastar los reintentos.
    ''' sync-oc ya es idempotente del lado de Django (update_or_create por
    ''' doc_entry, sesión 49) — reintentar un envío que sí llegó pero cuya
    ''' respuesta se perdió en la red no duplica nada.
    ''' </summary>
    Private Async Function AwakeApiPost(data As PurchaseOrderDTO) As Task(Of Boolean)
        Dim json = JsonConvert.SerializeObject(data)

        For intento As Integer = 1 To MaxIntentosApi
            Try
                Dim content = New StringContent(json, Encoding.UTF8, "application/json")

                ' Configurar el Token de Seguridad (Authorization: Token <key>)
                client.DefaultRequestHeaders.Clear() ' Limpia headers previos
                client.DefaultRequestHeaders.Authorization = New AuthenticationHeaderValue("Token", GacToken)

                ' Realizar el envío de forma asíncrona
                Dim response = Await client.PostAsync(SyncOcUrl, content)

                If response.StatusCode = System.Net.HttpStatusCode.Created Then
                    Return True
                End If

                Dim errorContent = Await response.Content.ReadAsStringAsync()
                Dim codigo As Integer = CInt(response.StatusCode)

                If codigo >= 400 AndAlso codigo < 500 Then
                    Logger.Escribir($"[API-FAIL] OC #{data.doc_num} - Status: {response.StatusCode} - Detalle: {errorContent} (rechazo de negocio, sin reintento)")
                    Return False
                End If

                Logger.Escribir($"[API-FAIL] OC #{data.doc_num} - Status: {response.StatusCode} - Detalle: {errorContent} (intento {intento}/{MaxIntentosApi})")

            Catch ex As TaskCanceledException
                ' HttpClient.Timeout expirado (30s) — se trata como falla
                ' transitoria, igual que un error de red.
                Logger.Escribir($"[TIMEOUT] OC #{data.doc_num} - Sin respuesta en {client.Timeout.TotalSeconds}s (intento {intento}/{MaxIntentosApi})")
            Catch ex As HttpRequestException
                Logger.Escribir($"[NETWORK-FAIL] OC #{data.doc_num} - Error de conexión: {ex.Message} (intento {intento}/{MaxIntentosApi})")
            Catch ex As Exception
                Logger.Escribir($"[NETWORK-FAIL] OC #{data.doc_num} - Error inesperado: {ex.Message} (intento {intento}/{MaxIntentosApi})")
            End Try

            If intento < MaxIntentosApi Then
                Await Task.Delay(DelaysReintentoMs(intento - 1))
            End If
        Next

        Logger.Escribir($"[API-FAIL-DEFINITIVO] OC #{data.doc_num} - Se agotaron los {MaxIntentosApi} intentos.")
        Return False
    End Function
End Class
