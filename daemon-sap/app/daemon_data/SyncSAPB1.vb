' Proyecto: daemon.Data
Imports Sap.Data.Hana
Imports System.Collections.Generic

Public Class SyncSAPB1

    ' Obtiene las cabeceras de cotizaciones pendientes
    Public Function GetPendingHeaders() As DataTable
        Dim dt As New DataTable()
        Using conn As HanaConnection = HanaConnectionManager.GetHanaConnection()
            ' U_DRYZ_PC = '1' es nuestro filtro de control
            Dim sql As String = "SELECT T0.""DocEntry"", T0.""DocNum"", T0.""CardCode"", T0.""CardName"" " &
                                ", (select IFNULL(T1.""E_Mail"", 'clozano@daryza.com') from OCRD T1 where T1.""CardCode"" = T0.""CardCode"") as ""E_Mail"" " &
                                ", T0.""U_MSS_TDB"" " &
                                "FROM OPOR T0 WHERE T0.""DocType""='I' and T0.""DocStatus"" = 'O' AND T0.""U_DRYZ_PC"" = '1'"
            Dim cmd As New HanaCommand(sql, conn)
            Dim adapter As New HanaDataAdapter(cmd)
            adapter.Fill(dt)
        End Using
        Return dt
    End Function

    ' Obtiene el detalle de artículos (PQT1) para un DocEntry específico
    ' Sesión 92: se agregan PriceBefDi/LineTotal/TaxCode — datos de
    ' precio/impuesto que ya existen en SAP al momento de aceptar la OC,
    ' confirmados por el usuario (columnas reales de POR1, no inventadas
    ' ni calculadas acá). Ver CLAUDE.md sesión 92 para el detalle
    ' completo del payload nuevo que esto habilita del lado de Django.
    Public Function GetLinesByDocEntry(docEntry As Integer) As DataTable
        Dim dt As New DataTable()
        Using conn As HanaConnection = HanaConnectionManager.GetHanaConnection()
            Dim sql As String = "SELECT t1.""LineNum"", t1.""ItemCode"", t1.""Dscription"", t1.""OpenQty"" as ""Quantity"", t1.""UomCode"" as ""UM""  " &
                               ", t1.""PriceBefDi"" as ""Unit Price"", t1.""LineTotal"" as ""Line Total"", t1.""TaxCode"" as ""Tax Code"" " &
                               ",(select case when t0.""U_MSS_TDB""='MP' then true else false end from OPOR t0 where t0.""DocEntry""=t1.""DocEntry"") as ""requiere_coa"" " &
                                "FROM POR1 t1 WHERE t1.""LineStatus"" = 'O' and  t1.""DocEntry"" = ?" '& docEntry.ToString()
            Dim cmd As New HanaCommand(sql, conn)
            cmd.Parameters.Add(New HanaParameter("p1", HanaDbType.Integer)).Value = docEntry

            Dim adapter As New HanaDataAdapter(cmd)
            adapter.Fill(dt)
        End Using
        Return dt
    End Function

    ' Actualiza el flag en SAP solo tras confirmación de la API
    Public Sub MarkAsSynced(docEntry As Integer)
        Using conn As HanaConnection = HanaConnectionManager.GetHanaConnection()
            Dim sql As String = "UPDATE OPOR SET ""U_DRYZ_PC"" = '2' WHERE ""DocEntry"" = ?"
            Dim cmd As New HanaCommand(sql, conn)
            cmd.Parameters.Add(New HanaParameter("p1", HanaDbType.Integer)).Value = docEntry
            cmd.ExecuteNonQuery()
        End Using
    End Sub
End Class
