# Creates the Container App the first time. Later deploys only swap the image (GitHub Actions does that).
#
#     .\azure-create-app.ps1                 # deploys the image built from the current origin/main
#     .\azure-create-app.ps1 -Tag <sha>      # or a specific commit's image
#
# Secrets are Key Vault references: the app's managed identity reads them from the vault when a
# container starts, so their values never appear in the app's configuration.

param([string]$Tag)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\azure-config.ps1"

function Invoke-Az {
    # Judge success by az's exit code only. Windows PowerShell can otherwise treat anything az writes
    # to stderr (such as extension warnings) as a fatal error, and --only-show-errors hides warnings.
    $ErrorActionPreference = "Continue"
    $output = & az @args --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed" }
    return $output
}

if (-not $Tag) { $Tag = (git rev-parse origin/main).Trim() }

$identityId = Invoke-Az identity show -n $Identity -g $ResourceGroup --query id -o tsv
# The environment is in another resource group, so it's referenced by its full ID.
$environmentId = Invoke-Az containerapp env show -n $Environment -g $EnvironmentGroup --query id -o tsv
$vaultUri = Invoke-Az keyvault show -n $KeyVault -g $ResourceGroup --query properties.vaultUri -o tsv
$stored = Invoke-Az keyvault secret list --vault-name $KeyVault --query "[].name" -o tsv

# App secret name -> environment variable.
$wanted = [ordered]@{
    "anthropic-api-key" = "ANTHROPIC_API_KEY"
    "nonprofit-api-key"    = "NONPROFIT_API_KEY"
}
$secrets = @()
# Every question is paid Claude calls (about $0.005-0.15): 5 a minute per caller (src/rate_limit.py), and no
# new question once the day's spending could pass $2.00 (src/spend.py).
$envVars = @("NONPROFIT_RATE_LIMIT_PER_MINUTE=5", "NONPROFIT_DAILY_SPEND_CAP_USD=2.00")
foreach ($name in $wanted.Keys) {
    if ($stored -contains $name) {
        $secrets += "$name=keyvaultref:${vaultUri}secrets/$name,identityref:$identityId"
        $envVars += "$($wanted[$name])=secretref:$name"
    } elseif ($name -in "anthropic-api-key", "nonprofit-api-key") {
        throw "Secret $name is missing from $KeyVault; run azure-secrets.ps1 first"
    }
}

# At most ONE replica, and it must stay that way: the daily spend cap, the rate limit and "one question at a
# time" are counted in the process's memory, so a second replica would double what they allow. The agent
# mostly waits on the Claude and ProPublica APIs, so half a vCPU and 1 GiB are plenty.
Write-Host "Creating $App from ${Image}:$Tag (scale 0-1, 0.5 vCPU, 1 GiB)"
Invoke-Az containerapp create -n $App -g $ResourceGroup --environment $environmentId `
    --image "${Image}:$Tag" --target-port 8000 --ingress external `
    --min-replicas 0 --max-replicas 1 --cpu 0.5 --memory 1.0Gi `
    --user-assigned $identityId `
    --secrets @secrets --env-vars @envVars -o none

$fqdn = Invoke-Az containerapp show -n $App -g $ResourceGroup --query properties.configuration.ingress.fqdn -o tsv
Write-Host "`nDone: https://$fqdn/health"
