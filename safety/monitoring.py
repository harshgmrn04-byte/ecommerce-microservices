from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Protocol

from .models import MetricValue, MonitoringSnapshot, ProbeValue, Validity


class MonitoringError(RuntimeError):
    pass


class MonitoringAdapter(Protocol):
    def snapshot(self) -> MonitoringSnapshot: ...


def parse_timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class PrometheusMonitoringAdapter:
    def __init__(self, policy: dict[str, Any]):
        self.policy = policy
        self.config = policy["monitoring"]
        self.abort = policy["abort"]
        self.admission = policy["admission"]
        self.timeout = float(policy["timing"]["monitoringRequestDeadlineSeconds"])

    def _json(self, url: str, timeout: float | None = None) -> Any:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise MonitoringError(str(error)) from error

    def _query(self, query: str) -> list[dict[str, Any]]:
        encoded = urllib.parse.urlencode({"query": query})
        response = self._json(f"{self.config['prometheusUrl']}/api/v1/query?{encoded}")
        if response.get("status") != "success":
            raise MonitoringError(f"Prometheus query failed: {response}")
        result = response.get("data", {}).get("result")
        if not isinstance(result, list):
            raise MonitoringError("Prometheus returned malformed result data")
        return result

    def _source_observations(self) -> dict[str, tuple[float | None, str]]:
        response = self._json(f"{self.config['prometheusUrl']}/api/v1/targets")
        targets = response.get("data", {}).get("activeTargets", [])
        observations: dict[str, tuple[float | None, str]] = {}
        for target in targets:
            job = target.get("labels", {}).get("job")
            if job:
                try:
                    observed = parse_timestamp(target["lastScrape"])
                except (KeyError, TypeError, ValueError):
                    observed = None
                observations[job] = (observed, target.get("health", "unknown"))
        return observations

    @staticmethod
    def _result_map(results: list[dict[str, Any]]) -> dict[str, tuple[float, float]]:
        mapped: dict[str, tuple[float, float]] = {}
        for item in results:
            try:
                service = item["metric"].get("service") or item["metric"].get("job")
                timestamp = float(item["value"][0])
                value = float(item["value"][1])
                if service and math.isfinite(value):
                    mapped[service] = (timestamp, value)
            except (KeyError, TypeError, ValueError):
                continue
        return mapped

    def _probes(self) -> dict[str, ProbeValue]:
        results: dict[str, ProbeValue] = {}
        timeout = float(self.policy["timing"]["probeTimeoutSeconds"])
        for probe in self.config["probes"]:
            started = time.monotonic()
            observed = time.time()
            try:
                request = urllib.request.Request(
                    probe["url"], method="GET", headers={"User-Agent": "safety-probe/1"}
                )
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    response.read(65536)
                    status = response.status
                results[probe["id"]] = ProbeValue(
                    probe_id=probe["id"],
                    status_code=status,
                    latency_ms=(time.monotonic() - started) * 1000,
                    valid=status == int(probe["expectedStatus"]),
                    observed_at=observed,
                )
            except Exception as error:
                status = error.code if isinstance(error, urllib.error.HTTPError) else None
                results[probe["id"]] = ProbeValue(
                    probe_id=probe["id"],
                    status_code=status,
                    latency_ms=(time.monotonic() - started) * 1000,
                    valid=False,
                    observed_at=observed,
                    error=type(error).__name__,
                )
        return results

    def snapshot(self) -> MonitoringSnapshot:
        observed_at = time.time()
        probes = self._probes()
        required = self.config["requiredJobs"]
        protected = self.config["protectedHttpServices"]
        window = int(self.policy["timing"]["rollingWindowSeconds"])
        minimum = int(self.admission["minimumRequestCountPerWindow"])
        try:
            sources = self._source_observations()
            up_map = self._result_map(
                self._query('up{job=~"' + "|".join(required) + '"}')
            )
            counts_map = self._result_map(
                self._query(
                    f'sum by (service) (increase(http_requests_total{{route!~"/metrics|/health/.*"}}[{window}s]))'
                )
            )
            errors_map = self._result_map(
                self._query(
                    f'sum by (service) (increase(http_requests_total{{status_code=~"5..",route!~"/metrics|/health/.*"}}[{window}s]))'
                    f' / clamp_min(sum by (service) (increase(http_requests_total{{route!~"/metrics|/health/.*"}}[{window}s])), 1)'
                )
            )
            p95_map = self._result_map(
                self._query(
                    "histogram_quantile(0.95, sum by (service, le) "
                    f'(rate(http_request_duration_seconds_bucket{{route!~"/metrics|/health/.*|/login"}}[{window}s]))) * 1000'
                )
            )
        except MonitoringError as error:
            return MonitoringSnapshot(
                observed_at=observed_at,
                target_up={},
                error_fraction={},
                p95_latency_ms={},
                probes=probes,
                telemetry_error=str(error),
            )

        target_up: dict[str, MetricValue] = {}
        for job in required:
            query = up_map.get(job)
            source_time, source_health = sources.get(job, (None, "unknown"))
            valid = query is not None and source_health in {"up", "down"}
            value = query[1] if query else None
            target_up[job] = MetricValue(
                service=job,
                value=value,
                unit="boolean",
                source_timestamp=source_time,
                request_count=None,
                validity=(
                    Validity.HEALTHY
                    if valid and value == 1
                    else Validity.UNHEALTHY
                    if valid
                    else Validity.UNKNOWN
                ),
                last_successful_observation=source_time if source_health == "up" else None,
            )

        error_values: dict[str, MetricValue] = {}
        latency_values: dict[str, MetricValue] = {}
        for service in protected:
            count_value = counts_map.get(service)
            count = int(count_value[1]) if count_value else None
            source_time, source_health = sources.get(service, (None, "unknown"))
            adequate = count is not None and count >= minimum and source_health == "up"
            error = errors_map.get(service)
            latency = p95_map.get(service)
            error_values[service] = MetricValue(
                service=service,
                value=error[1] if error else (0.0 if adequate else None),
                unit="fraction",
                source_timestamp=source_time,
                request_count=count,
                validity=(
                    Validity.HEALTHY if adequate and (error is not None or count > 0)
                    else Validity.INSUFFICIENT_TRAFFIC if source_health == "up"
                    else Validity.UNKNOWN
                ),
                last_successful_observation=source_time if source_health == "up" else None,
            )
            latency_values[service] = MetricValue(
                service=service,
                value=latency[1] if latency else None,
                unit="milliseconds",
                source_timestamp=source_time,
                request_count=count,
                validity=(
                    Validity.HEALTHY if adequate and latency is not None
                    else Validity.INSUFFICIENT_TRAFFIC if source_health == "up"
                    else Validity.UNKNOWN
                ),
                last_successful_observation=source_time if source_health == "up" else None,
            )

        return MonitoringSnapshot(
            observed_at=time.time(),
            target_up=target_up,
            error_fraction=error_values,
            p95_latency_ms=latency_values,
            probes=probes,
        )

