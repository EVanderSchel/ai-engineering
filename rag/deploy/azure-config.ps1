# Names shared by the deploy scripts. Nothing secret lives here.

$Location = "centralus"
$ResourceGroup = "rg-rag-demo"
# Key Vault names are global across all of Azure, so add a suffix from the subscription ID.
$KeyVault = "kv-rag-" + (az account show --query id -o tsv).Substring(0, 6)
$Identity = "id-rag-api"
$Environment = "cae-rag-demo"
$App = "ca-rag-api"
$Image = "ghcr.io/evanderschel/rag-api"
