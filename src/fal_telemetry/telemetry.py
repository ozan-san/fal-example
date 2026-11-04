"""OpenTelemetry providers for traces, metrics and logs, all exported over OTLP."""

import logging

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from fal_telemetry.config import Settings

# The SDK's default buckets assume milliseconds, which would put every value here
# (in seconds) in the lowest few buckets.
DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 7.5, 10, 15, 30, 60, 120)


def setup_telemetry(settings: Settings) -> list:
    """Install global providers and return them so they can be flushed on shutdown."""
    resource = Resource.create(
        {
            "service.name": "fal-telemetry",
            "service.version": "0.1.0",
            "deployment.environment.name": settings.fal_mode,
        }
    )
    endpoint = settings.otel_exporter_otlp_endpoint

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
    )
    trace.set_tracer_provider(tracer_provider)

    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=endpoint, insecure=True),
                export_interval_millis=settings.otel_export_interval_millis,
            )
        ],
        views=[
            View(
                instrument_name="fal.client.*.duration",
                aggregation=ExplicitBucketHistogramAggregation(DURATION_BUCKETS),
            )
        ],
    )
    metrics.set_meter_provider(meter_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint, insecure=True))
    )
    set_logger_provider(logger_provider)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger().addHandler(LoggingHandler(logger_provider=logger_provider))
    # Per-poll INFO lines from httpx would drown out the useful logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    return [tracer_provider, meter_provider, logger_provider]
