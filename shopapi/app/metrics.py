"""Prometheus metrics exposed on /metrics."""
import os

from prometheus_client import Counter, Gauge, Histogram, Info

REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
DB_QUERY = Histogram(
    "db_query_duration_seconds",
    "Database query latency",
    ["table", "op"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
DEPENDENCY_ERRORS = Counter("dependency_errors_total", "Failed calls to downstream dependencies", ["dependency"])
ORDER_CACHE_BYTES = Gauge("order_cache_bytes", "Bytes held by the in-process order cache")

BUILD = Info("shopapi_build", "Build information")
BUILD.info({"version": os.getenv("APP_VERSION", "dev")})
