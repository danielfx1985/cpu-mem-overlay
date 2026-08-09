Option Explicit

Dim fso, sh, dir, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir

cmd = "pythonw """ & dir & "\main.py"""
sh.Run cmd, 0, False
