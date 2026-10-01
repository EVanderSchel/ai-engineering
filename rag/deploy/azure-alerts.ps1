# Email alerts for problems the budget alert can't see. Safe to re-run.
#
#   rag-5xx-errors:  any server error (HTTP 5xx) in a 15-minute window
#   rag-restarts:    more than 2 container restarts in 15 minutes (the liveness probe in
#                    azure-health-probes.ps1 restarts a hung replica; repeated restarts mean it keeps hanging)
#
# Alerts go to the email of the signed-in Azure account. Checked every 5 minutes; an alert resolves by
# itself once the window is clean again.

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\azure-config.ps1"

function Invoke-Az {
    $ErrorActionPreference = "Continue"
    $output = & az @args --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed" }
    return $output
}

$ActionGroup = "ag-rag-email"
$email = Invoke-Az account show --query user.name -o tsv
$appId = Invoke-Az containerapp show -n $App -g $ResourceGroup --query id -o tsv

Write-Host "Action group $ActionGroup -> $email"
Invoke-Az monitor action-group create -n $ActionGroup -g $ResourceGroup --short-name "rag-alerts" `
    --action email owner $email -o none
$groupId = Invoke-Az monitor action-group show -n $ActionGroup -g $ResourceGroup --query id -o tsv

Write-Host "Alert rag-5xx-errors: any HTTP 5xx in 15 minutes"
Invoke-Az monitor metrics alert create -n "rag-5xx-errors" -g $ResourceGroup --scopes $appId `
    --condition "total Requests > 0 where statusCodeCategory includes 5xx" `
    --window-size 15m --evaluation-frequency 5m --severity 2 --action $groupId `
    --description "The RAG API returned server errors (5xx). Find the request IDs in the logs or Langfuse." -o none

Write-Host "Alert rag-restarts: more than 2 restarts in 15 minutes"
Invoke-Az monitor metrics alert create -n "rag-restarts" -g $ResourceGroup --scopes $appId `
    --condition "total RestartCount > 2" `
    --window-size 15m --evaluation-frequency 5m --severity 2 --action $groupId `
    --description "The RAG API container restarted repeatedly: likely failing its liveness probe (hung or crashing)." -o none

Write-Host "Done."
