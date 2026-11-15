' Starts the recorder, detector and dashboard with no console windows.
' Works from whatever folder this file is in, so no paths to edit.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir

py = dir & "\venv\Scripts\pythonw.exe"
If Not fso.FileExists(py) Then
    MsgBox "Could not find " & py & vbCrLf & "Create the venv first (see README.md).", 16, "Why is this slow?"
    WScript.Quit
End If

q = Chr(34)
sh.Run q & py & q & " " & q & dir & "\recorder.py" & q, 0, False
sh.Run q & py & q & " " & q & dir & "\detector.py" & q, 0, False
sh.Run q & py & q & " " & q & dir & "\dashboard.py" & q & " --no-browser", 0, False