Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

' 优先尝试当前用户的 pythonw，若不存在则回退到系统路径中的 pythonw
localPy = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python313\pythonw.exe"
If FSO.FileExists(localPy) Then
    py = localPy
Else
    py = "pythonw.exe"
End If

script = FSO.GetParentFolderName(WScript.ScriptFullName) & "\antigravity_bridge.py"
WshShell.Run """" & py & """ """ & script & """", 0, False
