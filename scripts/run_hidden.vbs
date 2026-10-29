' Runs its arguments as one command line with no console window, waits, and returns the
' command's exit code, so scheduled tasks never pop up a window (register_daily_ingest.ps1).
Dim cmd, a, i
For i = 0 To WScript.Arguments.Count - 1
  a = WScript.Arguments(i)
  If InStr(a, " ") > 0 Then a = """" & a & """"
  If i > 0 Then cmd = cmd & " "
  cmd = cmd & a
Next
WScript.Quit CreateObject("WScript.Shell").Run(cmd, 0, True)
