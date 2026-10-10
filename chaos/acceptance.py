"""Live Docker acceptance: bounded pause, kill, threshold, crash, lease, and restart."""
from __future__ import annotations

import argparse
import copy
import json
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from chaos.run_experiment import (MAILHOG_API, compose, create_canary_order, http_json,
                                  prepare_canary, rabbitmq_state)
from chaos.workload import BrowseLoad
from safety.client import ActuatorClient
from safety.configuration import ROOT, load_policy
from safety.controller import SafetyController
from safety.docker_api import DockerApi
from safety.monitoring import PrometheusMonitoringAdapter


RESULT_DIR = ROOT / "chaos/results"


def wait_until(predicate, timeout: float = 30, description: str = "condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except Exception as error:
            last = str(error)
        time.sleep(.25)
    raise RuntimeError(f"Timed out waiting for {description}: {last}")


def order_email(order_id: str) -> bool:
    status, response = http_json(MAILHOG_API + "?limit=1000")
    return status == 200 and any(order_id in json.dumps(item.get("Content", {}))
                                 for item in response.get("items", []))


def exact_queue() -> dict:
    rows = json.loads(compose("exec", "-T", "rabbitmq", "rabbitmqctl", "list_queues", "name",
                              "messages_ready", "messages_unacknowledged", "--formatter", "json").stdout)
    row = next(item for item in rows if item["name"] == "notifications")
    return {"messagesReady": row["messages_ready"], "messagesUnacknowledged": row["messages_unacknowledged"],
            "source": "rabbitmqctl direct queue counters"}


def run_suite(only: list[str] | None = None) -> dict:
    policy = load_policy()
    client = ActuatorClient(timeout=10)
    try:
        client.ping()
    except Exception:
        pass
    else:
        raise RuntimeError("A watchdog is already running; finish it before acceptance")
    suite_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    journal = ROOT / f"safety/state/acceptance-{suite_id}.db"
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    actuator_log = (RESULT_DIR / "actuator-acceptance.log").open("w")
    actuator = None
    controller_process = None
    cases = []
    adapter = PrometheusMonitoringAdapter(policy)
    verifier = SafetyController(policy, adapter, client)
    docker = DockerApi()
    target = docker.resolve_compose_service(policy["sandbox"]["composeProject"], "notification-service", ROOT)

    def start_actuator():
        process = subprocess.Popen([sys.executable, "-m", "safety.actuator", "--serve", "--journal", str(journal)],
                                   cwd=ROOT, stdout=actuator_log, stderr=subprocess.STDOUT)
        wait_until(lambda: process.poll() is None and client.ping(), description="watchdog startup")
        return process

    def healthy_hold():
        healthy_since = None
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                verifier.verify_reset_health()
                if docker.inspect(target["Id"])["State"]["Paused"]:
                    raise RuntimeError("Target still paused")
                if healthy_since is None:
                    healthy_since = time.monotonic()
                if time.monotonic() - healthy_since >= 5:
                    return
            except Exception:
                healthy_since = None
            time.sleep(1)
        raise RuntimeError("Healthy recovery hold was not confirmed")

    suite = {"schemaVersion": 1, "startedAt": datetime.now(timezone.utc).isoformat(),
             "environment": "local-docker-compose-sandbox", "policyVersion": policy["policyVersion"],
             "status": "running", "cases": cases}
    modes = only or ["completed", "latency-threshold", "manual-kill", "controller-sigterm",
                     "controller-crash", "lease-expiry", "actuator-restart"]
    try:
        actuator = start_actuator()
        with BrowseLoad() as load:
            wait_until(lambda: verifier.verify_reset_health(), timeout=75, description="adequate live browse traffic")
            canary = prepare_canary()
            for mode in modes:
                print(f"Acceptance: {mode}", flush=True)
                healthy_hold()
                client.call("reset", healthVerified=True)
                case = {"name": mode, "status": "running", "observations": [], "assertions": {}}
                cases.append(case)
                case_started = time.monotonic()
                result_path = RESULT_DIR / f"pause-{mode}.json"
                controller_result = None
                if mode in {"lease-expiry", "actuator-restart"}:
                    experiment_id = str(uuid.uuid4())
                    generation = client.call("begin_experiment", experimentId=experiment_id,
                                             profile="notification-pause", target="notification-service",
                                             policyVersion=policy["policyVersion"])["generation"]
                    fault = client.call("prepare", spec={"experiment_id": experiment_id,
                                       "target_service": "notification-service", "fault_type": "container-pause",
                                       "duration_seconds": 20, "generation": generation})
                    client.call("apply", faultId=fault["faultId"], generation=generation)
                    controller_process = None
                else:
                    profile = "notification-pause" if mode == "latency-threshold" else "notification-pause-acceptance"
                    command = [sys.executable, "-m", "safety", "run", "--profile", profile, "--result", str(result_path)]
                    if mode == "latency-threshold":
                        # A stricter ceiling triggers the real measured p95 signal; telemetry is not mocked.
                        stricter = copy.deepcopy(policy)
                        stricter["abort"].update({"absoluteP95Milliseconds": 1,
                                                  "relativeP95FloorMilliseconds": 1,
                                                  "relativeP95Multiplier": .01,
                                                  "sustainedBreachSeconds": 3})
                        stricter["profiles"][profile]["recoveryHoldSeconds"] = 5
                        policy_path = ROOT / "safety/runtime/threshold-acceptance-policy.json"
                        policy_path.write_text(json.dumps(stricter))
                        command += ["--policy", str(policy_path)]
                        case["stricterThresholdOverride"] = {"p95Milliseconds": 1, "holdSeconds": 3}
                    controller_process = subprocess.Popen(command, cwd=ROOT)
                wait_until(lambda: docker.inspect(target["Id"])["State"].get("Paused"),
                           timeout=10, description="actual container pause")
                active_fault_id = client.call("status")["faults"][0]["id"]
                case["faultId"] = active_fault_id
                case["assertions"]["faultPresentInDocker"] = True
                order = create_canary_order(canary)
                case["canaryOrder"] = order
                queue = exact_queue()
                if queue["messagesReady"] + queue["messagesUnacknowledged"] < 1:
                    raise RuntimeError(f"No buffered event in direct RabbitMQ counters: {queue}")
                case["observations"].append({"elapsedSeconds": round(time.monotonic() - case_started, 3),
                                             "queue": queue, "monitoring": adapter.snapshot().to_dict()})
                case["assertions"]["checkoutAvailableDuringFault"] = order["httpStatus"] == 201
                case["assertions"]["eventBufferedDuringFault"] = True
                if mode == "manual-kill":
                    case["operatorAction"] = client.call("kill", reason="MANUAL_KILL")
                elif mode == "controller-sigterm":
                    controller_process.send_signal(signal.SIGTERM)
                elif mode == "controller-crash":
                    controller_process.kill()
                elif mode == "actuator-restart":
                    actuator.kill()
                    actuator.wait(timeout=5)
                    actuator = start_actuator()
                if controller_process:
                    exit_code = controller_process.wait(timeout=100)
                    case["controllerExitCode"] = exit_code
                    if mode != "controller-crash":
                        controller_result = json.loads(result_path.read_text())
                        controller_result["runKind"] = "experiment" if mode == "completed" else "safety-check"
                        result_path.write_text(json.dumps(controller_result, indent=2) + "\n")
                        expected = {"completed": "MAX_DURATION", "latency-threshold": "LATENCY",
                                    "manual-kill": "MANUAL_KILL", "controller-sigterm": "MANUAL_KILL"}[mode]
                        case["assertions"]["expectedTerminationReason"] = controller_result["reasonCode"] == expected
                        case["assertions"]["controllerConfirmedRecovery"] = controller_result["recoveryStatus"] == "confirmed"
                        case["controllerResult"] = str(result_path.relative_to(ROOT))
                wait_until(lambda: not docker.inspect(target["Id"])["State"].get("Paused"),
                           timeout=12, description="independent unpause")
                healthy_hold()
                wait_until(lambda: order_email(order["orderId"]) and rabbitmq_state()["messagesReady"] == 0
                           and rabbitmq_state()["messagesUnacknowledged"] == 0,
                           description="canary email and queue drain")
                status = client.call("status")
                if mode in {"controller-crash", "lease-expiry", "actuator-restart"}:
                    case["assertions"]["watchdogRollbackRecorded"] = any(
                        event["state"] == "ROLLED_BACK" and event["reason_code"] == "WATCHDOG_EXPIRED"
                        and event["fault_id"] == active_fault_id
                        for event in status["events"])
                case["assertions"].update({"targetUnpausedAfterRollback": not docker.inspect(target["Id"])["State"]["Paused"],
                                          "canaryEmailDelivered": order_email(order["orderId"]),
                                          "queueDrained": rabbitmq_state()["messagesReady"] == 0,
                                          "noResidualFaults": all(item["cleanup_verified"] for item in status["faults"])})
                case["totalSeconds"] = round(time.monotonic() - case_started, 3)
                case["status"] = "passed" if all(case["assertions"].values()) else "failed"
                (RESULT_DIR / "safety-acceptance.json").write_text(json.dumps(suite, indent=2) + "\n")
                if case["status"] != "passed":
                    raise RuntimeError(f"Acceptance assertions failed for {mode}: {case['assertions']}")
                print(f"Passed: {mode} ({case['totalSeconds']}s)", flush=True)
            suite["workload"] = {"requests": load.requests, "failedRequests": load.failures}
            suite["status"] = "passed" if not load.failures else "failed"
            verifier.verify_reset_health()
            client.call("reset", healthVerified=True)
            history_path = RESULT_DIR / "history.json"
            history = json.loads(history_path.read_text()) if history_path.exists() else []
            if "completed" in modes:
                completed = json.loads((RESULT_DIR / "pause-completed.json").read_text())
                if not any(item.get("experimentId") == completed["experimentId"] for item in history):
                    history.append(completed)
            history_path.write_text(json.dumps(history, indent=2) + "\n")
    except Exception as error:
        suite["status"] = "failed"
        suite["error"] = str(error)
        for case in cases:
            if case["status"] == "running":
                case.update({"status": "failed", "error": str(error)})
        raise
    finally:
        if controller_process and controller_process.poll() is None:
            controller_process.terminate()
            controller_process.wait(timeout=20)
        if actuator and actuator.poll() is None:
            try:
                client.call("kill", reason="CONTROLLER_CANCELLED")
                client.call("shutdown")
                actuator.wait(timeout=10)
            except Exception as error:
                suite["actuatorShutdownError"] = str(error)
                suite["status"] = "failed"
                actuator.terminate()
                actuator.wait(timeout=10)
        # Last resort only touches the exact sandbox container identity resolved above.
        if docker.inspect(target["Id"])["State"].get("Paused"):
            docker.unpause(target["Id"])
            suite["status"] = "failed"
            suite["emergencyUnpauseRequired"] = True
        if journal.exists():
            db = sqlite3.connect(journal)
            db.row_factory = sqlite3.Row
            export = {table: [dict(row) for row in db.execute(f"SELECT * FROM {table}")]
                      for table in ("system_state", "experiments", "faults", "events")}
            db.close()
            (RESULT_DIR / "safety-journal.json").write_text(json.dumps(export, indent=2) + "\n")
        actuator_log.close()
        suite["finishedAt"] = datetime.now(timezone.utc).isoformat()
        (RESULT_DIR / "safety-acceptance.json").write_text(json.dumps(suite, indent=2) + "\n")
    return suite


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="+", choices=["completed", "latency-threshold", "manual-kill",
                        "controller-sigterm", "controller-crash", "lease-expiry", "actuator-restart"])
    args = parser.parse_args()
    suite = run_suite(args.only)
    print(f"Safety acceptance: {suite['status']} ({len(suite['cases'])} cases)")
    return 0 if suite["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
