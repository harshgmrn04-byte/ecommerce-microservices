from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import socketserver
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .configuration import ROOT, load_policy
from .docker_api import DockerApi, DockerApiError
from .journal import Journal, JournalError, utc_now
from .models import Lifecycle, ReasonCode


RUNTIME_DIR = ROOT / "safety/runtime"
STATE_DIR = ROOT / "safety/state"
SOCKET_PATH = RUNTIME_DIR / "actuator.sock"
PID_PATH = RUNTIME_DIR / "actuator.pid"
JOURNAL_PATH = STATE_DIR / "safety.db"


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value)


def future_utc(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="milliseconds"
    )


class Actuator:
    def __init__(self, policy: dict[str, Any], journal: Journal, docker: DockerApi):
        self.policy = policy
        self.journal = journal
        self.docker = docker
        self.mutation_lock = threading.RLock()
        self.shutdown_event = threading.Event()
        self.server: ActuatorServer | None = None

    @property
    def working_directory(self) -> Path:
        configured = Path(self.policy["sandbox"]["composeWorkingDirectory"])
        return configured if configured.is_absolute() else ROOT / configured

    def startup_reconcile(self) -> dict[str, Any]:
        self.journal.set_reconciliation(False)
        unfinished = self.journal.unfinished_faults()
        results: list[dict[str, Any]] = []
        if unfinished:
            self.journal.latch_kill("Actuator restart requires conservative reconciliation")
            for fault in unfinished:
                results.append(
                    self.revoke_and_rollback(
                        fault["id"], ReasonCode.WATCHDOG_EXPIRED.value, latch=False
                    )
                )
        remaining = self.journal.unfinished_faults()
        self.journal.set_reconciliation(not remaining)
        if not remaining:
            self.journal.finalize_orphans()
        return {"reconciled": results, "remaining": [item["id"] for item in remaining]}

    def validate_spec(self, spec: dict[str, Any]) -> None:
        exact_keys = {
            "experiment_id",
            "target_service",
            "fault_type",
            "duration_seconds",
            "generation",
        }
        if set(spec) != exact_keys:
            raise ValueError(f"Fault spec fields must be exactly {sorted(exact_keys)}")
        faults = self.policy["faults"]
        if spec["target_service"] not in faults["allowedTargets"]:
            raise ValueError("Target is outside the application allowlist")
        if spec["fault_type"] != "container-pause":
            raise ValueError("Only container-pause is supported")
        if not 1 <= int(spec["duration_seconds"]) <= int(faults["maximumDurationSeconds"]):
            raise ValueError("Fault duration exceeds policy")

    def prepare(self, spec: dict[str, Any]) -> dict[str, Any]:
        self.validate_spec(spec)
        with self.mutation_lock:
            system = self.journal.system_state()
            if system["kill_latched"] or not system["reconciliation_complete"]:
                raise JournalError("Admission is latched off")
            if int(spec["generation"]) != int(system["generation"]):
                raise JournalError("Stale controller generation")
            if self.journal.unfinished_faults():
                raise JournalError("An unfinished fault already exists")

            container = self.docker.resolve_compose_service(
                self.policy["sandbox"]["composeProject"],
                spec["target_service"],
                self.working_directory,
            )
            state = container["State"]
            if not state.get("Running") or state.get("Paused"):
                raise DockerApiError("Target must initially be running and unpaused")

            now = datetime.now(timezone.utc)
            maximum = now + timedelta(seconds=int(spec["duration_seconds"]))
            lease_seconds = int(self.policy["timing"]["watchdogLeaseSeconds"])
            lease = min(now + timedelta(seconds=lease_seconds), maximum)
            ownership = str(uuid.uuid4())
            fault_id = self.journal.create_fault(
                {
                    "experiment_id": spec["experiment_id"],
                    "generation": spec["generation"],
                    "target_service": spec["target_service"],
                    "container_id": container["Id"],
                    "fault_type": spec["fault_type"],
                    "original_state": {
                        "running": state.get("Running"),
                        "paused": state.get("Paused"),
                        "restartCount": container.get("RestartCount"),
                        "image": container.get("Image"),
                    },
                    "spec": spec,
                    "lease_expires_utc": lease.isoformat(timespec="milliseconds"),
                    "maximum_expires_utc": maximum.isoformat(timespec="milliseconds"),
                    "ownership_token": ownership,
                }
            )
            self.journal.add_event(
                experiment_id=spec["experiment_id"],
                fault_id=fault_id,
                state="PREPARED",
                reason_code=None,
                target=spec["target_service"],
                details={
                    "containerId": container["Id"],
                    "faultType": spec["fault_type"],
                    "durationSeconds": spec["duration_seconds"],
                    "generation": spec["generation"],
                    "rollbackIntentPersisted": True,
                },
            )
            return {
                "faultId": fault_id,
                "containerId": container["Id"],
                "ownershipToken": ownership,
                "leaseExpiresUtc": lease.isoformat(timespec="milliseconds"),
                "maximumExpiresUtc": maximum.isoformat(timespec="milliseconds"),
            }

    def apply(self, fault_id: str, generation: int) -> dict[str, Any]:
        with self.mutation_lock:
            fault = self.journal.fault(fault_id)
            system = self.journal.system_state()
            if not fault:
                raise JournalError("Unknown fault")
            if int(generation) != int(fault["generation"]) or int(generation) != int(system["generation"]):
                raise JournalError("Stale controller generation")
            if system["kill_latched"] or fault["desired_cancel"]:
                raise JournalError("Fault activation was cancelled")
            if fault["state"] != "PREPARED":
                raise JournalError(f"Fault cannot be applied from state {fault['state']}")
            now = datetime.now(timezone.utc)
            if now >= min(parse_utc(fault["lease_expires_utc"]), parse_utc(fault["maximum_expires_utc"])):
                self.revoke_and_rollback(fault_id, ReasonCode.WATCHDOG_EXPIRED.value, latch=True)
                raise JournalError("Prepared fault lease expired before activation")

            self.journal.update_fault(fault_id, state="APPLYING")
            fault = self.journal.fault(fault_id)
            system = self.journal.system_state()
            if system["kill_latched"] or fault["desired_cancel"] or int(system["generation"]) != generation:
                self.journal.update_fault(fault_id, state="CANCELLED", cleanup_verified=1)
                raise JournalError("Fault activation was revoked before mutation")

            try:
                self.docker.pause(fault["container_id"])
                inspected = self.docker.inspect(fault["container_id"])
                if not inspected["State"].get("Paused"):
                    raise DockerApiError("Docker did not report the target as paused")
                self.journal.update_fault(fault_id, state="ACTIVE")
                self.journal.add_event(
                    experiment_id=fault["experiment_id"],
                    fault_id=fault_id,
                    state="ACTIVE",
                    reason_code=None,
                    target=fault["target_service"],
                    details={"containerId": fault["container_id"], "faultPresent": True},
                )
                return self.inspect(fault_id)
            except Exception:
                self.journal.update_fault(fault_id, desired_cancel=1)
                self.revoke_and_rollback(fault_id, ReasonCode.CLEANUP_FAILED.value, latch=True)
                raise

    def renew(self, fault_id: str, generation: int) -> dict[str, Any]:
        with self.mutation_lock:
            fault = self.journal.fault(fault_id)
            system = self.journal.system_state()
            if not fault or fault["state"] != "ACTIVE":
                raise JournalError("Only an active fault lease can be renewed")
            if system["kill_latched"] or fault["desired_cancel"]:
                raise JournalError("Fault lease has been revoked")
            if int(generation) != int(system["generation"]) or int(generation) != int(fault["generation"]):
                raise JournalError("Stale controller generation")
            now = datetime.now(timezone.utc)
            maximum = parse_utc(fault["maximum_expires_utc"])
            if now >= maximum:
                raise JournalError("Absolute fault deadline has expired")
            lease = min(
                now + timedelta(seconds=int(self.policy["timing"]["watchdogLeaseSeconds"])),
                maximum,
            )
            self.journal.update_fault(
                fault_id, lease_expires_utc=lease.isoformat(timespec="milliseconds")
            )
            return {"faultId": fault_id, "leaseExpiresUtc": lease.isoformat(timespec="milliseconds")}

    def inspect(self, fault_id: str) -> dict[str, Any]:
        fault = self.journal.fault(fault_id)
        if not fault:
            raise JournalError("Unknown fault")
        evidence: dict[str, Any]
        try:
            container = self.docker.inspect(fault["container_id"])
            evidence = {
                "containerExists": True,
                "running": bool(container["State"].get("Running")),
                "paused": bool(container["State"].get("Paused")),
                "containerId": container["Id"],
            }
        except DockerApiError as error:
            evidence = {"containerExists": False, "error": str(error)}
        return {
            "faultId": fault_id,
            "state": fault["state"],
            "desiredCancel": bool(fault["desired_cancel"]),
            "cleanupVerified": bool(fault["cleanup_verified"]),
            "target": fault["target_service"],
            "evidence": evidence,
        }

    def revoke_and_rollback(
        self, fault_id: str, reason: str, *, latch: bool = False
    ) -> dict[str, Any]:
        with self.mutation_lock:
            if latch:
                self.journal.latch_kill(reason)
            fault = self.journal.fault(fault_id)
            if not fault:
                raise JournalError("Unknown fault")
            if fault["cleanup_verified"]:
                return self.inspect(fault_id)

            self.journal.update_fault(fault_id, desired_cancel=1, state="CLEANING_UP")
            self.journal.add_event(
                experiment_id=fault["experiment_id"],
                fault_id=fault_id,
                state=Lifecycle.CLEANING_UP.value,
                reason_code=reason,
                target=fault["target_service"],
                details={"rollbackStarted": True},
            )
            last_error = ""
            retries = int(self.policy["timing"]["rollbackRetryCount"])
            for attempt in range(1, retries + 1):
                try:
                    container = self.docker.inspect(fault["container_id"])
                    if container["State"].get("Paused"):
                        self.docker.unpause(fault["container_id"])
                    verified = self.docker.inspect(fault["container_id"])
                    if verified["State"].get("Paused"):
                        raise DockerApiError("Target remains paused after rollback")
                    self.journal.update_fault(
                        fault_id,
                        state="ROLLED_BACK",
                        cleanup_verified=1,
                        residual_evidence=None,
                    )
                    self.journal.add_event(
                        experiment_id=fault["experiment_id"],
                        fault_id=fault_id,
                        state="ROLLED_BACK",
                        reason_code=reason,
                        target=fault["target_service"],
                        details={
                            "cleanupVerified": True,
                            "containerId": fault["container_id"],
                            "attempt": attempt,
                        },
                    )
                    return self.inspect(fault_id)
                except Exception as error:
                    last_error = str(error)
                    if attempt < retries:
                        time.sleep(min(2 ** (attempt - 1), 4))

            self.journal.latch_kill(f"Cleanup failed: {last_error}")
            self.journal.update_fault(
                fault_id,
                state=Lifecycle.CLEANUP_FAILED.value,
                residual_evidence=last_error,
            )
            self.journal.add_event(
                experiment_id=fault["experiment_id"],
                fault_id=fault_id,
                state=Lifecycle.CLEANUP_FAILED.value,
                reason_code=ReasonCode.CLEANUP_FAILED.value,
                target=fault["target_service"],
                details={"cleanupVerified": False, "residualEvidence": last_error},
            )
            raise DockerApiError(f"Rollback could not be verified: {last_error}")

    def kill_all(self, reason: str) -> dict[str, Any]:
        with self.mutation_lock:
            self.journal.latch_kill(reason)
            results = []
            errors = []
            for fault in self.journal.unfinished_faults():
                try:
                    results.append(self.revoke_and_rollback(fault["id"], reason, latch=False))
                except Exception as error:
                    errors.append({"faultId": fault["id"], "error": str(error)})
            return {
                "killLatched": True,
                "reason": reason,
                "rolledBack": results,
                "errors": errors,
            }

    def reconcile(self) -> dict[str, Any]:
        with self.mutation_lock:
            self.journal.set_reconciliation(False)
            results = []
            errors = []
            unfinished = self.journal.unfinished_faults()
            if unfinished:
                self.journal.latch_kill("Emergency reconciliation")
            for fault in unfinished:
                try:
                    results.append(
                        self.revoke_and_rollback(
                            fault["id"], ReasonCode.WATCHDOG_EXPIRED.value, latch=False
                        )
                    )
                except Exception as error:
                    errors.append({"faultId": fault["id"], "error": str(error)})
            self.journal.set_reconciliation(not errors and not self.journal.unfinished_faults())
            if not errors:
                self.journal.finalize_orphans()
            return {"reconciled": results, "errors": errors}

    def watchdog_loop(self) -> None:
        interval = float(self.policy["timing"]["watchdogEvaluationSeconds"])
        while not self.shutdown_event.wait(interval):
            now = datetime.now(timezone.utc)
            for fault in self.journal.unfinished_faults():
                if fault["state"] not in {"ACTIVE", "APPLYING", "PREPARED"}:
                    continue
                maximum = parse_utc(fault["maximum_expires_utc"])
                lease = parse_utc(fault["lease_expires_utc"]) if fault["lease_expires_utc"] else maximum
                reason = None
                if now >= maximum:
                    reason = ReasonCode.MAX_DURATION.value
                elif now >= lease:
                    reason = ReasonCode.WATCHDOG_EXPIRED.value
                if reason:
                    try:
                        if reason == ReasonCode.MAX_DURATION.value:
                            self.revoke_and_rollback(fault["id"], reason)
                        else:
                            self.kill_all(reason)
                    except Exception as error:
                        self.journal.add_event(
                            experiment_id=fault["experiment_id"],
                            fault_id=fault["id"],
                            state=Lifecycle.CLEANUP_FAILED.value,
                            reason_code=ReasonCode.CLEANUP_FAILED.value,
                            target=fault["target_service"],
                            details={"watchdogError": str(error)},
                        )

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        if action == "ping":
            return {"pid": os.getpid(), "timestamp": utc_now(), "system": self.journal.system_state()}
        if action == "status":
            return self.journal.status()
        if action == "begin_experiment":
            generation = self.journal.begin_experiment(
                request["experimentId"], request["profile"], request["target"], request["policyVersion"]
            )
            return {"generation": generation}
        if action == "transition":
            self.journal.transition(
                request["experimentId"],
                request["state"],
                outcome=request.get("outcome"),
                cleanup_status=request.get("cleanupStatus"),
                recovery_status=request.get("recoveryStatus"),
                baseline=request.get("baseline"),
            )
            self.journal.add_event(
                experiment_id=request["experimentId"],
                state=request["state"],
                reason_code=request.get("reasonCode"),
                target=request.get("target"),
                details=request.get("details", {}),
                correlation_id=request.get("correlationId"),
            )
            return {"state": request["state"]}
        if action == "prepare":
            return self.prepare(request["spec"])
        if action == "apply":
            return self.apply(request["faultId"], int(request["generation"]))
        if action == "renew":
            return self.renew(request["faultId"], int(request["generation"]))
        if action == "inspect":
            return self.inspect(request["faultId"])
        if action == "revoke":
            return self.revoke_and_rollback(
                request["faultId"], request.get("reason", ReasonCode.CONTROLLER_CANCELLED.value)
            )
        if action == "kill":
            return self.kill_all(request.get("reason") or ReasonCode.MANUAL_KILL.value)
        if action == "reconcile":
            return self.reconcile()
        if action == "reset":
            if request.get("healthVerified") is not True:
                raise JournalError("Reset requires a verified healthy preflight")
            self.journal.finalize_orphans()
            return {"generation": self.journal.reset_kill(), "killLatched": False}
        if action == "shutdown":
            if self.journal.unfinished_faults():
                raise JournalError("Actuator cannot shut down with unfinished faults")
            self.shutdown_event.set()
            if self.server:
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            return {"shuttingDown": True}
        raise ValueError(f"Unsupported actuator action: {action}")


class RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(1024 * 1024)
        try:
            request = json.loads(raw.decode("utf-8"))
            result = self.server.actuator.handle(request)  # type: ignore[attr-defined]
            response = {"ok": True, "result": result}
        except Exception as error:
            response = {"ok": False, "error": f"{type(error).__name__}: {error}"}
        self.wfile.write((json.dumps(response, default=str) + "\n").encode("utf-8"))


class ActuatorServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, socket_path: str, actuator: Actuator):
        self.actuator = actuator
        super().__init__(socket_path, RequestHandler)


def serve(journal_path: Path = JOURNAL_PATH) -> int:
    policy = load_policy()
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Do not unlink a socket owned by another live watchdog.
    process_lock = (RUNTIME_DIR / "actuator.lock").open("w")
    fcntl.flock(process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    SOCKET_PATH.unlink(missing_ok=True)
    journal = Journal(journal_path)
    actuator = Actuator(policy, journal, DockerApi(timeout=5))
    actuator.docker.ping()
    actuator.startup_reconcile()
    server = ActuatorServer(str(SOCKET_PATH), actuator)
    actuator.server = server
    os.chmod(SOCKET_PATH, 0o600)
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")

    watchdog = threading.Thread(target=actuator.watchdog_loop, name="safety-watchdog", daemon=True)
    watchdog.start()

    def stop_handler(_signum: int, _frame: Any) -> None:
        actuator.shutdown_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        if journal.unfinished_faults():
            actuator.kill_all(ReasonCode.CONTROLLER_CANCELLED.value)
        actuator.shutdown_event.set()
        server.server_close()
        SOCKET_PATH.unlink(missing_ok=True)
        PID_PATH.unlink(missing_ok=True)
        process_lock.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Local safety actuator and watchdog")
    parser.add_argument("--serve", action="store_true", required=True)
    parser.add_argument("--journal", type=Path, default=JOURNAL_PATH)
    args = parser.parse_args()
    return serve(args.journal)


if __name__ == "__main__":
    raise SystemExit(main())
