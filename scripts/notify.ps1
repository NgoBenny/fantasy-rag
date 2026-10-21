# Shows a Windows toast (built-in WinRT API, no extra module). Never fails the caller:
# a job's failure is already in data/logs; the toast is only the nudge.
#   powershell -File scripts/notify.ps1 -Title "fantasy-rag" -Message "news job failed"
param([string]$Title = "fantasy-rag", [Parameter(Mandatory)] [string]$Message)
try {
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
    $t = [Security.SecurityElement]::Escape($Title)
    $m = [Security.SecurityElement]::Escape($Message)
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$t</text><text>$m</text></binding></visual></toast>")
    # Windows PowerShell's registered app ID, so the toast shows without registering our own.
    $app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show(
        [Windows.UI.Notifications.ToastNotification]::new($xml))
} catch {
    Write-Output "toast failed: $_"
}
