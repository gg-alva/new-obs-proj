# Flask OpenTelemetry + Prometheus + Grafana

## Kubernetes, GitOps, Azure Key Vault, and Dynatrace

The production-style deployment assets are included in this repository:

- `helm/otel-flask-app`: Helm chart for the application and in-cluster OTLP Collector.
- `gitops/`: Argo CD project/application, development desired-state values, and Azure Key Vault External Secrets store.
- `.github/workflows/`: pull-request validation and immutable-image publishing/GitOps promotion.
- `docs/AKS-ARGOCD-DYNATRACE.md`: complete manual AKS OIDC workload identity, Key Vault, External Secrets, Argo CD, and Dynatrace setup.

Start with the [full deployment guide](docs/AKS-ARGOCD-DYNATRACE.md). No Azure credentials or Dynatrace tokens are committed to this repository.

## Local start

1. Copy `.env.example` to `.env` and change `GRAFANA_ADMIN_PASSWORD`.
2. Run `docker compose up --build -d`.
3. Open:
   - Flask: http://localhost:5000
   - Prometheus: http://localhost:9090
   - Grafana: http://localhost:3000 (default username `admin`; password is `GRAFANA_ADMIN_PASSWORD` in `.env`)
4. Click the normal, slow, and error endpoints to generate telemetry.
5. In Prometheus, open Status > Targets and confirm `otel-collector` is UP.

Grafana automatically provisions a Prometheus datasource at `http://prometheus:9090`.

Useful PromQL:
- `app_requests_total`
- `app_errors_total`
- `app_request_latency_seconds_bucket`
- `sum(rate(app_requests_total[5m]))`
- `histogram_quantile(0.95, sum by (le) (rate(app_request_latency_seconds_bucket[5m])))`

Metric names are subject to Prometheus exporter translation. If a query returns no series, use the Prometheus UI to search available metric names.

## GitHub Actions -> Docker Hub

Create repository Actions secrets:
- `DOCKERHUB_USERNAME`: Docker Hub username
- `DOCKERHUB_TOKEN`: Docker Hub access token with permission to push

Push to `main` to build and publish:
- `<dockerhub-username>/otel-flask-app:latest`
- `<dockerhub-username>/otel-flask-app:<commit-sha>`

Pull request builds validate the Docker image but do not push it.

## Telemetry notes

- Flask sends OTLP traces and metrics to the Collector over the Compose network.
- The Collector exports metrics for Prometheus on port 8889 and writes traces to its debug logs.
- This stack visualizes metrics in Prometheus/Grafana. It does not store or display traces in a trace UI.
- Application logs are available with `docker compose logs -f app`; they are not currently exported as OpenTelemetry logs.
