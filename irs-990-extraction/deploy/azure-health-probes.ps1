# Give the Container App HTTP health checks on /health (without them, Azure only checks that the port
# accepts connections, so a hung app would never be restarted). Safe to re-run.
#
#   startup:   checked every 2 s, up to 30 failures (60 s) while the app imports and warms up; the other
#              two checks only start after this one passes
#   readiness: traffic is only sent to a replica that's answering
#   liveness:  3 failures in a row (90 s) restarts the replica
#
# /health does no search and calls no API, so these checks cost nothing. Later deploys only change the
# image, so the probes stay in place.

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\azure-config.ps1"

function Invoke-Az {
    $ErrorActionPreference = "Continue"
    $output = & az @args --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed" }
    return $output
}

function New-Probe([string]$Type, [int]$Period, [int]$Failures, [int]$Timeout) {
    [ordered]@{
        type             = $Type
        httpGet          = [ordered]@{ path = "/health"; port = 8000 }
        periodSeconds    = $Period
        failureThreshold = $Failures
        timeoutSeconds   = $Timeout
    }
}

# Read and write the app with the same stable API version: `az containerapp show` uses a preview API
# whose extra fields the stable API rejects.
$ApiVersion = "2026-07-01"
$appId = Invoke-Az containerapp show -n $App -g $ResourceGroup --query id -o tsv
$url = "https://management.azure.com$($appId)?api-version=$ApiVersion"
# Note: PowerShell variable names ignore case, so this must not be called $app ($App is the app name).
$appDetails = Invoke-Az rest --method get --url $url | Out-String | ConvertFrom-Json
$template = $appDetails.properties.template
$probes = @(
    (New-Probe "Startup" 2 30 2),
    (New-Probe "Readiness" 10 3 5),
    (New-Probe "Liveness" 30 3 5)
)
$template.containers[0] | Add-Member -NotePropertyName probes -NotePropertyValue $probes -Force

# Send the whole template back (Azure replaces it as a unit). Building the JSON with -Depth high enough
# that nested settings aren't flattened into strings.
$body = @{ properties = @{ template = $template } } | ConvertTo-Json -Depth 30
$tmp = New-TemporaryFile
try {
    Set-Content -Path $tmp -Value $body -Encoding ascii
    Write-Host "Setting startup/readiness/liveness probes on $App (this creates a new revision)"
    Invoke-Az rest --method patch --url $url --body "@$tmp" -o none
} finally {
    Remove-Item $tmp -Force
}

# Azure applies the change in the background, so a read straight after the PATCH can still show the old
# template (empty probes). Check again for up to 30 seconds before reporting.
$configured = ""
foreach ($attempt in 1..6) {
    $after = Invoke-Az rest --method get --url $url | Out-String | ConvertFrom-Json
    $configured = ($after.properties.template.containers[0].probes | ForEach-Object { $_.type }) -join ", "
    if ($configured) { break }
    Start-Sleep -Seconds 5
}
Write-Host "Probes now configured: $(if ($configured) { $configured } else { 'none yet (check again in a minute)' })"
