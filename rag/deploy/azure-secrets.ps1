# Stores the rag API's secrets in Key Vault. Run this yourself, from the rag\deploy folder:
#
#     .\azure-secrets.ps1
#
# - anthropic-api-key: asked for with hidden input, so it never appears on screen, in a file, or in chat.
#   Use a key created just for this deployment, with a spending limit.
# - rag-api-key: generated randomly the first time (the key callers send in X-API-Key). Re-running
#   keeps it; pass -RotateApiKey to replace it.
# - langfuse-public-key / langfuse-secret-key: copied from ..\.env if present; otherwise tracing is off.
#
# If storing fails with "Forbidden", your permission from azure-setup.ps1 hasn't propagated yet:
# wait a minute or two and run it again.

#
# If pasting into the hidden prompt only registers one character (some terminals, including VS Code's,
# send Ctrl+V as a single keystroke there), copy the key and run with -FromClipboard instead. The
# clipboard is cleared afterwards.

param([switch]$RotateApiKey, [switch]$FromClipboard)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\azure-config.ps1"

function Set-Secret([string]$Name, [string]$Value) {
    # Pass the value in a temporary file, not on the command line, where other processes on this
    # machine could read it. The file is deleted immediately afterwards.
    $tmp = New-TemporaryFile
    try {
        [IO.File]::WriteAllText($tmp, $Value)  # UTF-8, no byte-order mark, no trailing newline
        az keyvault secret set --vault-name $KeyVault -n $Name --file $tmp -o none
        if ($LASTEXITCODE -ne 0) { throw "Storing $Name failed" }
        Write-Host "  stored $Name"
    } finally {
        Remove-Item $tmp -Force
    }
}

function Test-SecretExists([string]$Name) {
    $found = az keyvault secret list --vault-name $KeyVault --query "[?name=='$Name'].name" -o tsv
    if ($LASTEXITCODE -ne 0) { throw "Couldn't list secrets in $KeyVault" }
    return [bool]$found
}

# 1. Anthropic key
if ($FromClipboard) {
    $anthropicKey = "$(Get-Clipboard -Raw)".Trim()
    Set-Clipboard -Value " "  # don't leave the key sitting on the clipboard
} else {
    $secure = Read-Host "Paste the Anthropic API key for this deployment (input is hidden)" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $anthropicKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr).Trim()
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}
if (-not $anthropicKey.StartsWith("sk-ant-")) {
    # Report only the length, never the characters, so the message is safe to share.
    throw ("That doesn't look like an Anthropic key: expected it to start with sk-ant-, and received " +
        "$($anthropicKey.Length) characters. If that's 1, the paste didn't register; try -FromClipboard.")
}
Set-Secret "anthropic-api-key" $anthropicKey
$anthropicKey = $null

# 2. The API's own key
if ($RotateApiKey -or -not (Test-SecretExists "rag-api-key")) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $apiKey = [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
    Set-Secret "rag-api-key" $apiKey
    $apiKey = $null
} else {
    Write-Host "  rag-api-key already exists; keeping it (use -RotateApiKey to replace it)"
}

# 3. Langfuse keys from rag\.env
$envFile = Join-Path $PSScriptRoot "..\.env"
if (Test-Path $envFile) {
    $values = @{}
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*(LANGFUSE_PUBLIC_KEY|LANGFUSE_SECRET_KEY)\s*=\s*(.+?)\s*$') { $values[$Matches[1]] = $Matches[2] }
    }
    if ($values.Count -eq 2) {
        Set-Secret "langfuse-public-key" $values["LANGFUSE_PUBLIC_KEY"]
        Set-Secret "langfuse-secret-key" $values["LANGFUSE_SECRET_KEY"]
    } else {
        Write-Host "  Langfuse keys not found in rag\.env; the deployed app will run without tracing"
    }
}

Write-Host "`nDone. To call the deployed API you'll need its key; print it only when you need it:"
Write-Host "  az keyvault secret show --vault-name $KeyVault -n rag-api-key --query value -o tsv"
