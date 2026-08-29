' Proyecto: daemon_logical / ServiceLayerSmokeTest.vb
'
' Sesión 95, Sub-etapa 3.2: prueba de humo manual, disparada solo con
' el flag de línea de comandos "--test-service-layer" (ver Main() en
' app/Service1.Designer.vb) — NUNCA corre como parte del ciclo normal
' de sincronización de OC (WorkerThread/SyncService), ni al instalar
' el demonio como servicio de Windows. Es exclusivamente una
' herramienta de verificación manual de la Sub-etapa 3.1/3.2 del plan
' de integración VB.NET ↔ Portal.
'
' Orquesta daemon_data.SapServiceLayerClient (que nunca loggea por sí
' solo, ver el comentario en ese archivo) + Logger — mismo principio
' de separación de capas ya usado en SyncService.vb (orquesta
' SyncSAPB1 + el envío al Portal + Logger).
Imports daemon_data
Imports Newtonsoft.Json

Public Class ServiceLayerSmokeTest

    ''' <summary>
    ''' Login contra Service Layer + un GET de solo lectura sobre un
    ''' BusinessPartner real conocido de la base de pruebas, para
    ''' confirmar que la sesión realmente sirve para operar (no solo
    ''' que el login devuelve 200). Nunca lanza — cualquier fallo queda
    ''' registrado en el log y se devuelve como resultado, sin
    ''' interrumpir el proceso. Además de loggear, imprime a consola
    ''' (Console.WriteLine) para que quede visible en el modo consola
    ''' usado para esta verificación manual.
    ''' </summary>
    ''' <param name="cardCodeDePrueba">CardCode real a consultar, ej. un proveedor ya confirmado en la base de pruebas.</param>
    Public Shared Async Function Ejecutar(cardCodeDePrueba As String) As Task(Of Boolean)
        Logger.Escribir("[SL-SMOKE] Iniciando prueba de humo de Service Layer (Sub-etapa 3.2).")
        Console.WriteLine("[SL-SMOKE] Iniciando prueba de humo de Service Layer...")

        Dim cliente As New SapServiceLayerClient()
        Try
            Dim loginResult = Await cliente.LoginAsync()
            If Not loginResult.Exitoso Then
                Logger.Escribir($"[SL-SMOKE-FAIL] Login falló: {loginResult.Mensaje}")
                Console.WriteLine($"[SL-SMOKE-FAIL] Login falló: {loginResult.Mensaje}")
                Return False
            End If
            Logger.Escribir($"[SL-SMOKE] Login OK. SessionId={cliente.SessionId} RouteId={If(cliente.RouteId, "(sin balanceo)")}")
            Console.WriteLine($"[SL-SMOKE] Login OK. SessionId={cliente.SessionId} RouteId={If(cliente.RouteId, "(sin balanceo)")}")

            Dim getResult = Await cliente.GetAsync($"BusinessPartners('{cardCodeDePrueba}')")
            If Not getResult.Exitoso Then
                Logger.Escribir($"[SL-SMOKE-FAIL] GET BusinessPartners('{cardCodeDePrueba}') falló: {getResult.Mensaje}")
                Console.WriteLine($"[SL-SMOKE-FAIL] GET BusinessPartners('{cardCodeDePrueba}') falló: {getResult.Mensaje}")
                Return False
            End If

            ' Extrae unos pocos campos clave para un log/consola legibles
            ' — no vuelca el JSON completo (puede traer decenas de
            ' campos) al log de texto plano del demonio; el contenido
            ' completo sí se imprime a consola, para esta verificación
            ' puntual.
            Dim cardName As String = "?", email As String = "?"
            Try
                Dim json = JsonConvert.DeserializeObject(Of Dictionary(Of String, Object))(getResult.Contenido)
                If json.ContainsKey("CardName") Then cardName = json("CardName").ToString()
                If json.ContainsKey("EmailAddress") Then email = If(json("EmailAddress")?.ToString(), "")
            Catch
                ' Si el body no es el JSON esperado, se deja "?" — no es
                ' motivo de falla, ya se confirmó el HTTP 200 arriba.
            End Try

            Logger.Escribir($"[SL-SMOKE] GET BusinessPartners('{cardCodeDePrueba}') -> HTTP {getResult.StatusCode}. CardName='{cardName}' EmailAddress='{email}' ({getResult.Contenido.Length} bytes).")
            Logger.Escribir("[SL-SMOKE] Prueba de humo EXITOSA — sesión de Service Layer confirmada operativa.")
            Console.WriteLine($"[SL-SMOKE] GET BusinessPartners('{cardCodeDePrueba}') -> HTTP {getResult.StatusCode}. CardName='{cardName}' EmailAddress='{email}'")
            Console.WriteLine("[SL-SMOKE] --- Contenido completo de la respuesta ---")
            Console.WriteLine(getResult.Contenido)
            Console.WriteLine("[SL-SMOKE] Prueba de humo EXITOSA.")
            Return True
        Finally
            cliente.Dispose()
        End Try
    End Function

End Class
