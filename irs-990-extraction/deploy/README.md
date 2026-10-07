# Deploying the Form 990 extraction API to Azure

The API runs on Azure Container Apps, scaled to zero when idle (no charge while nobody calls it). Its
resources are in their own resource group, except the Container Apps environment: the subscription
allows only one, so the app runs in the rag project's (`cae-rag-demo`). Deleting `rg-irs990-demo` leaves
the rag project untouched; deleting `rg-rag-demo` would also take this app down. The scripts are adapted from
`rag/deploy/`. Run them from this folder in PowerShell, signed in with `az login` and `gh auth login`.

| Resource | Name |
|---|---|
| Resource group (Central US) | `rg-irs990-demo` |
| Key Vault (secrets) | `kv-irs990-<first 6 characters of the subscription ID>` |
| Managed identity (the app's, reads the vault) | `id-irs990-api` |
| Container Apps environment (shared, in `rg-rag-demo`) / app | `cae-rag-demo` / `ca-irs990-api` |
| Image | `ghcr.io/evanderschel/irs990-api` |
| GitHub environment (approval gate for deploys) | `irs990-production` |

## First deployment, in order

1. **Spending limits first.** In the Anthropic Console, create a workspace for this deployment with a
   monthly spend limit, and an API key in it: every `/extract` call on the public API spends it. Make sure
   an Azure budget covers this resource group (a subscription-scoped one covers every group).
2. `.\azure-setup.ps1`: resource group, Key Vault, and the app's managed identity (allowed only to read
   secrets in the vault); checks that the shared environment exists.
3. `.\azure-secrets.ps1` (run it yourself): the Anthropic key, typed at a hidden prompt, and a random
   API key for callers. Neither ever appears on screen, in a file, or in chat.
4. `.\azure-github-oidc.ps1`: lets GitHub Actions deploy without a stored password (OIDC), with the
   least privilege needed (on the shared environment, a custom role that can only run apps in it), and creates the `irs990-production` environment with you as required approver.
5. Merge to main: `irs-990-extraction-publish` builds, smoke-tests, and pushes the image; the deploy job
   waits for approval. In GitHub (Packages > irs990-api > Package settings), make the package **public**,
   so Azure can pull it without registry credentials. It contains no secrets: only code and prompts.
6. `.\azure-create-app.ps1 -Tag <commit SHA>`: creates the app from that image, with the secrets as Key
   Vault references (the values never appear in the app's configuration). Scale 0-1, 1 vCPU, 2 GiB.
7. `.\azure-health-probes.ps1` and `.\azure-alerts.ps1`: HTTP health probes on `/health`, and email
   alerts for server errors, repeated restarts, and unusual traffic (over 100 requests an hour).
8. Approve the waiting deploy job; it confirms `/health` reports the deployed commit.

After that, every merge to main publishes a new image and, once approved, deploys it.

## Using it

```
$key = az keyvault secret show --vault-name <vault> -n irs990-api-key --query value -o tsv
curl.exe -H "X-API-Key: $key" -F "file=@return.pdf;type=application/pdf" https://<app URL>/extract
```

The first request after an idle period takes about 30 seconds longer while a container starts.

## If the key leaks

`.\azure-secrets.ps1 -RotateApiKey`, then restart the app (`az containerapp revision restart`) so it reads
the new value. The rate limit (10 requests a minute) and the Anthropic workspace limit cap the damage
meanwhile.

## Removing it

`az group delete -n rg-irs990-demo` removes everything in Azure. The Key Vault name stays reserved for 90
days (soft delete); `az keyvault purge -n <vault>` frees it.
