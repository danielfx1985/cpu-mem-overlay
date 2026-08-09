Option Explicit

Dim fso, sh, dir, exe, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir

exe = dir & "\dist\CpuMemOverlay.exe"
If fso.FileExists(exe) Then
  cmd = """" & exe & """"
ElseIf fso.FileExists(dir & "\CpuMemOverlay.exe") Then
  cmd = """" & dir & "\CpuMemOverlay.exe"""
Else
  cmd = "pythonw """ & dir & "\main.py"""
End If

sh.Run cmd, 0, False
