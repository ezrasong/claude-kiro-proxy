# Dot-source this once per terminal: . .\scripts\activate.ps1
# Only `caveman claude` enters the stack; other Caveman commands stay unchanged.
if (-not $global:ClaudeKiroCaveman) {
    $global:ClaudeKiroCaveman = (Get-Command caveman -CommandType Application,ExternalScript -ErrorAction Stop | Select-Object -First 1).Source
}
$global:ClaudeKiroController = Join-Path $PSScriptRoot 'stack.py'
$global:ClaudeKiroPython = (Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
function global:caveman {
    if ($args.Count -gt 0 -and $args[0] -eq 'claude') {
        $forward = @($args | Select-Object -Skip 1)
        & $global:ClaudeKiroPython $global:ClaudeKiroController run -- @forward
    } else {
        & $global:ClaudeKiroCaveman @args
    }
}
function global:kiro-account {
    & $global:ClaudeKiroPython $global:ClaudeKiroController account @args
}
Write-Host 'Ready: caveman claude --model kiro:claude-sonnet-4-5; kiro-account use <name>'
