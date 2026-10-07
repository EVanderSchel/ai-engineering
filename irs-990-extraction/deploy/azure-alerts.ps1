# Email alerts for problems the budget alert can't see. Safe to re-run.
#
#   irs990-5xx-errors:  any server error (HTTP 5xx) in a 15-minute window
#   irs990-traffic:     more than 100 requests in an hour: every /extract call is a paid Claude call,
#                    so a surge (a leaked key, a runaway client) should be noticed within the hour
#   irs990-restarts:    more than 2 container restarts in 15 minutes (the liveness probe in
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

$ActionGroup = "ag-irs990-email"
$email = Invoke-Az account show --query user.name -o tsv
$appId = Invoke-Az containerapp show -n $App -g $ResourceGroup --query id -o tsv

Write-Host "Action group $ActionGroup -> $email"
Invoke-Az monitor action-group create -n $ActionGroup -g $ResourceGroup --short-name "irs990" `
    --action email owner $email -o none
$groupId = Invoke-Az monitor action-group show -n $ActionGroup -g $ResourceGroup --query id -o tsv

Write-Host "Alert irs990-5xx-errors: any HTTP 5xx in 15 minutes"
Invoke-Az monitor metrics alert create -n "irs990-5xx-errors" -g $ResourceGroup --scopes $appId `
    --condition "total Requests > 0 where statusCodeCategory includes 5xx" `
    --window-size 15m --evaluation-frequency 5m --severity 2 --action $groupId `
    --description "The Form 990 extraction API returned server errors (5xx). Find the request IDs and errors in the logs (Log Analytics: ContainerAppConsoleLogs_CL)." -o none

Write-Host "Alert irs990-restarts: more than 2 restarts in 15 minutes"
Invoke-Az monitor metrics alert create -n "irs990-restarts" -g $ResourceGroup --scopes $appId `
    --condition "total RestartCount > 2" `
    --window-size 15m --evaluation-frequency 5m --severity 2 --action $groupId `
    --description "The Form 990 extraction API container restarted repeatedly: likely failing its liveness probe (hung or crashing)." -o none

Write-Host "Alert irs990-traffic: more than 100 requests in an hour"
Invoke-Az monitor metrics alert create -n "irs990-traffic" -g $ResourceGroup --scopes $appId `
    --condition "total Requests > 100" `
    --window-size 1h --evaluation-frequency 15m --severity 3 --action $groupId `
    --description "Unusual traffic to the Form 990 API: each request spends Anthropic credits. Check who is calling (logs) and rotate the key if it leaked: azure-secrets.ps1 -RotateApiKey." -o none

Write-Host "Done."
