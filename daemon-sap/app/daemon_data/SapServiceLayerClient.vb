' DAL (Data Access Layer) / SapServiceLayerClient.vb
'
' Sesión 95, Sub-etapa 3.1/3.2 del plan de integración VB.NET ↔ Portal
' (ver CLAUDE.md raíz, sección "Plan de integración VB.NET ↔ Portal"):
' cliente de sesión de SAP Service Layer — login + lectura + (desde la
' sesión 97) escritura genérica (PostAsync). Esta clase es el ÚNICO
' punto de contacto HTTP con Service Layer en todo el proyecto — sabe
' de sesión/cookies/reintentos, pero NADA de qué documento de negocio
' se está creando (eso vive en GrpoService.vb, que la CONSUME — ver su
' propio docstring). Mantener esta separación: si un mañana hace falta
' un tercer tipo de documento (Factura Preliminar, Etapa 5), agrega
' otra clase tipo GrpoService que reutilice PostAsync/GetAsync de acá,
' nunca repitas el manejo de sesión/cookies en otro lado.
'
' Decisión de diseño (confirmada en el plan, sesión 85): UNA sola
' sesión de Service Layer por ciclo del demonio — login una vez,
' reutilizar la sesión mientras siga vigente, no un login por cada
' llamada.
'
' Manejo de cookies 100% MANUAL, no vía HttpClientHandler.CookieContainer
' — hallazgo real de esta sesión, no una preferencia de estilo: el
' Set-Cookie real que devuelve Service Layer para B1SESSION viene con
' un formato que CookieContainer de .NET Framework 4.8 no logra
' parsear/almacenar (termina en ";HttpOnly;" con punto y coma colgante,
' sin atributo Path — un patrón ya documentado como problemático contra
' el parser de cookies de .NET). Confirmado empíricamente: con
' CookieContainer, tras un login 200 exitoso, el contenedor solo
' terminaba reteniendo la cookie ROUTEID (formato más convencional) y
' JAMÁS B1SESSION — cada GET posterior fallaba con 401 "Invalid
' session or session already timeout", pese a que el login sí había
' sido válido. Se optó por extraer ambas cookies (B1SESSION y ROUTEID)
' directo de los headers Set-Cookie crudos de la respuesta de /Login, y
' reenviarlas a mano en el header Cookie de cada request — sin
' depender del parser de .NET para nada de esto.
'
' Esta clase NUNCA escribe al log del demonio directamente (Logger
' vive en daemon_logical, que a su vez referencia a daemon_data — una
' referencia en sentido inverso crearía una dependencia circular entre
' proyectos, que MSBuild rechaza). En su lugar, cada método devuelve un
' resultado explícito (ServiceLayerResult) con toda la información
' necesaria para que el llamador (daemon_logical.ServiceLayerSmokeTest,
' en esta sub-etapa) decida qué loggear — mismo principio de
' separación de capas ya usado en el resto del proyecto (SyncSAPB1
' tampoco loggea, solo devuelve datos; SyncService es quien loggea).
' Ningún método público de esta clase deja escapar una excepción sin
' capturar — todo error (credenciales inválidas, servidor caído,
' timeout) se traduce a un ServiceLayerResult con Exitoso=False, nunca
' un crash del proceso.
Imports System.Configuration
Imports System.Net
Imports System.Net.Http
Imports System.Text
Imports Newtonsoft.Json

Public Class ServiceLayerResult
    Public Property Exitoso As Boolean
    Public Property StatusCode As Integer
    Public Property Contenido As String
    Public Property Mensaje As String
End Class

Public Class SapServiceLayerClient
    Implements IDisposable

    Private Shared ReadOnly BaseUrl As String = ConfigurationManager.AppSettings("ServiceLayerBaseUrl")
    Private Shared ReadOnly CompanyDB As String = ConfigurationManager.AppSettings("ServiceLayerCompanyDB")
    Private Shared ReadOnly UserName As String = ConfigurationManager.AppSettings("ServiceLayerUser")
    Private Shared ReadOnly Password As String = ConfigurationManager.AppSettings("ServiceLayerPassword")

    Private ReadOnly _handler As HttpClientHandler
    Private ReadOnly _client As HttpClient

    ' Cookies de sesión, extraídas a mano de los Set-Cookie crudos de
    ' /Login (ver docstring del módulo) — B1SESSION es la que realmente
    ' autentica cada request; ROUTEID es la afinidad de nodo del
    ' balanceador (solo presente en instalaciones balanceadas). Ambas
    ' se reenvían juntas, a mano, en el header Cookie de cada request.
    Private _b1Session As String
    Private _routeIdCookie As String

    Public ReadOnly Property SessionId As String
    Public ReadOnly Property RouteId As String
    Public ReadOnly Property SesionActiva As Boolean

    Public Sub New()
        ' TLS 1.2 explícito — necesario en algunos servidores Windows
        ' internos más viejos donde el default de .NET Framework 4.8
        ' no siempre negocia la versión correcta contra IIS/Service
        ' Layer sin este empujón.
        ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12

        ' Hallazgo real, confirmado empíricamente (curl con el mismo
        ' body/headers funciona; agregándole a mano el header
        ' "Expect: 100-continue" reproduce EXACTO el mismo 500 con
        ' Content-Length: 0 que daba .NET): HttpClient en .NET
        ' Framework 4.8 envía "Expect: 100-continue" por default en
        ' cualquier POST con body — el balanceador Apache/mod_proxy_
        ' balancer que reparte Service Layer entre sus varios nodos
        ' (ROUTEID=.node1..4, confirmado real) no maneja ese handshake
        ' y devuelve 500 vacío en TODOS los nodos, sin importar cuál
        ' atienda la request. Desactivarlo es la única forma de que el
        ' login funcione contra esta instalación.
        ServicePointManager.Expect100Continue = False

        _handler = New HttpClientHandler() With {
            .UseCookies = False
        }
        ' Service Layer on-prem casi siempre corre con certificado
        ' autofirmado (instalación interna, sin CA pública) — Service
        ' Layer confirmado activo contra una IP interna (192.168.10.30,
        ' sesión 85), no un dominio público con certificado real. Sin
        ' este bypass, CUALQUIER llamada fallaría con un error de SSL
        ' antes de siquiera llegar al login. Aceptable acá porque el
        ' demonio y el servidor SAP están en la misma red interna de
        ' Daryza, nunca expuesto a Internet — mismo criterio implícito
        ' que ya aplica la conexión HANA de este mismo proyecto (sin
        ' validación de certificado tampoco).
        _handler.ServerCertificateCustomValidationCallback = Function(msg, cert, chain, errors) True

        _client = New HttpClient(_handler) With {
            .Timeout = TimeSpan.FromSeconds(30)
        }
        _client.BaseAddress = New Uri(BaseUrl.TrimEnd("/"c) & "/")
    End Sub

    ''' <summary>
    ''' Extrae "Nombre=Valor" (solo eso, descartando atributos como
    ''' HttpOnly/Path/Expires) del primer segmento de un Set-Cookie
    ''' crudo — ej. "B1SESSION=abc-123;HttpOnly;" -> "B1SESSION=abc-123".
    ''' </summary>
    Private Shared Function ExtraerNombreValor(setCookieCrudo As String) As String
        Dim primerSegmento = setCookieCrudo.Split(";"c)(0).Trim()
        Return primerSegmento
    End Function

    ''' <summary>
    ''' POST /Login — inicia una sesión nueva contra CompanyDB con las
    ''' credenciales de App.config. Nunca lanza: cualquier fallo
    ''' (credenciales inválidas, servidor caído, timeout, SSL) se
    ''' traduce a Exitoso=False con Mensaje legible.
    ''' </summary>
    Public Async Function LoginAsync() As Task(Of ServiceLayerResult)
        Try
            Dim body = JsonConvert.SerializeObject(New With {
                .CompanyDB = CompanyDB,
                .UserName = UserName,
                .Password = Password
            })
            Dim content = New StringContent(body, Encoding.UTF8, "application/json")
            Dim response = Await _client.PostAsync("Login", content)
            Dim contenido = Await response.Content.ReadAsStringAsync()

            If response.IsSuccessStatusCode Then
                Try
                    Dim json = JsonConvert.DeserializeObject(Of Dictionary(Of String, Object))(contenido)
                    If json.ContainsKey("SessionId") Then
                        _SessionId = json("SessionId").ToString()
                    End If
                Catch
                    ' El login igual fue 200 — si el body no se pudo parsear
                    ' (formato inesperado), no es motivo para tratarlo como
                    ' fallo; SessionId queda sin poblar, solo para diagnóstico.
                End Try

                ' Extracción manual de las cookies reales — ver docstring
                ' del módulo sobre por qué no se usa CookieContainer.
                _b1Session = Nothing
                _routeIdCookie = Nothing
                _RouteId = Nothing
                Dim setCookies As IEnumerable(Of String) = Nothing
                If response.Headers.TryGetValues("Set-Cookie", setCookies) Then
                    For Each crudo In setCookies
                        If crudo.StartsWith("B1SESSION=", StringComparison.OrdinalIgnoreCase) Then
                            _b1Session = ExtraerNombreValor(crudo)
                        ElseIf crudo.StartsWith("ROUTEID=", StringComparison.OrdinalIgnoreCase) Then
                            _routeIdCookie = ExtraerNombreValor(crudo)
                            _RouteId = _routeIdCookie.Split("="c)(1)
                        End If
                    Next
                End If

                If String.IsNullOrEmpty(_b1Session) Then
                    ' Login 200 pero sin cookie B1SESSION reconocible —
                    ' no debería pasar nunca en la práctica, pero sin
                    ' ella ninguna llamada posterior podría autenticar.
                    ' Se trata como fallo explícito en vez de dejar
                    ' _SesionActiva=True con una sesión que en realidad
                    ' no sirve para nada.
                    _SesionActiva = False
                    Return New ServiceLayerResult With {
                        .Exitoso = False, .StatusCode = CInt(response.StatusCode),
                        .Contenido = contenido,
                        .Mensaje = "Login devolvió 200 pero sin cookie B1SESSION reconocible en la respuesta."
                    }
                End If

                _SesionActiva = True
                Return New ServiceLayerResult With {
                    .Exitoso = True, .StatusCode = CInt(response.StatusCode),
                    .Contenido = contenido, .Mensaje = "Login exitoso."
                }
            Else
                _SesionActiva = False
                Return New ServiceLayerResult With {
                    .Exitoso = False, .StatusCode = CInt(response.StatusCode),
                    .Contenido = contenido,
                    .Mensaje = $"Login rechazado por Service Layer: {response.StatusCode} - {contenido}"
                }
            End If
        Catch ex As Exception
            _SesionActiva = False
            Return New ServiceLayerResult With {
                .Exitoso = False, .StatusCode = 0, .Contenido = "",
                .Mensaje = $"Error de conexión al intentar loguear en Service Layer: {ex.Message}"
            }
        End Try
    End Function

    ''' <summary>
    ''' Arma el header Cookie combinado (B1SESSION + ROUTEID si existe)
    ''' para adjuntar a mano en cada request — ver docstring del módulo.
    ''' </summary>
    Private Function ArmarHeaderCookie() As String
        If String.IsNullOrEmpty(_routeIdCookie) Then Return _b1Session
        Return $"{_b1Session}; {_routeIdCookie}"
    End Function

    ''' <summary>
    ''' Sesión 97: núcleo compartido de GetAsync/PostAsync — asegura
    ''' sesión activa (loguea si hace falta), ejecuta el request (vía
    ''' el delegate `construirRequest`, que arma un HttpRequestMessage
    ''' NUEVO cada vez que se lo invoca — un HttpRequestMessage no se
    ''' puede reenviar dos veces en .NET, de ahí que sea una función,
    ''' no un objeto ya armado), y si la respuesta es 401 (sesión
    ''' expirada — Service Layer expira por inactividad, ~30 min por
    ''' defecto, configurable del lado de SAP) re-loguea UNA vez y
    ''' reintenta — nunca más de un reintento, para no entrar en un
    ''' loop si las credenciales dejaron de ser válidas a mitad de
    ''' camino. Nunca lanza — cualquier excepción (red, timeout) se
    ''' traduce a Exitoso=False.
    ''' </summary>
    ''' <param name="descripcionOperacion">Solo para el Mensaje de error, ej. "GET '...'" o "POST '...'"</param>
    Private Async Function EjecutarConReintentoAsync(
        construirRequest As Func(Of HttpRequestMessage),
        descripcionOperacion As String
    ) As Task(Of ServiceLayerResult)
        If Not _SesionActiva Then
            Dim loginResult = Await LoginAsync()
            If Not loginResult.Exitoso Then
                Return loginResult
            End If
        End If

        Try
            Dim EnviarUnaVez =
                Async Function() As Task(Of HttpResponseMessage)
                    Dim req = construirRequest()
                    req.Headers.Add("Cookie", ArmarHeaderCookie())
                    Return Await _client.SendAsync(req)
                End Function

            Dim response = Await EnviarUnaVez()

            If response.StatusCode = HttpStatusCode.Unauthorized Then
                ' Sesión expirada — re-login una única vez y reintento.
                _SesionActiva = False
                Dim loginResult = Await LoginAsync()
                If Not loginResult.Exitoso Then
                    Return loginResult
                End If
                response = Await EnviarUnaVez()
            End If

            Dim contenido = Await response.Content.ReadAsStringAsync()
            Return New ServiceLayerResult With {
                .Exitoso = response.IsSuccessStatusCode,
                .StatusCode = CInt(response.StatusCode),
                .Contenido = contenido,
                .Mensaje = If(response.IsSuccessStatusCode, "OK.", $"{descripcionOperacion} rechazado: {response.StatusCode} - {contenido}")
            }
        Catch ex As Exception
            Return New ServiceLayerResult With {
                .Exitoso = False, .StatusCode = 0, .Contenido = "",
                .Mensaje = $"Error de conexión durante {descripcionOperacion}: {ex.Message}"
            }
        End Try
    End Function

    ''' <summary>
    ''' GET de solo lectura contra Service Layer, con la sesión ya
    ''' activa (login automático si hace falta, re-login automático
    ''' ante un 401) — ver EjecutarConReintentoAsync.
    ''' </summary>
    ''' <param name="relativeUrl">Ruta relativa a BaseUrl, ej. "BusinessPartners('P123')" (sin barra inicial).</param>
    Public Async Function GetAsync(relativeUrl As String) As Task(Of ServiceLayerResult)
        Return Await EjecutarConReintentoAsync(
            Function() New HttpRequestMessage(HttpMethod.Get, relativeUrl),
            $"GET '{relativeUrl}'"
        )
    End Function

    ''' <summary>
    ''' Sesión 97 (Etapa 4): POST genérico contra Service Layer, con la
    ''' sesión ya activa (mismo login/re-login automático que GetAsync).
    ''' Genérico a propósito — no sabe nada de qué documento de negocio
    ''' se está creando; eso es responsabilidad exclusiva del llamador
    ''' (ej. GrpoService.vb), que arma el JSON del body. Reutilizable
    ''' tal cual para cualquier escritura futura contra Service Layer
    ''' (Etapa 5, Factura Preliminar).
    ''' </summary>
    ''' <param name="relativeUrl">Ruta relativa a BaseUrl, ej. "PurchaseDeliveryNotes" (sin barra inicial).</param>
    ''' <param name="bodyJson">Body ya serializado a JSON — esta clase no conoce la forma del documento.</param>
    Public Async Function PostAsync(relativeUrl As String, bodyJson As String) As Task(Of ServiceLayerResult)
        Return Await EjecutarConReintentoAsync(
            Function()
                Dim req = New HttpRequestMessage(HttpMethod.Post, relativeUrl)
                req.Content = New StringContent(bodyJson, Encoding.UTF8, "application/json")
                Return req
            End Function,
            $"POST '{relativeUrl}'"
        )
    End Function

    ''' <summary>
    ''' Sesión 97 (Etapa 5, Sub-etapa 5.4): DELETE genérico contra
    ''' Service Layer — mismo criterio que PostAsync (genérico, sin
    ''' saber qué entidad se está borrando). Único uso hoy:
    ''' FacturaService.CancelarPreliminarAsync, DELETE /Drafts(DocEntry)
    ''' para anular un Preliminar todavía no confirmado como documento
    ''' definitivo en SAP. Un 204 (No Content, la respuesta típica de un
    ''' DELETE exitoso en Service Layer) cuenta como éxito igual que
    ''' cualquier otro 2xx — IsSuccessStatusCode ya lo cubre, sin
    ''' necesitar ningún caso especial acá.
    ''' </summary>
    ''' <param name="relativeUrl">Ruta relativa a BaseUrl, ej. "Drafts(123)" (sin barra inicial).</param>
    Public Async Function DeleteAsync(relativeUrl As String) As Task(Of ServiceLayerResult)
        Return Await EjecutarConReintentoAsync(
            Function() New HttpRequestMessage(HttpMethod.Delete, relativeUrl),
            $"DELETE '{relativeUrl}'"
        )
    End Function

    Public Sub Dispose() Implements IDisposable.Dispose
        _client?.Dispose()
        _handler?.Dispose()
    End Sub
End Class
