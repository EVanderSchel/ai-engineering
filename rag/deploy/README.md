# Deploying to Azure Container Apps

The API runs as a single container on Azure Container Apps (Consumption plan, scale 0-1), pulling the public image `ghcr.io/evanderschel/rag-api:<commit-sha>` that CI publishes after every merge to `main`. The search index is built into the image, so there's no database to host.

```
GitHub Actions (merge to main) --build+push--> GHCR (public image, no secrets)
                                                   |
Azure: rg-rag-demo                                 v pull
  Container Apps environment cae-rag-demo --> Container App ca-rag-api (0-1 replicas)
  Key Vault kv-rag-<suffix>  <--reads secrets-- managed identity id-rag-api
  Log Analytics workspace    <--console logs (one JSON line per request)
```

Secrets never live in the image or the app's configuration. The app holds **Key Vault references**; its managed identity (allowed only the *Key Vault Secrets User* role on this one vault) reads the values when a container starts.

| Secret | Environment variable | Source |
|---|---|---|
| `anthropic-api-key` | `ANTHROPIC_API_KEY` | A key created for this deployment, with a spending limit |
| `rag-api-key` | `RAG_API_KEY` | Generated randomly; callers send it in `X-API-Key` |
| `langfuse-public-key`, `langfuse-secret-key` | `LANGFUSE_*` | Copied from `rag/.env`; traces are tagged `production` |

## First-time setup

The scripts are Windows PowerShell. If your execution policy blocks them, run each as `powershell -ExecutionPolicy Bypass -File <script>`.

1. `az login`, and select the subscription.
2. `.\azure-setup.ps1`: resource group, Key Vault, managed identity, Container Apps environment. Safe to re-run.
3. `.\azure-secrets.ps1` (run it yourself; it asks for the Anthropic key with hidden input, or use `-FromClipboard`).
4. `.\azure-create-app.ps1`: creates the Container App from the image for the current `origin/main`.

Names live in `azure-config.ps1`.

## Using it

```
$key = az keyvault secret show --vault-name kv-rag-aca579 -n rag-api-key --query value -o tsv
Invoke-RestMethod -Method Post https://<fqdn>/ask -ContentType application/json `
    -Headers @{ "X-API-Key" = $key } -Body '{"question": "Who won Sweden''s September election?"}'
```

`/health` is open; `/ask` and `/ask/stream` need the key. Logs: `az containerapp logs show -n ca-rag-api -g rg-rag-demo --tail 50`.

## Cold starts

With `min-replicas 0`, Azure stops the container after about 5 idle minutes, and the next request waits for a new one to start (pull the image if the host doesn't have it cached, start Python, warm up). Measured on 2026-09-28: the first request after scaling to zero took **30 s** (about 13 s of that is the container's own startup; the rest is Azure scheduling and starting it); the next `/ask` took 3.6 s, mostly Claude's generation. That's the trade for costing nearly nothing when idle. Setting `--min-replicas 1` removes the wait but bills for an always-on replica.

## Tearing down

`az group delete -n rg-rag-demo` removes everything. The Key Vault is soft-deleted and its name stays reserved for 90 days; `az keyvault purge -n kv-rag-aca579` frees it immediately.
