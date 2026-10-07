# One-time Azure setup for the Form 990 extraction API: resource group, Key Vault, and the app's managed
# identity. The app runs in the rag project's Container Apps environment (the subscription allows one). Safe to re-run; existing resources are left as they are.
#
# Prerequisites: Azure CLI logged in (az login) with the right subscription selected.
# Afterwards: run azure-secrets.ps1 to store the secrets, then azure-create-app.ps1.
# To delete everything: az group delete -n rg-irs990-demo   (the Key Vault name then stays
# reserved for 90 days by soft-delete; purge it with az keyvault purge -n <name> to reuse it).

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

Write-Host "Resource group $ResourceGroup ($Location)"
Invoke-Az group create -n $ResourceGroup -l $Location -o none

Write-Host "Key Vault $KeyVault (access controlled by Azure roles, not vault access policies)"
$existing = Invoke-Az keyvault list -g $ResourceGroup --query "[?name=='$KeyVault'].id" -o tsv
if (-not $existing) {
    Invoke-Az keyvault create -n $KeyVault -g $ResourceGroup -l $Location --enable-rbac-authorization true -o none
}
$kvId = Invoke-Az keyvault show -n $KeyVault -g $ResourceGroup --query id -o tsv

Write-Host "Letting you (the signed-in user) manage secrets in the vault"
$me = Invoke-Az ad signed-in-user show --query id -o tsv
Invoke-Az role assignment create --assignee-object-id $me --assignee-principal-type User `
    --role "Key Vault Secrets Officer" --scope $kvId -o none

Write-Host "Managed identity $Identity, allowed only to read secrets in this vault"
Invoke-Az identity create -n $Identity -g $ResourceGroup -l $Location -o none
$principalId = Invoke-Az identity show -n $Identity -g $ResourceGroup --query principalId -o tsv
Invoke-Az role assignment create --assignee-object-id $principalId --assignee-principal-type ServicePrincipal `
    --role "Key Vault Secrets User" --scope $kvId -o none

Write-Host "Container Apps environment: sharing $Environment in $EnvironmentGroup"
$envExists = Invoke-Az containerapp env list -g $EnvironmentGroup --query "[?name=='$Environment'].id" -o tsv
if (-not $envExists) {
    throw "$Environment not found in ${EnvironmentGroup}: the rag project's rag/deploy/azure-setup.ps1 creates it"
}

Write-Host "`nDone. Next: .\azure-secrets.ps1"
