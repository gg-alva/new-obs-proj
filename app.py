import logging
import os
import random
import time

from flask import Flask, render_template_string
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("myapp")

# Set this to the OTLP HTTP base URL, without /v1/traces or /v1/metrics.
endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318").rstrip("/")

SERVICE_NAME = "myapp"
SERVICE_VERSION = os.environ.get("APP_VERSION", os.environ.get("GITHUB_SHA", "1.0"))
ENVIRONMENT = os.environ.get("APP_ENV", "development")

resource = Resource.create({
    "service.name": SERVICE_NAME,
    "service.version": SERVICE_VERSION,
    "deployment.environment.name": ENVIRONMENT,
    "vcs.provider.name": "github",
    "vcs.repository.name": os.environ.get("GITHUB_REPOSITORY", ""),
})


trace_provider = TracerProvider(resource=resource)
trace_provider.add_span_processor(
    BatchSpanProcessor(
        OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")
    )
)
trace.set_tracer_provider(trace_provider)
tracer = trace.get_tracer("myapp")

metric_reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"),
    export_interval_millis=5000,
)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
from opentelemetry.metrics import set_meter_provider
set_meter_provider(meter_provider)

meter = meter_provider.get_meter("myapp")
request_counter = meter.create_counter(
    "app_requests", description="Number of application requests"
)
error_counter = meter.create_counter(
    "app_errors", description="Number of application errors"
)
latency_histogram = meter.create_histogram(
    "app_request_latency_seconds",
    unit="s",
    description="Application request latency in seconds",
)

app = Flask(__name__)

HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
  <title>OpenTelemetry Flask Demo</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { font-family: Arial, sans-serif; background: #f4f7fb; margin: 0; padding: 0; }
    .container { max-width: 900px; margin: 50px auto; padding: 20px; }
    .card { background: white; padding: 32px; border-radius: 18px; box-shadow: 0 10px 30px rgba(0,0,0,.10); text-align: center; }
    h1 { color: #111827; margin-bottom: 10px; }
    .subtitle { color: #6b7280; margin-bottom: 25px; }
    .status { display: inline-block; padding: 8px 16px; background: #dcfce7; color: #166534; border-radius: 20px; font-weight: bold; }
    .info-box { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin: 28px 0; }
    .info { background: #f9fafb; padding: 18px; border-radius: 12px; border: 1px solid #e5e7eb; overflow-wrap: anywhere; }
    .info strong { display: block; color: #374151; margin-bottom: 8px; }
    .info span { color: #2563eb; font-weight: bold; }
    a.btn { display: inline-block; margin: 8px; padding: 14px 20px; text-decoration: none; border-radius: 10px; color: white; font-weight: bold; }
    .normal { background: #2563eb; } .slow { background: #7c3aed; } .error { background: #dc2626; }
    .metrics { margin-top: 25px; padding: 20px; background: #f8fafc; border-radius: 14px; text-align: left; border: 1px solid #e5e7eb; }
    code { background: #e5e7eb; padding: 3px 6px; border-radius: 5px; overflow-wrap: anywhere; }
  </style>
</head>
<body>
  <div class="container"><div class="card">
    <h1>🚀 OpenTelemetry Flask Demo</h1>
    <p class="subtitle">Metrics and traces with Prometheus and Grafana</p>
    <div class="status">Application Running</div>
    <div class="info-box">
      <div class="info"><strong>Service</strong><span>{{ service_name }}</span></div>
      <div class="info"><strong>Environment</strong><span>{{ environment }}</span></div>
      <div class="info"><strong>Version</strong><span>{{ service_version }}</span></div>
    </div>
    <a class="btn normal" href="/">Generate Normal Request</a>
    <a class="btn slow" href="/slow">Generate Slow Request</a>
    <a class="btn error" href="/error">Generate Error</a>
    <div class="metrics">
      <h3>Observability signals</h3>
      <p>✅ Application request counter</p>
      <p>✅ Application error counter</p>
      <p>✅ Request latency histogram</p>
      <p>✅ OpenTelemetry spans exported over OTLP HTTP</p>
    </div>
    <p>OTLP endpoint: <code>{{ endpoint }}</code></p>
  </div></div>
</body>
</html>
"""

def render_home():
    return render_template_string(
        HTML_PAGE,
        service_name=SERVICE_NAME,
        service_version=SERVICE_VERSION,
        environment=ENVIRONMENT,
        endpoint=endpoint,
    )

def record_request(endpoint_name, started_at):
    latency_histogram.record(time.time() - started_at, {"endpoint": endpoint_name})


@app.route("/healthz")
def healthz():
    """Liveness check: the process is able to serve HTTP requests."""
    return {"status": "ok"}, 200


@app.route("/readyz")
def readyz():
    """Readiness check: this stateless application is ready immediately."""
    return {"status": "ready"}, 200

@app.route("/")
def home():
    started_at = time.time()
    endpoint_name = "/"
    logger.info("Home endpoint called")
    with tracer.start_as_current_span("home-span") as span:
        try:
            time.sleep(random.uniform(0.1, 0.5))
            span.set_attribute("http.route", endpoint_name)
            span.set_attribute("request.type", "normal")
            request_counter.add(1, {"endpoint": endpoint_name, "method": "GET"})
            return render_home()
        except Exception as exc:
            logger.exception("Error occurred on home endpoint")
            error_counter.add(1, {"endpoint": endpoint_name})
            span.record_exception(exc)
            span.set_attribute("error", True)
            return "Internal Server Error", 500
        finally:
            record_request(endpoint_name, started_at)

@app.route("/slow")
def slow():
    started_at = time.time()
    endpoint_name = "/slow"
    logger.info("Slow endpoint called")
    with tracer.start_as_current_span("slow-span") as span:
        try:
            sleep_time = random.uniform(2, 5)
            time.sleep(sleep_time)
            span.set_attribute("http.route", endpoint_name)
            span.set_attribute("request.type", "slow")
            span.set_attribute("sleep_time_seconds", sleep_time)
            request_counter.add(1, {"endpoint": endpoint_name, "method": "GET"})
            return render_home()
        except Exception as exc:
            logger.exception("Error occurred on slow endpoint")
            error_counter.add(1, {"endpoint": endpoint_name})
            span.record_exception(exc)
            span.set_attribute("error", True)
            return "Internal Server Error", 500
        finally:
            record_request(endpoint_name, started_at)

@app.route("/error")
def error():
    started_at = time.time()
    endpoint_name = "/error"
    logger.warning("Error endpoint triggered")
    with tracer.start_as_current_span("error-span") as span:
        try:
            span.set_attribute("http.route", endpoint_name)
            span.set_attribute("request.type", "error")
            request_counter.add(1, {"endpoint": endpoint_name, "method": "GET"})
            error_counter.add(1, {"endpoint": endpoint_name})
            raise RuntimeError("Manual error triggered for observability testing")
        except Exception as exc:
            logger.error("Manual error occurred: %s", exc)
            span.record_exception(exc)
            span.set_attribute("error", True)
            return """
            <h1 style="font-family:Arial;color:red;text-align:center;margin-top:80px">
              ❌ Manual Error Triggered
            </h1>
            <p style="font-family:Arial;text-align:center">
              This error was intentionally generated for observability testing.
            </p>
            <p style="text-align:center"><a href="/">Go Back Home</a></p>
            """, 500
        finally:
            record_request(endpoint_name, started_at)

if __name__ == "__main__":
    logger.info("Starting app with observability enabled")
    app.run(host="0.0.0.0", port=5000)
