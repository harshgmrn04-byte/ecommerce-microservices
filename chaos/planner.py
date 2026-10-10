"""Explainable experiment ranking; plans never execute faults or alter policy."""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from safety.configuration import ROOT, load_policy, validate_policy
from safety.controller import ControllerError, load_baseline


def learn_impact(history: list[dict[str, Any]], profile: str, version: str) -> dict[str, Any]:
    """Beta-Bernoulli learner: update only from observed, real experiment outcomes."""
    failures = successes = 0
    for result in history:
        if (result.get("profile") != profile or result.get("policyVersion") != version
                or result.get("runKind", "experiment") != "experiment"
                or not result.get("samples")):
            continue
        if result.get("reasonCode") in {"ERROR_RATE", "LATENCY", "CRITICAL_PROBE_FAILED", "RECOVERY_TIMEOUT"}:
            failures += 1
        elif (result.get("state") == "COMPLETED" and result.get("cleanupStatus") == "verified"
              and result.get("recoveryStatus") == "confirmed"):
            successes += 1
    alpha, beta = 1 + failures, 1 + successes
    mean = alpha / (alpha + beta)
    deviation = math.sqrt(alpha * beta / ((alpha + beta) ** 2 * (alpha + beta + 1)))
    return {"model": "Beta-Bernoulli adverse-impact learner with Beta(1,1) prior",
            "observedExperiments": successes + failures, "adverseOutcomes": failures,
            "recoveredOutcomes": successes, "alpha": alpha, "beta": beta,
            "posteriorMean": round(mean, 5), "posteriorStdDev": round(deviation, 5),
            "explorationWeight": round(mean + 2 * deviation, 5),
            "interpretation": "Planning estimate with limited evidence; not a calibrated production failure probability"}


def upstream_services(graph: dict[str, list[str]], target: str) -> list[str]:
    affected = {target}
    while True:
        callers = {service for service, dependencies in graph.items()
                   if affected.intersection(dependencies)}
        expanded = affected | callers
        if expanded == affected:
            return sorted(affected - {target})
        affected = expanded


def build_plan(policy: dict[str, Any], graph: dict[str, list[str]],
               history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    validate_policy(policy)
    history = history or []
    candidates = []
    for name, profile in policy["profiles"].items():
        target = profile["target"]
        if target not in graph:
            raise ValueError(f"Dependency map is missing policy target {target}")
        callers = upstream_services(graph, target)
        dependencies = graph[target]
        criticality = 1 + len(callers) + (2 if target in {"frontend", "order-service", "auth-service"} else 0)
        # Dependency count is a transparent proxy, not a measured failure probability.
        fragility = 1 + len(dependencies) / 4
        cost = (1 + len(callers)) * profile["durationSeconds"] / 10
        learning = learn_impact(history, name, policy["policyVersion"])
        risk = round(criticality * fragility * learning["explorationWeight"] / cost, 4)
        smaller = [other_name for other_name, other in policy["profiles"].items()
                   if other["target"] == target and other["faultType"] == profile["faultType"]
                   and other["durationSeconds"] < profile["durationSeconds"]]
        latest = {profile_name: next((item for item in reversed(history)
                                      if item.get("profile") == profile_name
                                      and item.get("runKind", "experiment") == "experiment"
                                      and item.get("policyVersion") == policy["policyVersion"]), None)
                  for profile_name in [name, *smaller]}
        def succeeded(result):
            return bool(result and result.get("state") == "COMPLETED"
                        and result.get("cleanupStatus") == "verified"
                        and result.get("recoveryStatus") == "confirmed")
        blockers = []
        if latest[name] and not succeeded(latest[name]):
            blockers.append("Latest result failed or aborted; investigate before retrying")
        for prerequisite in sorted(smaller):
            if not succeeded(latest[prerequisite]):
                blockers.append(f"Requires a completed, cleaned up, recovered run of {prerequisite}")
        protected = policy["monitoring"]["protectedHttpServices"]
        candidates.append({
            "profile": name, "target": target, "faultType": profile["faultType"],
            "durationSeconds": profile["durationSeconds"], "score": risk,
            "scoreComponents": {"criticality": criticality, "fragilityProxy": fragility, "cost": cost},
            "learning": learning,
            "upstreamServices": callers, "dependencies": dependencies,
            "eligible": not blockers, "blockers": blockers,
            "hypothesis": (
                f"Pausing {target} for {profile['durationSeconds']} seconds affects only that container. "
                f"Critical probes stay healthy and protected HTTP services ({', '.join(protected)}) "
                f"remain below {policy['abort']['maximumErrorFraction'] * 100:g}% errors and the "
                "policy's baseline-relative/absolute p95 limit. Cleanup is verified and "
                "health is sustained for the configured recovery hold."
            ),
            "abortThresholds": dict(policy["abort"]),
        })
    candidates.sort(key=lambda item: (not item["eligible"], -item["score"], item["profile"]))
    return {
        "schemaVersion": 1, "policyVersion": policy["policyVersion"],
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "strategy": "criticality * dependency-fragility proxy * learned impact/uncertainty / blast-radius-duration cost",
        "executionMode": "proposal-only", "maximumConcurrentTargets": 1,
        "historyOrder": "oldest-to-newest; only current policy version counts",
        "experiments": candidates,
        "coverageLimits": ["Only existing policy profiles are candidates; no new faults or targets",
                           "Graph is a configured dependency map, not measured runtime propagation",
                           "No live safety acceptance is implied by a plan",
                           "Safety drills, manual cancellation, and controller crashes do not train the impact model"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, help="JSON array of controller results, oldest first")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    policy = load_policy()
    graph = json.loads((ROOT / "safety/config/dependency-map.json").read_text(encoding="utf-8"))["services"]
    history = json.loads(args.history.read_text(encoding="utf-8")) if args.history else []
    if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
        parser.error("history must be a JSON array of controller results")
    plan = build_plan(policy, graph, history)
    try:
        baseline = load_baseline(policy)
        plan["baselineAdmission"] = {"status": "passed", "reference": baseline.reference,
                                     "livePreflightRequired": True}
    except (ControllerError, KeyError, ValueError, OSError) as error:
        plan["baselineAdmission"] = {"status": "blocked", "reason": str(error)}
    output = json.dumps(plan, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
