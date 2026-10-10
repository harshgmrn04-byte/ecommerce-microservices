from __future__ import annotations

import json
import math
import signal
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .client import ActuatorClient, ActuatorClientError
from .configuration import ROOT, resolve_policy_path
from .models import Baseline, Lifecycle, ReasonCode, Validity
from .monitoring import MonitoringAdapter
from .policy import SafetyPolicyEvaluator


class ControllerError(RuntimeError):
    pass


def load_baseline(policy: dict[str, Any]) -> Baseline:
    path = resolve_policy_path(policy, policy["admission"]["baselineArtifact"])
    artifact = json.loads(path.read_text(encoding="utf-8"))
    if artifact.get("status") != "passed":
        raise ControllerError("Recorded monitoring baseline did not pass")
    captured = datetime.fromisoformat(artifact["capturedAt"])
    age = datetime.now().astimezone().timestamp() - captured.timestamp()
    if not 0 <= age <= float(policy["admission"]["baselineMaximumAgeSeconds"]):
        raise ControllerError(f"Recorded baseline is stale ({age:.0f}s old)")
    error_fraction = float(artifact["http"]["failureRate"])
    if not math.isfinite(error_fraction) or not 0 <= error_fraction < float(
        policy["admission"]["maximumBaselineErrorFraction"]):
        raise ControllerError("Baseline error fraction blocks admission")
    p95 = float(artifact["http"]["latencyMilliseconds"]["p95"])
    if not math.isfinite(p95) or not 0 <= p95 < float(
        policy["admission"]["maximumBaselineP95Milliseconds"]):
        raise ControllerError("Baseline latency blocks admission")
    if any(int(value) != 0 for value in artifact["availability"]["containerRestarts"].values()):
        raise ControllerError("Baseline contains unresolved container restarts")
    service_counts = artifact.get("workload", {}).get("serviceRequestCounts", {})
    protected = policy["monitoring"]["protectedHttpServices"]
    minimum = int(policy["admission"]["minimumRequestCountPerWindow"])
    for service in protected:
        service_p95 = float(artifact["http"]["serviceP95Milliseconds"][service])
        if not math.isfinite(service_p95) or not 0 <= service_p95 < float(
                policy["admission"]["maximumBaselineP95Milliseconds"]):
            raise ControllerError(f"Baseline latency for {service} blocks admission")
        if int(service_counts.get(service, 0)) < minimum:
            raise ControllerError(
                f"Baseline traffic for {service} is insufficient: {service_counts.get(service, 0)} < {minimum}"
            )
    return Baseline(
        reference=str(path.relative_to(ROOT)),
        captured_at=artifact["capturedAt"],
        error_fraction={service: float(artifact["http"]["failureRate"]) for service in protected},
        p95_latency_ms={
            service: float(artifact["http"]["serviceP95Milliseconds"][service])
            for service in protected
        },
        request_counts={service: int(service_counts[service]) for service in protected},
        artifact=artifact,
    )


class SafetyController:
    def __init__(
        self,
        policy: dict[str, Any],
        monitoring: MonitoringAdapter,
        actuator: ActuatorClient,
        clock=time.monotonic,
        sleeper=time.sleep,
    ):
        self.policy = policy
        self.monitoring = monitoring
        self.actuator = actuator
        self.clock = clock
        self.sleeper = sleeper
        self.cancel_reason: ReasonCode | None = None
        self.heartbeat_error: str | None = None
        self.heartbeat_stop = threading.Event()

    def _transition(
        self,
        experiment_id: str,
        state: Lifecycle,
        *,
        reason: ReasonCode | None = None,
        details: dict[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        self.actuator.call(
            "transition",
            experimentId=experiment_id,
            state=state.value,
            reasonCode=reason.value if reason else None,
            target=extra.pop("target", None),
            details=details or {},
            **extra,
        )

    def _healthy_preflight(self, snapshot, *, expected_absent: str | None = None) -> None:
        if snapshot.telemetry_error:
            raise ControllerError(f"Monitoring is unavailable: {snapshot.telemetry_error}")
        required = set(self.policy["monitoring"]["requiredJobs"]) - {expected_absent}
        if required - snapshot.target_up.keys():
            raise ControllerError("Required telemetry targets are missing")
        required_probes = {probe["id"] for probe in self.policy["monitoring"]["probes"]}
        if required_probes - snapshot.probes.keys():
            raise ControllerError("Required critical probes are missing")
        maximum_age = float(self.policy["abort"]["maximumSourceAgeSeconds"])
        for service in required:
            metric = snapshot.target_up[service]
            if (metric.source_timestamp is None
                    or not math.isfinite(metric.source_timestamp)
                    or not 0 <= snapshot.observed_at - metric.source_timestamp <= maximum_age):
                raise ControllerError(f"Telemetry for {service} is stale or invalid")
        for service in self.policy["monitoring"]["protectedHttpServices"]:
            for values, ceiling in (
                (snapshot.error_fraction, self.policy["admission"]["maximumBaselineErrorFraction"]),
                (snapshot.p95_latency_ms, self.policy["admission"]["maximumBaselineP95Milliseconds"]),
            ):
                metric = values.get(service)
                if (metric is None or metric.validity != Validity.HEALTHY
                        or metric.value is None or not math.isfinite(metric.value)
                        or not 0 <= metric.value < float(ceiling)
                        or metric.request_count is None
                        or metric.request_count < self.policy["admission"]["minimumRequestCountPerWindow"]
                        or metric.source_timestamp is None
                        or not math.isfinite(metric.source_timestamp)
                        or not 0 <= snapshot.observed_at - metric.source_timestamp <= maximum_age):
                    raise ControllerError(f"Live baseline metric for {service} is unhealthy or incomplete")
        failures = [probe_id for probe_id, probe in snapshot.probes.items() if not probe.valid]
        if failures:
            raise ControllerError(f"Critical probes failed: {failures}")
        unhealthy = [
            service
            for service, metric in snapshot.target_up.items()
            if service != expected_absent and metric.validity != Validity.HEALTHY
        ]
        if unhealthy:
            raise ControllerError(f"Required telemetry targets are not healthy: {unhealthy}")

    def verify_reset_health(self) -> dict[str, Any]:
        snapshot = self.monitoring.snapshot()
        self._healthy_preflight(snapshot)
        return snapshot.to_dict()

    def _heartbeat(self, fault_id: str, generation: int) -> None:
        interval = float(self.policy["timing"]["watchdogRenewSeconds"])
        while not self.heartbeat_stop.wait(interval):
            try:
                self.actuator.call("renew", faultId=fault_id, generation=generation)
            except Exception as error:
                self.heartbeat_error = str(error)
                return

    def run(self, profile_name: str) -> dict[str, Any]:
        profile = self.policy["profiles"].get(profile_name)
        if not profile:
            raise ControllerError(f"Unknown policy profile: {profile_name}")
        experiment_id = str(uuid.uuid4())
        target = profile["target"]
        baseline = load_baseline(self.policy)
        started_wall = time.time()
        fault_id: str | None = None
        generation: int | None = None
        reason = ReasonCode.NORMAL_COMPLETION
        cleanup_verified = False
        recovery_confirmed = False
        fault_activated_at: float | None = None
        abort_requested_at: float | None = None
        fault_removed_at: float | None = None
        first_healthy_at: float | None = None
        samples: list[dict[str, Any]] = []
        began = False
        failure_terminal: Lifecycle | None = None

        previous_sigint = signal.getsignal(signal.SIGINT)
        previous_sigterm = signal.getsignal(signal.SIGTERM)

        def request_cancel(_signum: int, _frame: Any) -> None:
            self.cancel_reason = ReasonCode.MANUAL_KILL

        signal.signal(signal.SIGINT, request_cancel)
        signal.signal(signal.SIGTERM, request_cancel)
        try:
            begin = self.actuator.call(
                "begin_experiment",
                experimentId=experiment_id,
                profile=profile_name,
                target=target,
                policyVersion=self.policy["policyVersion"],
            )
            generation = int(begin["generation"])
            began = True
            snapshot = self.monitoring.snapshot()
            self._healthy_preflight(snapshot)
            samples.append({"phase": "preflight", **snapshot.to_dict()})
            self._transition(
                experiment_id,
                Lifecycle.BASELINING,
                target=target,
                details={"baselineReference": baseline.reference},
                baseline=baseline.to_dict(),
            )
            self._transition(experiment_id, Lifecycle.ARMED, target=target)

            prepared = self.actuator.call(
                "prepare",
                spec={
                    "experiment_id": experiment_id,
                    "target_service": target,
                    "fault_type": profile["faultType"],
                    "duration_seconds": int(profile["durationSeconds"]),
                    "generation": generation,
                },
            )
            fault_id = prepared["faultId"]
            self._healthy_preflight(self.monitoring.snapshot())
            self.actuator.call("apply", faultId=fault_id, generation=generation)
            fault_activated_at = self.clock()
            self._transition(
                experiment_id,
                Lifecycle.RUNNING,
                target=target,
                details={"faultId": fault_id, "containerId": prepared["containerId"]},
            )

            heartbeat = threading.Thread(
                target=self._heartbeat, args=(fault_id, generation), daemon=True
            )
            heartbeat.start()
            evaluator = SafetyPolicyEvaluator(
                self.policy,
                self.clock,
                require_traffic=bool(profile.get("requireAdequateTraffic", True)),
            )
            interval = float(self.policy["timing"]["evaluationIntervalSeconds"])
            duration = float(profile["durationSeconds"])
            while self.clock() - fault_activated_at < duration:
                if self.cancel_reason:
                    reason = self.cancel_reason
                    abort_requested_at = self.clock()
                    break
                inspection = self.actuator.call("inspect", faultId=fault_id)
                if not inspection["evidence"].get("containerExists"):
                    reason = ReasonCode.UNEXPECTED_CONTAINER_EVENT
                    abort_requested_at = self.clock()
                    break
                if inspection["cleanupVerified"]:
                    system = self.actuator.call("status")["system"]
                    kill_reason = system.get("kill_reason")
                    reason = (ReasonCode(kill_reason) if kill_reason in ReasonCode._value2member_map_
                              else ReasonCode.MAX_DURATION if not system["kill_latched"]
                              else ReasonCode.MANUAL_KILL)
                    abort_requested_at = self.clock()
                    break
                if self.heartbeat_error:
                    reason = ReasonCode.WATCHDOG_EXPIRED
                    abort_requested_at = self.clock()
                    break
                snapshot = self.monitoring.snapshot()
                samples.append({"phase": "fault", **snapshot.to_dict()})
                decision = evaluator.evaluate(
                    snapshot,
                    baseline,
                    expected_absent_target=(
                        target if profile.get("expectedTargetTelemetryAbsence") else None
                    ),
                )
                if decision:
                    reason = decision.reason
                    abort_requested_at = self.clock()
                    break
                self.sleeper(min(interval, max(0, duration - (self.clock() - fault_activated_at))))

            if abort_requested_at is None:
                abort_requested_at = self.clock()
                reason = ReasonCode.MAX_DURATION
            self._transition(
                experiment_id,
                Lifecycle.CLEANING_UP,
                reason=reason,
                target=target,
                details={"faultId": fault_id},
            )
            self.heartbeat_stop.set()
            rollback = self.actuator.call("revoke", faultId=fault_id, reason=reason.value)
            cleanup_verified = bool(rollback["cleanupVerified"] and not rollback["evidence"].get("paused"))
            fault_removed_at = self.clock()
            if not cleanup_verified:
                failure_terminal = Lifecycle.CLEANUP_FAILED
                self._transition(
                    experiment_id,
                    Lifecycle.CLEANUP_FAILED,
                    reason=ReasonCode.CLEANUP_FAILED,
                    target=target,
                    outcome="aborted",
                    cleanupStatus="failed",
                    recoveryStatus="not_started",
                )
                raise ControllerError("Fault removal could not be verified")

            self._transition(
                experiment_id,
                Lifecycle.RECOVERING,
                reason=reason,
                target=target,
                cleanupStatus="verified",
            )
            recovery_timeout = float(self.policy["timing"]["recoveryTimeoutSeconds"])
            recovery_hold = float(
                profile.get(
                    "recoveryHoldSeconds",
                    self.policy["timing"]["healthyRecoveryHoldSeconds"],
                )
            )
            recovery_deadline = self.clock() + recovery_timeout
            healthy_since: float | None = None
            while self.clock() < recovery_deadline:
                try:
                    snapshot = self.monitoring.snapshot()
                    self._healthy_preflight(snapshot)
                    inspection = self.actuator.call("inspect", faultId=fault_id)
                    healthy = not inspection["evidence"].get("paused", True)
                except Exception:
                    healthy = False
                if healthy:
                    samples.append({"phase": "recovery", **snapshot.to_dict()})
                    if healthy_since is None:
                        healthy_since = self.clock()
                        first_healthy_at = healthy_since
                    if self.clock() - healthy_since >= recovery_hold:
                        recovery_confirmed = True
                        break
                else:
                    healthy_since = None
                self.sleeper(1)

            if not recovery_confirmed:
                failure_terminal = Lifecycle.RECOVERY_FAILED
                self._transition(
                    experiment_id,
                    Lifecycle.RECOVERY_FAILED,
                    reason=ReasonCode.RECOVERY_TIMEOUT,
                    target=target,
                    outcome="aborted",
                    cleanupStatus="verified",
                    recoveryStatus="failed",
                )
                raise ControllerError("Recovery confirmation timed out")

            terminal = Lifecycle.COMPLETED if reason == ReasonCode.MAX_DURATION else Lifecycle.ABORTED
            self._transition(
                experiment_id,
                terminal,
                reason=reason,
                target=target,
                outcome="completed" if terminal == Lifecycle.COMPLETED else "aborted",
                cleanupStatus="verified",
                recoveryStatus="confirmed",
                details={"healthyHoldSeconds": recovery_hold},
            )
            return {
                "experimentId": experiment_id,
                "profile": profile_name,
                "faultId": fault_id,
                "state": terminal.value,
                "reasonCode": reason.value,
                "cleanupStatus": "verified",
                "recoveryStatus": "confirmed",
                "durationsSeconds": {
                    "faultToDetection": round(abort_requested_at - fault_activated_at, 3),
                    "abortToFaultRemoval": round(fault_removed_at - abort_requested_at, 3),
                    "faultRemovalToFirstHealthy": round(first_healthy_at - fault_removed_at, 3),
                    "faultRemovalToConfirmedRecovery": round(self.clock() - fault_removed_at, 3),
                    "faultToConfirmedRecovery": round(self.clock() - fault_activated_at, 3),
                },
                "baselineReference": baseline.reference,
                "policyVersion": self.policy["policyVersion"],
                "startedAtEpoch": started_wall,
                "target": target,
                "samples": samples,
            }
        except Exception as error:
            self.heartbeat_stop.set()
            if fault_id and not cleanup_verified:
                try:
                    cleanup = self.actuator.call(
                        "revoke",
                        faultId=fault_id,
                        reason=(self.cancel_reason or ReasonCode.CONTROLLER_CANCELLED).value,
                    )
                    cleanup_verified = bool(cleanup.get("cleanupVerified"))
                except Exception:
                    try:
                        self.actuator.call(
                            "kill",
                            reason=ReasonCode.CLEANUP_FAILED.value,
                        )
                    except Exception:
                        pass
            if began and failure_terminal is None:
                try:
                    self._transition(
                        experiment_id, Lifecycle.ABORTED if not fault_id or cleanup_verified else Lifecycle.CLEANUP_FAILED,
                        reason=self.cancel_reason or ReasonCode.CONTROLLER_CANCELLED,
                        target=target, outcome="aborted",
                        cleanupStatus="verified" if not fault_id or cleanup_verified else "failed",
                        recoveryStatus="confirmed" if recovery_confirmed else "not_confirmed",
                        details={"error": str(error)},
                    )
                except ActuatorClientError:
                    pass
            raise
        finally:
            signal.signal(signal.SIGINT, previous_sigint)
            signal.signal(signal.SIGTERM, previous_sigterm)

