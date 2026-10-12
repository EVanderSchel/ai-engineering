# Deploying the nonprofit research agent API to Azure

The API ([`src/api.py`](../src/api.py)) runs on Azure Container Apps, scaled to zero when idle (no charge while nobody calls it), with at most one replica. Its resources are in their own resource group, except the Container Apps environment: the subscription allows only one, so the app runs in the rag project's (`cae-rag-demo`), like the irs-990-extraction API. Deleting `rg-nonprofit-demo` leaves the other projects untouched; deleting `rg-rag-demo` would also take this app down. The scripts are adapted from `irs-990-extraction/deploy/`. Run them from this folder in PowerShell, signed in with `az login` and `gh auth login`.

| Resource | Name |
|---|---|
| Resource group (Central US) | `rg-nonprofit-demo` |
| Key Vault (secrets; its own, so this app can't read the other projects' secrets) | `kv-npa-<first 6 characters of the subscription ID>` |
| Managed identity (the app's, reads the vault) | `id-nonprofit-api` |
| Container Apps environment (shared, in `rg-rag-demo`) / app | `cae-rag-demo` / `ca-nonprofit-api` |
| Image | `ghcr.io/evanderschel/nonprofit-agent-api` |
| GitHub environment (approval gate for deploys) | `nonprofit-agent-production` |

## Why spending needs more care than the irs-990 API

Every question runs the agent: several Claude calls, about $0.005-0.15 each question (capped at $0.25). The limits, from the outside in:

1. **A dedicated Anthropic workspace with its own monthly spend limit**, whose key is the only one the deployment has. This is the hard backstop: it holds whatever happens to the app.
2. **The daily spend cap** (`NONPROFIT_DAILY_SPEND_CAP_USD`, $2.00): no new question once today's spending plus one question's worst case would pass it. Counted in memory, so a restart resets it; that's why the workspace limit matters.
3. **5 questions a minute** for the key, **one question at a time**, questions of 3-500 characters, and the agent's own 10-call / $0.25 limits.
4. **At most one replica**: the cap, the rate limit and one-at-a-time are counted in the process's memory, so a second replica would double what they allow. Keep `--max-replicas 1`.
5. **Alerts** for unusual traffic (over 30 requests an hour) and for server errors, including 502 when the Claude API fails because a limit or the credit is used up.

## First deployment, in order

1. **Spending limits first.** In the Anthropic Console, create a workspace (for example `nonprofit-agent-public`) with a monthly spend limit (for example $20), and an API key in it. Make sure an Azure budget covers this resource group (the subscription-scoped one does).
2. `.\azure-setup.ps1`: resource group, Key Vault, and the app's managed identity (allowed only to read secrets in this vault); checks that the shared environment exists.
3. `.\azure-secrets.ps1` (run it yourself): the workspace's Anthropic key, typed at a hidden prompt (or `-FromClipboard`), and a random API key for callers. Neither ever appears on screen, in a file, or in chat.
4. `.\azure-github-oidc.ps1`: lets GitHub Actions deploy without a stored password (OIDC), with least privilege (on the shared environment, the existing custom role that can only run apps in it), and creates the `nonprofit-agent-production` environment with you as required approver.
5. The image: merges to main publish `ghcr.io/evanderschel/nonprofit-agent-api` (built and smoke-tested first). In GitHub (Packages > nonprofit-agent-api > Package settings), make the package **public**, so Azure can pull it without registry credentials. It contains only code.
6. `.\azure-create-app.ps1 -Tag <commit SHA>`: creates the app from that image, with the secrets as Key Vault references (the values never appear in the app's configuration). Scale 0-1, 0.5 vCPU, 1 GiB.
7. `.\azure-health-probes.ps1` and `.\azure-alerts.ps1`: HTTP health probes on `/health`, and email alerts.
8. From then on, every merge to main publishes a new image, and the deploy job waits for your approval, then confirms `/health` reports the deployed commit.

## Using it

```powershell
$key = az keyvault secret show --vault-name <vault> -n nonprofit-api-key --query value -o tsv
Invoke-RestMethod -Method Post https://<app URL>/ask -Headers @{"X-API-Key"=$key} -ContentType "application/json" -Body '{"question": "What was Iowa Public Radio''s revenue in 2023?"}'
```

The first question after an idle period takes about 30 seconds longer while a container starts. Logs (Log Analytics, `ContainerAppConsoleLogs_CL`) have one JSON line per question: stop reason, calls, cost and the day's running total, never the question text.

## If the key leaks

`.\azure-secrets.ps1 -RotateApiKey`, then restart the app (`az containerapp revision restart`) so it reads the new value. Meanwhile the rate limit, the daily cap and the workspace limit cap the damage.

## Removing it

`az group delete -n rg-nonprofit-demo` removes everything in Azure. The Key Vault name stays reserved for 90 days (soft delete); `az keyvault purge -n <vault>` frees it. Delete the Anthropic workspace's key in the Console.
