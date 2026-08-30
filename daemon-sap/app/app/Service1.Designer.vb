Imports System.ServiceProcess
Imports daemon_logical

<Global.Microsoft.VisualBasic.CompilerServices.DesignerGenerated()> _
Partial Class Service1
    Inherits System.ServiceProcess.ServiceBase

    'UserService reemplaza a Dispose para limpiar la lista de componentes.
    <System.Diagnostics.DebuggerNonUserCode()> _
    Protected Overrides Sub Dispose(ByVal disposing As Boolean)
        Try
            If disposing AndAlso components IsNot Nothing Then
                components.Dispose()
            End If
        Finally
            MyBase.Dispose(disposing)
        End Try
    End Sub

    ' Punto de entrada principal del proceso
    <MTAThread()> _
    <System.Diagnostics.DebuggerNonUserCode()> _
    Shared Sub Main()
        ' Sesión 95, Sub-etapa 3.2: flag opcional de línea de comandos
        ' para correr SOLO la prueba de humo de Service Layer (login +
        ' 1 GET de solo lectura), sin tocar HANA ni el ciclo real de
        ' sync de OC — pensado para verificación manual puntual de la
        ' Sub-etapa 3.1/3.2, no para uso permanente. El CardCode a
        ' consultar es opcional (2do argumento); si no se pasa, usa un
        ' proveedor real ya confirmado en la base de pruebas
        ' BK_1808261700 (mismo que se usó para verificar doc_cur,
        ' sesión 93).
        Dim args = Environment.GetCommandLineArgs()
        If args.Contains("--test-service-layer") Then
            Dim indice = Array.IndexOf(args, "--test-service-layer")
            Dim cardCode = If(indice + 1 < args.Length, args(indice + 1), "P20100055237")
            Console.WriteLine($"=== daemon-sap: prueba de humo de Service Layer (sesión 95) — CardCode='{cardCode}' ===")
            ServiceLayerSmokeTest.Ejecutar(cardCode).GetAwaiter().GetResult()
            Return
        End If

        ' Sesión 97, Etapa 4: flag manual para correr SOLO el ciclo de
        ' creación de GRPO — deliberadamente NO wireado al WorkerThread
        ' permanente todavía (mismo criterio que --test-service-layer):
        ' esta es la primera vez que el demonio CREA documentos reales
        ' en SAP, y hacerlo automático cada 17s sin que el usuario haya
        ' validado el comportamiento primero sería un riesgo real, no
        ' solo una preferencia de estilo. Wireado al loop automático
        ' recién cuando se pida explícitamente en una sesión futura.
        If args.Contains("--sync-grpo") Then
            Console.WriteLine("=== daemon-sap: ciclo de creación de GRPO (Etapa 4, sesión 97) ===")
            Dim grpoSync As New GrpoSyncService()
            grpoSync.ExecuteGrpoSync().GetAwaiter().GetResult()
            Return
        End If

        ' Sesión 97, Etapa 5: 3 flags manuales independientes, uno por
        ' cada sub-etapa (5.1/5.2 creación, 5.3 reconciliación, 5.4
        ' cancelación) — mismo criterio que --sync-grpo/--test-service-
        ' layer: deliberadamente NO wireados a WorkerThread todavía.
        ' Ninguno de los 3 depende de los otros para ejecutarse (cada
        ' uno abre/cierra su propia sesión de Service Layer).
        If args.Contains("--sync-factura-preliminar") Then
            Console.WriteLine("=== daemon-sap: ciclo de creación del Preliminar de Factura (Etapa 5.1/5.2, sesión 97) ===")
            Dim facturaSync As New FacturaSyncService()
            facturaSync.ExecutePreliminarSync().GetAwaiter().GetResult()
            Return
        End If

        If args.Contains("--sync-factura-reconciliar") Then
            Console.WriteLine("=== daemon-sap: ciclo de reconciliación de Factura (Etapa 5.3, sesión 97) ===")
            Dim facturaSync As New FacturaSyncService()
            facturaSync.ExecuteReconciliacionSync().GetAwaiter().GetResult()
            Return
        End If

        If args.Contains("--sync-factura-cancelar") Then
            Console.WriteLine("=== daemon-sap: ciclo de cancelación de Factura (Etapa 5.4, sesión 97) ===")
            Dim facturaSync As New FacturaSyncService()
            facturaSync.ExecuteCancelacionSync().GetAwaiter().GetResult()
            Return
        End If

        ' Sesión 92: modo consola para pruebas manuales end-to-end, sin
        ' instalar el servicio de Windows — Environment.UserInteractive
        ' es True cuando el proceso corre desde una consola real (False
        ' cuando lo lanza el Service Control Manager). Llama a OnStart/
        ' OnStop directo (son Protected, accesibles desde acá porque
        ' Main es un miembro más de esta misma clase Service1) — mismo
        ' código que correría el servicio real instalado, sin duplicar
        ' ninguna lógica.
        If Environment.UserInteractive Then
            Dim svc As New Service1()
            Console.WriteLine("=== daemon-sap: modo consola (sesión 92) — presione ENTER para detener ===")
            svc.OnStart(New String() {})
            Dim linea As String = Console.ReadLine()
            If linea Is Nothing Then
                ' Sin stdin interactivo real conectado (EOF inmediato,
                ' caso de una prueba automatizada sin terminal real) —
                ' correr un tiempo fijo para permitir al menos un ciclo
                ' completo de sincronización antes de detenerse solo, en
                ' vez de salir al instante. En un uso manual real (consola
                ' real con teclado), ReadLine bloquea normalmente hasta
                ' que el usuario presiona ENTER — este bloque no cambia
                ' ese comportamiento.
                Console.WriteLine("(sin entrada interactiva detectada — corriendo 90s de prueba antes de detenerse solo)")
                System.Threading.Thread.Sleep(90000)
            End If
            svc.OnStop()
            Return
        End If

        Dim ServicesToRun() As System.ServiceProcess.ServiceBase

        ' Puede que más de un servicio de NT se ejecute con el mismo proceso. Para agregar
        ' otro servicio a este proceso, cambie la siguiente línea para
        ' crear un segundo objeto de servicio. Por ejemplo,
        '
        '   ServicesToRun = New System.ServiceProcess.ServiceBase () {New Service1, New MySecondUserService}
        '
        ServicesToRun = New System.ServiceProcess.ServiceBase() {New Service1}

        System.ServiceProcess.ServiceBase.Run(ServicesToRun)
    End Sub

    'Requerido por el Diseñador de componentes
    Private components As System.ComponentModel.IContainer

    ' NOTA: el Diseñador de componentes requiere el siguiente procedimiento
    ' Se puede modificar usando el Diseñador de componentes.
    ' No lo modifique con el editor de código.
    <System.Diagnostics.DebuggerStepThrough()> _
    Private Sub InitializeComponent()
        components = New System.ComponentModel.Container()
        Me.ServiceName = "Service1"
    End Sub

End Class
