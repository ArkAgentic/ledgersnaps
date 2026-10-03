# Azure Deploy via GitHub Actions (Fully Automatic)

This repo now includes `.github/workflows/deploy-azure.yml`.

## What it does

On every push to `main` (and manual workflow dispatch), it will:

1. Login to Azure using OIDC service principal
2. Build Docker image from `Dockerfile`
3. Push image to ACR
4. Update Azure Container App to the new image
5. Verify health endpoint: `GET /health`

## Required GitHub Secrets

Configure these in: GitHub Repo -> Settings -> Secrets and variables -> Actions

### Azure auth (OIDC)
- `AZURE_CLIENT_ID`
- `AZURE_TENANT_ID`
- `AZURE_SUBSCRIPTION_ID`

### Deploy target
- `AZURE_RESOURCE_GROUP`
- `AZURE_CONTAINER_APP_NAME`
- `AZURE_CONTAINER_REGISTRY` (e.g. `myregistry.azurecr.io`)
- `AZURE_CONTAINER_REGISTRY_NAME` (e.g. `myregistry`)

## One-time Azure setup notes

1. Create/choose an Entra app (service principal) for GitHub OIDC.
2. Add federated credential for this repo/branch (main).
3. Grant roles:
   - On resource group: `Contributor` (or narrower custom role)
   - On ACR: `AcrPush`

## Runtime app settings (Container App)

Make sure your Container App already has required env vars/secrets set (outside workflow):

- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_DEPLOYMENT`
- `ABR_GUID`
- `XERO_CLIENT_ID`
- `XERO_CLIENT_SECRET`
- `XERO_REDIRECT_URI`
- `XERO_SCOPE` (optional, defaults in code)

The workflow updates image only; it does not overwrite runtime secrets.

## Deployment flow

1. Local test -> commit -> push to `main`
2. GitHub Actions auto-runs deployment
3. Check Actions logs for `Verify latest revision healthy`

## Rollback

Use Azure Container Apps revision rollback:

- Azure Portal -> Container App -> Revisions -> Activate previous revision

or CLI:

```bash
az containerapp revision list -n <app> -g <rg> -o table
az containerapp revision activate -n <app> -g <rg> --revision <revision-name>
```
