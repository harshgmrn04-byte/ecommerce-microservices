#!/usr/bin/env python3
"""Run an allow-listed, automatically reversible local chaos experiment."""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXPERIMENT = ROOT / "chaos/experiments/notification-service-crash.json"
RESULT_PATH = ROOT / "chaos/results/notification-service-crash.json"
KILL_SWITCH = ROOT / "chaos/KILL_SWITCH"
LOCK_FILE = ROOT / "chaos/.experiment.lock"

PROMETHEUS_URL = "http://localhost:9090"
RABBITMQ_API = "http://localhost:15672/api/queues/%2F/notifications"
MAILHOG_API = "http://localhost:8025/api/v2/messages"
AUTH_URL = "http://localhost:3004"
PRODUCT_URL = "http://localhost:3002"
ORDER_URL = "http://localhost:3003"

APPLICATION_JOBS = (
    "auth-service",
    "product-service",
    "order-service",
    "notification-service",
)
ALLOWED_TARGETS = {
    "notification-service": "ecommerce-notifications",
}


class ExperimentError(RuntimeError):
    pass


class GuardrailBreach(ExperimentError):
    pass


class ManualAbort(ExperimentError):
    pass


def utc_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"[{datetime.now().astimezone().strftime('%H:%M:%S')}] {message}", flush=True)


def run(command: list[str], *, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip()
        raise ExperimentError(f"Command failed ({' '.join(command)}): {details}")
    return result


def compose(*arguments: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return run(["docker", "compose", *arguments], check=check, timeout=timeout)


def http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    basic_auth: tuple[str, str] | None = None,
    timeout: int = 5,
) -> tuple[int, Any]:
    request_headers = {"Accept": "application/json", **(headers or {})}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if basic_auth:
        credentials = base64.b64encode(f"{basic_auth[0]}:{basic_auth[1]}".encode()).decode()
        request_headers["Authorization"] = f"Basic {credentials}"

    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode("utf-8")
            return response.status, json.loads(response_body) if response_body else {}
    except urllib.error.HTTPError as error:
        response_body = error.read().decode("utf-8")
        try:
            parsed: Any = json.loads(response_body)
        except json.JSONDecodeError:
            parsed = response_body
        return error.code, parsed
    except (urllib.error.URLError, TimeoutError) as error:
        raise ExperimentError(f"Request failed for {url}: {error}") from error


def prometheus_query(query: str) -> list[dict[str, Any]]:
    encoded = urllib.parse.urlencode({"query": query})
    status, response = http_json(f"{PROMETHEUS_URL}/api/v1/query?{encoded}")
    if status != 200 or response.get("status") != "success":
        raise ExperimentError(f"Prometheus query failed: {response}")
    return response["data"]["result"]


def application_up() -> dict[str, int]:
    jobs = "|".join(APPLICATION_JOBS)
    results = prometheus_query(f'up{{job=~"{jobs}"}}')
    values = {item["metric"]["job"]: int(float(item["value"][1])) for item in results}
    return {job: values.get(job, 0) for job in APPLICATION_JOBS}


def maximum_metric(query: str, label: str = "service") -> tuple[float, str | None]:
    results = prometheus_query(query)
    if not results:
        return 0.0, None
    parsed = [
        (float(item["value"][1]), item["metric"].get(label))
        for item in results
        if math.isfinite(float(item["value"][1]))
    ]
    if not parsed:
        return 0.0, None
    return max(parsed, key=lambda item: item[0])


def firing_safety_alerts() -> list[str]:
    status, response = http_json(f"{PROMETHEUS_URL}/api/v1/alerts")
    if status != 200 or response.get("status") != "success":
        raise ExperimentError(f"Unable to read Prometheus alerts: {response}")
    return [
        alert["labels"].get("alertname", "unknown")
        for alert in response["data"]["alerts"]
        if alert.get("state") == "firing"
        and alert.get("labels", {}).get("safety_action") == "abort_experiment"
    ]


def rabbitmq_state() -> dict[str, int]:
    status, response = http_json(RABBITMQ_API, basic_auth=("ecommerce", "ecommerce123"))
    if status != 200:
        raise ExperimentError(f"RabbitMQ queue inspection failed ({status}): {response}")
    return {
        "messagesReady": int(response.get("messages_ready", 0)),
        "messagesUnacknowledged": int(response.get("messages_unacknowledged", 0)),
        "consumers": int(response.get("consumers", 0)),
    }


def mailhog_total() -> int:
    status, response = http_json(MAILHOG_API)
    if status != 200:
        raise ExperimentError(f"MailHog inspection failed ({status}): {response}")
    return int(response.get("total", 0))


def prepare_canary() -> dict[str, Any]:
    login_status, login = http_json(
        f"{AUTH_URL}/login",
        method="POST",
        payload={"email": "admin@test.com", "password": "123456"},
    )
    product_status, products = http_json(f"{PRODUCT_URL}/products?limit=1")
    if login_status != 200 or product_status != 200 or not products.get("products"):
        raise ExperimentError(
            f"Canary setup failed: login={login_status}, products={product_status}"
        )
    return {"token": login["accessToken"], "product": products["products"][0]}


def create_canary_order(canary: dict[str, Any]) -> dict[str, Any]:
    product = canary["product"]
    payload = {
        "items": [
            {
                "productId": product["id"],
                "name": product["name"],
                "price": product["price"],
                "quantity": 1,
                "image": (product.get("images") or [""])[0],
            }
        ],
        "shippingAddress": {
            "street": "1 Chaos Lane",
            "city": "Test City",
            "state": "KA",
            "zip": "560001",
            "country": "IN",
        },
        "paymentMethod": "local",
    }
    status, response = http_json(
        f"{ORDER_URL}/orders",
        method="POST",
        payload=payload,
        headers={"Authorization": f"Bearer {canary['token']}"},
    )
    if status != 201:
        raise GuardrailBreach(f"Checkout canary failed with HTTP {status}: {response}")
    return {"httpStatus": status, "orderId": response["order"]["id"]}


def probe_non_target_services(canary: dict[str, Any]) -> dict[str, int]:
    authorization = {"Authorization": f"Bearer {canary['token']}"}
    probes = {
        "auth-service": http_json(f"{AUTH_URL}/me", headers=authorization)[0],
        "product-service": http_json(f"{PRODUCT_URL}/products?limit=1")[0],
        "order-service": http_json(f"{ORDER_URL}/orders?limit=1", headers=authorization)[0],
    }
    failures = {service: status for service, status in probes.items() if status != 200}
    if failures:
        raise GuardrailBreach(f"Synthetic availability probes failed: {failures}")
    return probes


def inspect_target(target: str) -> None:
    container = ALLOWED_TARGETS[target]
    template = (
        '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}|'
        '{{ index .Config.Labels "com.docker.compose.service" }}|{{.State.Running}}'
    )
    result = run(["docker", "inspect", "--format", template, container])
    working_dir, service, running = result.stdout.strip().split("|", 2)
    if Path(working_dir).resolve() != ROOT or service != target or running != "true":
        raise ExperimentError(
            f"Refusing to inject: target identity mismatch ({working_dir}, {service}, {running})"
        )


def validate_manifest(experiment: dict[str, Any]) -> None:
    target = experiment.get("target")
    guardrails = experiment.get("guardrails", {})
    if target not in ALLOWED_TARGETS:
        raise ExperimentError(f"Target is not allow-listed: {target}")
    if experiment.get("fault") != "container-stop":
        raise ExperimentError("Only the reversible container-stop fault is supported")
    if not 1 <= int(experiment.get("durationSeconds", 0)) <= 20:
        raise ExperimentError("Experiment duration must be between 1 and 20 seconds")
    if not 1 <= int(experiment.get("recoveryDeadlineSeconds", 0)) <= 30:
        raise ExperimentError("Recovery deadline must be between 1 and 30 seconds")
    if int(guardrails.get("maximumConcurrentFaultTargets", 0)) != 1:
        raise ExperimentError("Exactly one concurrent fault target must be enforced")
    if not 1 <= int(guardrails.get("maximumTargetUnavailableSeconds", 0)) <= 15:
        raise ExperimentError("Target unavailability limit must be at most 15 seconds")


def acquire_lock() -> int:
    try:
        descriptor = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise ExperimentError(f"Another experiment may be active: {LOCK_FILE}") from error
    os.write(descriptor, str(os.getpid()).encode())
    return descriptor


def preflight(experiment: dict[str, Any]) -> dict[str, Any]:
    if KILL_SWITCH.exists():
        raise ExperimentError(f"Kill switch is engaged; remove {KILL_SWITCH} before starting")

    compose("config", "--quiet")
    inspect_target(experiment["target"])
    targets = application_up()
    if any(value != 1 for value in targets.values()):
        raise ExperimentError(f"All application targets must be healthy before injection: {targets}")
    alerts = firing_safety_alerts()
    if alerts:
        raise ExperimentError(f"Safety alerts are already firing: {alerts}")
    queue = rabbitmq_state()
    if queue["consumers"] != 1 or queue["messagesReady"] != 0:
        raise ExperimentError(f"RabbitMQ must start drained with one consumer: {queue}")

    return {
        "applicationTargets": targets,
        "rabbitmq": queue,
        "mailhogMessages": mailhog_total(),
    }


def sample_guardrails(
    experiment: dict[str, Any], canary: dict[str, Any], elapsed: float
) -> dict[str, Any]:
    probes = probe_non_target_services(canary)
    error_rate, error_service = maximum_metric(
        'sum by (service) (rate(http_requests_total{status_code=~"5.."}[1m])) '
        '/ clamp_min(sum by (service) (rate(http_requests_total[1m])), 0.001) '
        'and on(service) sum by (service) (rate(http_requests_total[1m])) > 0.1'
    )
    p95_seconds, latency_service = maximum_metric(
        "histogram_quantile(0.95, sum by (service, le) "
        '(rate(http_request_duration_seconds_bucket{route!="/login"}[1m]))) '
        "and on(service) sum by (service) "
        '(rate(http_request_duration_seconds_count{route!="/login"}[1m])) > 0.1'
    )
    sample = {
        "elapsedSeconds": round(elapsed, 3),
        "syntheticProbeStatus": probes,
        "applicationTargets": application_up(),
        "maximumHttpErrorRate": error_rate,
        "errorRateService": error_service,
        "maximumP95LatencyMilliseconds": p95_seconds * 1000,
        "latencyService": latency_service,
        "rabbitmq": rabbitmq_state(),
        "firingSafetyAlerts": firing_safety_alerts(),
    }
    thresholds = experiment["guardrails"]
    target = experiment["target"]

    if KILL_SWITCH.exists():
        raise ManualAbort("Manual kill switch engaged")
    unavailable_non_targets = [
        job for job, value in sample["applicationTargets"].items()
        if job != target and value != 1
    ]
    if unavailable_non_targets:
        raise GuardrailBreach(f"Non-target service unavailable: {unavailable_non_targets}")
    if error_rate > float(thresholds["maximumHttpErrorRate"]):
        raise GuardrailBreach(
            f"HTTP error rate {error_rate:.3f} exceeded threshold for {error_service}"
        )
    if p95_seconds * 1000 > float(thresholds["maximumP95LatencyMilliseconds"]):
        raise GuardrailBreach(
            f"p95 latency {p95_seconds * 1000:.1f}ms exceeded threshold for {latency_service}"
        )
    if sample["rabbitmq"]["messagesReady"] > int(thresholds["maximumRabbitMqReadyMessages"]):
        raise GuardrailBreach("RabbitMQ notification backlog exceeded threshold")
    if sample["firingSafetyAlerts"]:
        raise GuardrailBreach(
            f"Prometheus requested abort: {sample['firingSafetyAlerts']}"
        )
    if elapsed >= float(thresholds["maximumTargetUnavailableSeconds"]):
        raise GuardrailBreach(
            f"Target unavailable for {elapsed:.1f}s; 15s safety limit reached"
        )
    return sample


def wait_for_recovery(experiment: dict[str, Any], expected_mailhog_total: int) -> dict[str, Any]:
    deadline = time.monotonic() + int(experiment["recoveryDeadlineSeconds"])
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            targets = application_up()
            queue = rabbitmq_state()
            messages = mailhog_total()
            alerts = firing_safety_alerts()
            last_state = {
                "applicationTargets": targets,
                "rabbitmq": queue,
                "mailhogMessages": messages,
                "firingSafetyAlerts": alerts,
            }
            if (
                all(value == 1 for value in targets.values())
                and queue["consumers"] == 1
                and queue["messagesReady"] == 0
                and messages >= expected_mailhog_total
                and not alerts
            ):
                return last_state
        except ExperimentError as error:
            last_state = {"waitingFor": str(error)}
        time.sleep(2)
    raise ExperimentError(f"Recovery deadline exceeded; last state: {last_state}")


def write_result(result: dict[str, Any], path: Path = RESULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def run_experiment(experiment: dict[str, Any], *, dry_run: bool,
                   result_path: Path = RESULT_PATH) -> int:
    validate_manifest(experiment)
    lock_descriptor = acquire_lock()
    target = experiment["target"]
    injected = False
    started = time.monotonic()
    result: dict[str, Any] = {
        "experiment": experiment,
        "startedAt": utc_timestamp(),
        "environment": "local-docker-compose-sandbox",
        "dryRun": dry_run,
        "status": "running",
        "samples": [],
    }

    def abort_from_signal(signum: int, _frame: Any) -> None:
        raise ManualAbort(f"Received signal {signal.Signals(signum).name}")

    previous_sigint = signal.signal(signal.SIGINT, abort_from_signal)
    previous_sigterm = signal.signal(signal.SIGTERM, abort_from_signal)

    try:
        log("Running safety preflight")
        before = preflight(experiment)
        result["before"] = before
        canary = prepare_canary()
        log("Preflight passed: all services healthy, queue drained, no alerts")

        if dry_run:
            result.update({"status": "passed", "termination": "dry_run"})
            return 0

        log(f"Injecting {experiment['fault']} into {target}")
        compose("stop", "--timeout", "1", target)
        injected = True
        injection_started = time.monotonic()
        result["injectedAt"] = utc_timestamp()

        if target in compose("ps", "--status", "running", "--services").stdout.splitlines():
            raise ExperimentError(f"Fault injection failed; {target} is still running")

        result["canaryOrder"] = create_canary_order(canary)
        log(f"Checkout stayed available; order {result['canaryOrder']['orderId']} was created")

        termination = "planned_duration"
        termination_reason = "Experiment duration completed"
        while time.monotonic() - injection_started < int(experiment["durationSeconds"]):
            if KILL_SWITCH.exists():
                termination = "kill_switch"
                termination_reason = "Manual kill switch engaged"
                log("Safety controller requested rollback: Manual kill switch engaged")
                break
            elapsed = time.monotonic() - injection_started
            unavailability_limit = float(
                experiment["guardrails"]["maximumTargetUnavailableSeconds"]
            )
            if elapsed >= unavailability_limit:
                termination = "guardrail_triggered"
                termination_reason = (
                    f"Target unavailable for {elapsed:.1f}s; "
                    f"{unavailability_limit:.0f}s safety limit reached"
                )
                log(f"Safety controller requested rollback: {termination_reason}")
                break
            try:
                sample = sample_guardrails(experiment, canary, elapsed)
                result["samples"].append(sample)
                if (sample["applicationTargets"].get(target) == 0
                        and sample["rabbitmq"]["messagesReady"] >= 1):
                    log("Fault evidence captured: target down and notification buffered")
                log(
                    "Guardrails healthy: "
                    f"elapsed={elapsed:.1f}s, ready={sample['rabbitmq']['messagesReady']}, "
                    f"p95={sample['maximumP95LatencyMilliseconds']:.1f}ms"
                )
            except (GuardrailBreach, ManualAbort) as error:
                termination = "kill_switch" if isinstance(error, ManualAbort) else "guardrail_triggered"
                termination_reason = str(error)
                log(f"Safety controller requested rollback: {error}")
                break
            remaining_safe_time = unavailability_limit - (
                time.monotonic() - injection_started
            )
            time.sleep(
                max(
                    0,
                    min(
                        float(experiment["monitorIntervalSeconds"]),
                        remaining_safe_time,
                    ),
                )
            )

        result["termination"] = termination
        result["terminationReason"] = termination_reason
    except (ExperimentError, KeyboardInterrupt) as error:
        result["status"] = "failed"
        result["error"] = str(error)
        log(f"Experiment error: {error}")
    finally:
        if injected:
            log(f"Rolling back fault: starting {target}")
            rollback_started = time.monotonic()
            rollback_error = None
            try:
                compose("start", target)
                before_messages = int(result.get("before", {}).get("mailhogMessages", 0))
                result["after"] = wait_for_recovery(experiment, before_messages + 1)
                result["recoverySeconds"] = round(time.monotonic() - rollback_started, 3)
                log(f"Recovery verified in {result['recoverySeconds']}s")
            except ExperimentError as error:
                rollback_error = str(error)
                result["status"] = "failed"
                result["rollbackError"] = rollback_error
                log(f"Rollback verification failed: {error}")

            if not rollback_error and result.get("canaryOrder"):
                observed_down = any(
                    sample.get("applicationTargets", {}).get(target) == 0
                    for sample in result["samples"]
                )
                observed_backlog = max(
                    (sample.get("rabbitmq", {}).get("messagesReady", 0) for sample in result["samples"]),
                    default=0,
                )
                result["assertions"] = {
                    "faultObservedByPrometheus": observed_down,
                    "checkoutAvailableDuringFault": result["canaryOrder"]["httpStatus"] == 201,
                    "rabbitMqBufferedNotification": observed_backlog >= 1,
                    "automaticRollbackExecuted": True,
                    "recoveryWithinDeadline": result["recoverySeconds"]
                    <= int(experiment["recoveryDeadlineSeconds"]),
                }
                if all(result["assertions"].values()) and "error" not in result:
                    result["status"] = "passed"
                else:
                    result["status"] = "failed"
                    result["error"] = f"Experiment assertions failed: {result['assertions']}"

        result["finishedAt"] = utc_timestamp()
        result["totalSeconds"] = round(time.monotonic() - started, 3)
        try:
            write_result(result, result_path)
        finally:
            signal.signal(signal.SIGINT, previous_sigint)
            signal.signal(signal.SIGTERM, previous_sigterm)
            os.close(lock_descriptor)
            LOCK_FILE.unlink(missing_ok=True)

    return 0 if result.get("status") == "passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, default=DEFAULT_EXPERIMENT)
    parser.add_argument("--dry-run", action="store_true", help="Validate safety preconditions only")
    parser.add_argument("--result", type=Path, default=RESULT_PATH)
    arguments = parser.parse_args()

    try:
        experiment = json.loads(arguments.experiment.read_text(encoding="utf-8"))
        return run_experiment(experiment, dry_run=arguments.dry_run, result_path=arguments.result)
    except (OSError, json.JSONDecodeError, ExperimentError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
