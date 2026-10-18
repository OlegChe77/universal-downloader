' Silent launcher for the desktop shortcut. Keep this file ASCII only.
' Everything installed -> start the program hidden (it lives as an icon near the clock).
' Something missing or requirements.txt changed -> run start.ps1 in a visible window to show the installation.
Option Explicit
Dim fso, sh, dir, py, args, i
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
py = dir & "\python\python.exe"
args = ""
For i = 0 To WScript.Arguments.Count - 1
    args = args & " """ & WScript.Arguments(i) & """"
Next
sh.CurrentDirectory = dir

If fso.FileExists(py) And ReadAll(dir & "\requirements.txt") = ReadAll(dir & "\python\requirements.installed") Then
    sh.Run """" & py & """ app.py --tray" & args, 0, False
Else
    sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & dir & "\start.ps1""" & args, 1, False
End If

Function ReadAll(path)
    ReadAll = "-"
    If fso.FileExists(path) Then
        If fso.GetFile(path).Size > 0 Then ReadAll = fso.OpenTextFile(path).ReadAll()
    End If
End Function
