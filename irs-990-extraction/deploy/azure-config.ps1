# Names shared by the deploy scripts. Nothing secret lives here.

$Location = "centralus"
$ResourceGroup = "rg-irs990-demo"
# Key Vault names are global across all of Azure, so add a suffix from the subscription ID.
$KeyVault = "kv-irs990-" + (az account show --query id -o tsv).Substring(0, 6)
$Identity = "id-irs990-api"
# The subscription allows only one Container Apps environment, and the rag project's is it, so the app
# runs in that one (in rg-rag-demo). Everything else is this project's own, in rg-irs990-demo. Note:
# deleting rg-rag-demo would remove the environment, and with it this app.
$Environment = "cae-rag-demo"
$EnvironmentGroup = "rg-rag-demo"
$App = "ca-irs990-api"
$Image = "ghcr.io/evanderschel/irs990-api"
