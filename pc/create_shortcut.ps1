# Creates "MirrorLink" shortcuts (Desktop + Start menu) that open the viewer
# without a console window, with its own icon. Pin either one to the taskbar.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyw  = Join-Path $here ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pyw)) {
    Write-Host "Run run.bat once first (it sets things up), then run this again."
    exit 1
}
$shell = New-Object -ComObject WScript.Shell
$targets = @(
    [Environment]::GetFolderPath("Desktop"),
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs")
)
foreach ($dir in $targets) {
    $lnk = $shell.CreateShortcut((Join-Path $dir "MirrorLink.lnk"))
    $lnk.TargetPath       = $pyw
    $lnk.Arguments        = '"' + (Join-Path $here "mirror.py") + '"'
    $lnk.WorkingDirectory = $here
    $lnk.IconLocation     = (Join-Path $here "mirrorlink.ico") + ",0"
    $lnk.Description      = "MirrorLink: your iPhone on your PC"
    $lnk.Save()
}
Write-Host ""
Write-Host "Done! A MirrorLink shortcut is on your Desktop and in the Start menu."
Write-Host "To pin it: open Start, type MirrorLink, right-click it, choose 'Pin to taskbar'."
