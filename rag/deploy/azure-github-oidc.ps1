# One-time setup that lets GitHub Actions deploy to Azure without storing any password.
#
# How it works (OIDC federation): Azure is told to trust tokens that GitHub issues to workflow jobs in
# this repository's "rag-production" environment. When the deploy job runs, GitHub gives it a token
# lasting minutes; azure/login trades it for Azure access as the app registration below. A token from
# any other repo, branch, or environment is rejected.
#
# The deploy identity gets only what updating the app needs:
#   - Container Apps Contributor on the resource group (container apps only: no Key Vault, no deletes
#     of other resources)
#   - Managed Identity Operator on id-rag-api (Azure requires it to update an app that uses that identity)
#
# Also creates the GitHub environment with you as required reviewer, restricted to main, and stores the
# three IDs azure/login needs as environment variables (they're identifiers, not secrets).
#
# Prerequisites: az login, gh auth login (as a repo admin). Safe to re-run.

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\azure-config.ps1"

$Repo = "EVanderSchel/ai-engineering"
$GitHubEnvironment = "rag-production"
$AppRegistration = "github-rag-deploy"

function Invoke-Az {
    $ErrorActionPreference = "Continue"
    $output = & az @args --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed" }
    return $output
}

function Invoke-Gh {
    $ErrorActionPreference = "Continue"
    $output = & gh @args
    if ($LASTEXITCODE -ne 0) { throw "gh $($args -join ' ') failed" }
    return $output
}

$subscriptionId = Invoke-Az account show --query id -o tsv
$tenantId = Invoke-Az account show --query tenantId -o tsv

Write-Host "App registration $AppRegistration (the identity GitHub Actions deploys as)"
$appId = Invoke-Az ad app list --display-name $AppRegistration --query "[0].appId" -o tsv
if (-not $appId) {
    $appId = Invoke-Az ad app create --display-name $AppRegistration --query appId -o tsv
}
$spId = Invoke-Az ad sp list --filter "appId eq '$appId'" --query "[0].id" -o tsv
if (-not $spId) {
    $spId = Invoke-Az ad sp create --id $appId --query id -o tsv
}

Write-Host "Trusting GitHub tokens for repo:${Repo}:environment:$GitHubEnvironment only"
$credentialName = "github-$GitHubEnvironment"
$existing = Invoke-Az ad app federated-credential list --id $appId --query "[?name=='$credentialName'].name" -o tsv
if (-not $existing) {
    $tmp = New-TemporaryFile
    try {
        @{
            name      = $credentialName
            issuer    = "https://token.actions.githubusercontent.com"
            subject   = "repo:${Repo}:environment:$GitHubEnvironment"
            audiences = @("api://AzureADTokenExchange")
        } | ConvertTo-Json | Set-Content -Path $tmp -Encoding ascii
        Invoke-Az ad app federated-credential create --id $appId --parameters "@$tmp" -o none
    } finally {
        Remove-Item $tmp -Force
    }
}

Write-Host "Granting least-privilege roles"
$rgId = Invoke-Az group show -n $ResourceGroup --query id -o tsv
$identityId = Invoke-Az identity show -n $Identity -g $ResourceGroup --query id -o tsv
Invoke-Az role assignment create --assignee-object-id $spId --assignee-principal-type ServicePrincipal `
    --role "Container Apps Contributor" --scope $rgId -o none
Invoke-Az role assignment create --assignee-object-id $spId --assignee-principal-type ServicePrincipal `
    --role "Managed Identity Operator" --scope $identityId -o none

Write-Host "GitHub environment ${GitHubEnvironment}: you approve every deploy; only main can deploy"
$me = Invoke-Gh api user --jq .id
$tmp = New-TemporaryFile
try {
    @{
        reviewers                = @(@{ type = "User"; id = [int]$me })
        deployment_branch_policy = @{ protected_branches = $false; custom_branch_policies = $true }
    } | ConvertTo-Json -Depth 5 | Set-Content -Path $tmp -Encoding ascii
    Invoke-Gh api -X PUT "repos/$Repo/environments/$GitHubEnvironment" --input $tmp | Out-Null
} finally {
    Remove-Item $tmp -Force
}
$policies = Invoke-Gh api "repos/$Repo/environments/$GitHubEnvironment/deployment-branch-policies" --jq ".branch_policies[].name"
if ($policies -notcontains "main") {
    Invoke-Gh api -X POST "repos/$Repo/environments/$GitHubEnvironment/deployment-branch-policies" -f name=main | Out-Null
}

Write-Host "Storing the IDs azure/login needs as environment variables"
Invoke-Gh variable set AZURE_CLIENT_ID --env $GitHubEnvironment --repo $Repo --body $appId | Out-Null
Invoke-Gh variable set AZURE_TENANT_ID --env $GitHubEnvironment --repo $Repo --body $tenantId | Out-Null
Invoke-Gh variable set AZURE_SUBSCRIPTION_ID --env $GitHubEnvironment --repo $Repo --body $subscriptionId | Out-Null

Write-Host "`nDone."
