# AKS, Helm, Argo CD, Azure Key Vault, and Dynatrace

This guide deploys the Flask application to AKS with Helm and makes Git the desired-state authority through Argo CD. Azure setup is deliberately manual: the commands and portal-equivalent settings below create no Terraform-managed resources.

## Architecture

```text
Git push → GitHub Actions builds immutable image → commits image SHA to GitOps values
                                                       ↓
Argo CD watches main → Helm release in AKS → Flask → OTLP/HTTP → Collector → Dynatrace
                                                   ↑                         
Key Vault → External Secrets Operator (Azure workload identity) → Kubernetes Secret
```

The Dynatrace token never enters Git, Helm values, or the GitHub workflow. External Secrets Operator (ESO) reads it from Key Vault using a user-assigned managed identity federated with an AKS service account. The Collector consumes the resulting Kubernetes Secret.

## 1. Prerequisites

Install and authenticate the following locally: Azure CLI, `kubectl`, Helm 3, the Argo CD CLI (optional but useful), and Git. You need permission to create or administer the AKS cluster, user-assigned identities, federated identity credentials, Key Vault role assignments, and the GitHub repository.

Set shell values once; replace every all-caps value.

```bash
export SUBSCRIPTION_ID="YOUR_SUBSCRIPTION_ID"
export RESOURCE_GROUP="YOUR_RESOURCE_GROUP"
export AKS_NAME="YOUR_AKS_NAME"
export LOCATION="YOUR_AZURE_REGION"
export KEYVAULT_NAME="YOUR_KEY_VAULT_NAME"
export IDENTITY_NAME="external-secrets-identity"
export APP_NAMESPACE="otel-demo-dev"
export ESO_NAMESPACE="external-secrets"
az account set --subscription "$SUBSCRIPTION_ID"
az aks get-credentials --resource-group "$RESOURCE_GROUP" --name "$AKS_NAME"
```

The cluster needs egress to Docker Hub, GitHub (or your Git server), Azure AD and Key Vault, and the Dynatrace OTLP endpoint. If your registry is private, create an `imagePullSecret` separately and set `imagePullSecrets` in the environment values.

## 2. Enable AKS OIDC issuer and workload identity

Run the following even on an existing cluster; it enables the required AKS features without recreating workloads.

```bash
az aks update \
  --resource-group "$RESOURCE_GROUP" \
  --name "$AKS_NAME" \
  --enable-oidc-issuer \
  --enable-workload-identity

export AKS_OIDC_ISSUER="$(az aks show --resource-group "$RESOURCE_GROUP" --name "$AKS_NAME" --query oidcIssuerProfile.issuerUrl -o tsv)"
test -n "$AKS_OIDC_ISSUER" && echo "$AKS_OIDC_ISSUER"
```

## 3. Create the Azure identity and Key Vault access

Create the identity used only by ESO, discover its client and principal IDs, and grant it read-only data-plane access to secrets. The Key Vault must use Azure RBAC authorization. For vaults still using access policies, grant the identity **Secret Get** and **List** in the Key Vault access policy instead of the role assignment command.

```bash
az identity create --name "$IDENTITY_NAME" --resource-group "$RESOURCE_GROUP" --location "$LOCATION"

export AZURE_CLIENT_ID="$(az identity show --name "$IDENTITY_NAME" --resource-group "$RESOURCE_GROUP" --query clientId -o tsv)"
export IDENTITY_PRINCIPAL_ID="$(az identity show --name "$IDENTITY_NAME" --resource-group "$RESOURCE_GROUP" --query principalId -o tsv)"
export KEYVAULT_ID="$(az keyvault show --name "$KEYVAULT_NAME" --query id -o tsv)"

az role assignment create \
  --assignee-object-id "$IDENTITY_PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Key Vault Secrets User" \
  --scope "$KEYVAULT_ID"
```

Role-assignment propagation can take several minutes. Do not grant Key Vault Contributor or Owner just to make ESO work.

## 4. Create a least-privilege Dynatrace API token and store it

In Dynatrace, create an API token with these scopes:

- `openTelemetryTrace.ingest`
- `metrics.ingest`

Add `logs.ingest` only if you later add an OTLP logs pipeline. Copy the value once, then place it in Key Vault. Secret names cannot contain underscores, so this guide uses `dynatrace-otel-token`.

```bash
az keyvault secret set \
  --vault-name "$KEYVAULT_NAME" \
  --name dynatrace-otel-token \
  --value 'PASTE_DYNATRACE_TOKEN_HERE'
```

For Dynatrace SaaS the OTLP endpoint is normally `https://ENVIRONMENT_ID.live.dynatrace.com/api/v2/otlp`. For Managed, use the environment-specific OTLP endpoint shown by Dynatrace. It must be the base endpoint ending in `/api/v2/otlp`; the Collector appends `/v1/traces` and `/v1/metrics` itself.

## 5. Install External Secrets Operator with workload identity

Create the namespace and federated credential. The issuer, audience, namespace, and service-account subject must match exactly. The federation means that only this Kubernetes service account can exchange its projected token for this Azure identity.

```bash
kubectl create namespace "$ESO_NAMESPACE"

az identity federated-credential create \
  --name external-secrets-aks \
  --identity-name "$IDENTITY_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --issuer "$AKS_OIDC_ISSUER" \
  --subject "system:serviceaccount:${ESO_NAMESPACE}:external-secrets" \
  --audience api://AzureADTokenExchange

helm repo add external-secrets https://charts.external-secrets.io
helm repo update
helm upgrade --install external-secrets external-secrets/external-secrets \
  --namespace "$ESO_NAMESPACE" \
  --set installCRDs=true \
  --set serviceAccount.create=true \
  --set serviceAccount.name=external-secrets \
  --set serviceAccount.annotations.azure\\.workload\\.identity/client-id="$AZURE_CLIENT_ID" \
  --set podLabels.azure\\.workload\\.identity/use=true
```

Apply the cluster-scoped SecretStore after replacing `REPLACE_WITH_KEY_VAULT_NAME` in `gitops/infra/external-secrets/cluster-secret-store.yaml`. It deliberately refers to ESO’s service account rather than the app service account, so applications cannot independently read Key Vault.

```bash
kubectl apply -f gitops/infra/external-secrets/cluster-secret-store.yaml
kubectl get clustersecretstore azure-keyvault
```

If your ESO chart version uses a different controller service-account name, change both the federated credential subject and `serviceAccountRef` to that exact name before installation.

## 6. Prepare the Git repository and container publishing

1. In GitHub, create two Actions secrets: `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (a Docker Hub access token with push permission).
2. Replace `REPLACE_WITH_OWNER/REPLACE_WITH_REPOSITORY` in both files in `gitops/argocd/` with this repository’s GitHub owner and repository name.
3. Replace `REPLACE_ME` in `gitops/environments/dev/myapp-values.yaml` with the Dynatrace environment ID, then commit those non-secret settings.
4. Confirm Actions has **Read and write permissions** under repository Actions settings, because the release workflow commits the new immutable image tag back into `main`.

`CI` validates Python syntax, renders the chart, and builds the image on pull requests and application changes. `Release and promote development` publishes both the commit SHA and `latest`, then writes only the SHA into the GitOps values file. The SHA is the deployment artifact; `latest` is merely a convenience tag.

## 7. Install Argo CD and register the application

Install Argo CD (or use your managed installation), then apply the project and development application. Do not expose the default admin password externally; follow your organization’s SSO and ingress standard.

```bash
kubectl create namespace argocd
kubectl apply -n argocd \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# Edit the repository placeholders first.
kubectl apply -f gitops/argocd/project.yaml
kubectl apply -f gitops/argocd/application-dev.yaml
```

Argo CD creates `otel-demo-dev`, renders `helm/otel-flask-app` with `gitops/environments/dev/myapp-values.yaml`, and continuously reconciles it. It has automated prune and self-heal enabled; edit Git rather than using `kubectl edit` for desired-state changes.

For a private Git repository, add repository credentials to Argo CD before applying the Application, for example through the Argo CD UI/CLI using a GitHub App or a short-lived credential. Keep the credential in Argo CD’s secret storage, never in the Application manifest.

## 8. Validate the rollout and telemetry

Wait for Argo to reach Healthy/Synced, then check the secret and workloads:

```bash
kubectl -n "$APP_NAMESPACE" get externalsecret
kubectl -n "$APP_NAMESPACE" get secret dynatrace-otel
kubectl -n "$APP_NAMESPACE" get deploy,pods,svc
kubectl -n "$APP_NAMESPACE" port-forward svc/otel-flask-otel-flask-app 8080:80
```

Open `http://localhost:8080/`, `http://localhost:8080/slow`, and `http://localhost:8080/error`. These generate normal, slow, and failed requests. Confirm the Collector can authenticate and export:

```bash
kubectl -n "$APP_NAMESPACE" logs deploy/otel-flask-otel-flask-app-collector --tail=100
```

In Dynatrace, use Distributed traces and Metrics to find `service.name = myapp`; filter by `deployment.environment.name = development`. The app emits `app_requests`, `app_errors`, and `app_request_latency_seconds` (Dynatrace may normalize metric names). If traces appear but metrics do not, re-check that `metrics.ingest` is on the API token.

## Operations and troubleshooting

- **ExternalSecret is not Ready:** inspect `kubectl -n otel-demo-dev describe externalsecret otel-flask-otel-flask-app-dynatrace`, then ESO controller logs. Most causes are the Key Vault role assignment still propagating, an incorrect vault URL, or federation subject mismatch.
- **Azure token exchange fails:** confirm the ESO pod has the `azure.workload.identity/use: "true"` label and the service account has `azure.workload.identity/client-id: <AZURE_CLIENT_ID>`. Compare the federation issuer and subject with the cluster values exactly.
- **Collector CrashLoopBackOff:** make sure the generated secret name/key (`dynatrace-otel` / `api-token`) matches the values file and the Dynatrace endpoint is populated.
- **Dynatrace 401/403:** use a fresh API token with the listed scopes, verify it is stored as the Key Vault secret value (not a secret identifier), and check the endpoint has no `/v1/traces` suffix.
- **ImagePullBackOff:** check Docker Hub image visibility and `image.repository`; configure a pull secret for private images.
- **Rollback:** revert the Git commit that changed `image.tag`. Argo CD reconciles the previous SHA; do not deploy ad hoc mutable tags.

## Production hardening checklist

- Create a separate namespace, values file, Dynatrace token, Key Vault secret, and Argo CD Application per environment.
- Promote an already-built image SHA from development to production through a reviewed pull request; do not rebuild it.
- Add network policies that allow app-to-Collector and Collector-to-Dynatrace traffic only.
- Set an ingress or gateway with TLS and authentication before exposing the Service outside the cluster.
- Use a private registry or private Docker Hub repository with a pull secret, image scanning, and an admission policy for signed images.
- Configure Dynatrace token rotation by creating a new Key Vault secret version; ESO refreshes every hour. Reduce `refreshInterval` if your rotation policy requires it.
- Add alerting for ESO secret-sync failures, Collector export failures, Argo OutOfSync, and Kubernetes resource saturation.
