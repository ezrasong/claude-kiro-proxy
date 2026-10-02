# Install the launcher for new Windows PowerShell and PowerShell 7 terminals.
# Existing profile contents are preserved; a backup is made before appending.
$ErrorActionPreference = 'Stop'
$taskActivationPath = Join-Path $PSScriptRoot 'activate.ps1'
$taskActivationLine = ". '" + $taskActivationPath.Replace("'", "''") + "'"
$taskDocuments = [Environment]::GetFolderPath('MyDocuments')
if (-not $taskDocuments) {
    throw 'Could not locate the current user Documents directory.'
}
foreach ($taskShellDirectory in @('WindowsPowerShell', 'PowerShell')) {
    $taskProfileDirectory = Join-Path $taskDocuments $taskShellDirectory
    $taskProfilePath = Join-Path $taskProfileDirectory 'profile.ps1'
    $taskProfileText = ''
    if (Test-Path -LiteralPath $taskProfilePath) {
        $taskProfileText = Get-Content -LiteralPath $taskProfilePath -Raw
        if ($taskProfileText -and $taskProfileText.Contains($taskActivationLine)) {
            Write-Host "Already installed: $taskProfilePath"
            continue
        }
        $taskBackupPath = $taskProfilePath + '.before-claude-kiro-' + [Guid]::NewGuid().ToString('N')
        Copy-Item -LiteralPath $taskProfilePath -Destination $taskBackupPath
        Write-Host "Profile backup: $taskBackupPath"
    }
    [IO.Directory]::CreateDirectory($taskProfileDirectory) | Out-Null
    $taskProfileText += "`r`n# Claude Kiro Proxy launcher`r`n" + $taskActivationLine + "`r`n"
    [IO.File]::WriteAllText($taskProfilePath, $taskProfileText, [Text.UTF8Encoding]::new($true))
    Write-Host "Installed: $taskProfilePath"
}
. $taskActivationPath
Write-Host 'New PowerShell terminals will route caveman claude through this proxy automatically.'
