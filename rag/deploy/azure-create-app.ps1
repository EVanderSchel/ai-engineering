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
$vaultUri = Invoke-Az keyvault show -n $KeyVault -g $ResourceGroup --query properties.vaultUri -o tsv
$stored = Invoke-Az keyvault secret list --vault-name $KeyVault --query "[].name" -o tsv

# App secret name -> environment variable. Langfuse is optional.
$wanted = [ordered]@{
    "anthropic-api-key"   = "ANTHROPIC_API_KEY"
    "rag-api-key"         = "RAG_API_KEY"
    "langfuse-public-key" = "LANGFUSE_PUBLIC_KEY"
    "langfuse-secret-key" = "LANGFUSE_SECRET_KEY"
}
$secrets = @()
$envVars = @("LANGFUSE_BASE_URL=https://cloud.langfuse.com", "LANGFUSE_TRACING_ENVIRONMENT=production")
foreach ($name in $wanted.Keys) {
    if ($stored -contains $name) {
        $secrets += "$name=keyvaultref:${vaultUri}secrets/$name,identityref:$identityId"
        $envVars += "$($wanted[$name])=secretref:$name"
    } elseif ($name -in "anthropic-api-key", "rag-api-key") {
        throw "Secret $name is missing from $KeyVault; run azure-secrets.ps1 first"
    }
}

Write-Host "Creating $App from ${Image}:$Tag (scale 0-1, 1 vCPU, 2 GiB)"
Invoke-Az containerapp create -n $App -g $ResourceGroup --environment $Environment `
    --image "${Image}:$Tag" --target-port 8000 --ingress external `
    --min-replicas 0 --max-replicas 1 --cpu 1.0 --memory 2.0Gi `
    --user-assigned $identityId `
    --secrets @secrets --env-vars @envVars -o none

$fqdn = Invoke-Az containerapp show -n $App -g $ResourceGroup --query properties.configuration.ingress.fqdn -o tsv
Write-Host "`nDone: https://$fqdn/health"
