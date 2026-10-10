"""Generate judge-facing findings, scenario scores, and a local evidence dashboard."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from chaos.planner import build_plan
from safety.configuration import ROOT, load_policy
from safety.controller import ControllerError, load_baseline


RESULTS = ROOT / "chaos/results"


def scenario_score(case: dict, controller: dict, recovery_limit: float) -> dict:
    checks = case.get("assertions", {})
    weights = {"checkoutAvailableDuringFault": 40, "canaryEmailDelivered": 25,
               "targetUnpausedAfterRollback": 20}
    components = {name: weight if checks.get(name) else 0 for name, weight in weights.items()}
    recovery = controller.get("durationsSeconds", {}).get("faultRemovalToConfirmedRecovery")
    components["recoveryWithinDeadline"] = 15 if recovery is not None and recovery <= recovery_limit else 0
    return {"score": sum(components.values()), "maximum": 100, "components": components,
            "recoverySeconds": recovery, "scope": "Observed notification-pause checkout/delivery scenario only"}


def generate_report() -> dict:
    policy = load_policy()
    baseline = json.loads((ROOT / "observability/baseline.json").read_text())
    topology = json.loads((ROOT / "safety/config/dependency-map.json").read_text())
    suite = json.loads((RESULTS / "safety-acceptance.json").read_text())
    history = json.loads((RESULTS / "history.json").read_text())
    completed = next(case for case in suite["cases"] if case["name"] == "completed")
    controller = json.loads((ROOT / completed["controllerResult"]).read_text())
    plan = build_plan(policy, topology["services"], history)
    try:
        admitted = load_baseline(policy)
        plan["baselineAdmission"] = {"status": "passed", "reference": admitted.reference,
                                     "livePreflightRequired": True}
    except ControllerError as error:
        plan["baselineAdmission"] = {"status": "blocked", "reason": str(error)}
    (RESULTS / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    score = scenario_score(completed, controller, policy["timing"]["recoveryTimeoutSeconds"])
    cases = suite["cases"]
    passed_cases = sum(case["status"] == "passed" for case in cases)
    required_cases = {"completed", "latency-threshold", "manual-kill", "controller-sigterm",
                      "controller-crash", "lease-expiry", "actuator-restart"}
    safety_passed = (suite["status"] == "passed" and passed_cases == len(cases)
                     and required_cases <= {case["name"] for case in cases})
    findings = [
        {"rank": 1, "title": "Single notification consumer delays all delivery during its outage",
         "basis": "observed", "evidence": "pause-completed.json and safety-acceptance.json",
         "recommendation": "Run redundant consumers, alert on oldest message age, and bound consumer prefetch.",
         "severity": "medium"},
        {"rank": 2, "title": "Order storage and event publication lack an atomic outbox",
         "basis": "source review", "evidence": "order-service/src/controllers/orderController.ts; order-service/src/utils/rabbitmq.ts",
         "recommendation": "Persist an outbox row with the order, relay with publisher confirms, and make delivery idempotent.",
         "severity": "high", "limitation": "Broker failure was not injected; event loss is a source-based risk."},
        {"rank": 3, "title": "Failed notification messages can be requeued indefinitely",
         "basis": "source review", "evidence": "notification-service/src/consumers/orderConsumer.ts",
         "recommendation": "Use bounded retries and a dead-letter queue; deduplicate by order ID and event type.",
         "severity": "medium", "limitation": "SMTP failure and poison-message load were not injected."},
    ]
    matrix = [
        {"deliverable": "Public, free open-source sample application", "status": "complete",
         "evidence": "Public upstream repository and local MIT LICENSE"},
        {"deliverable": "Containerized application running end to end locally", "status": "complete" if baseline["status"] == "passed" else "failed",
         "evidence": "baseline.json: browse, login, checkout, local email"},
        {"deliverable": "Service, dependency and data-flow mapping", "status": "complete",
         "evidence": "dependency-map.json and docs/application-map.md; generated from Compose/source"},
        {"deliverable": "Steady-load error, latency and health baseline", "status": "complete" if plan["baselineAdmission"]["status"] == "passed" else "refresh required",
         "evidence": "baseline.json, k6-summary.json, baseline-observations.json"},
        {"deliverable": "Intelligent experiment choice and explanation", "status": "complete",
         "evidence": "plan.json: graph scoring, Bayesian outcome learning, uncertainty, escalation gates"},
        {"deliverable": "At least one controlled failure observed", "status": "complete" if completed["status"] == "passed" else "failed",
         "evidence": "pause-completed.json and direct Docker/RabbitMQ observations"},
        {"deliverable": "Single-target blast radius and bounded duration", "status": "complete" if safety_passed else "failed",
         "evidence": "Policy allowlist, project identity, lease, absolute deadline, safety-journal.json"},
        {"deliverable": "Automatic threshold stop and rollback tested", "status": "complete" if any(c["name"] == "latency-threshold" and c["status"] == "passed" for c in cases) else "failed",
         "evidence": "Real p95 exceeds a stricter 1 ms test ceiling; legacy outage-limit rollback also retained"},
        {"deliverable": "Working manual kill switch tested", "status": "complete" if any(c["name"] == "manual-kill" and c["status"] == "passed" for c in cases) else "failed",
         "evidence": "pause-manual-kill.json; persistent latch and verified unpause"},
        {"deliverable": "Capture errors, slowdowns and recovery time", "status": "complete",
         "evidence": "Controller phase samples: per-service errors/p95, probes, timestamps and recovery durations"},
        {"deliverable": "Analysis, ranked fixes and resilience score", "status": "complete",
         "evidence": "report.json, report.html and docs/resilience-report.md; scoped score and coverage limits"},
        {"deliverable": "Repository links, deployment steps and Day 1 demo", "status": "complete",
         "evidence": "docs/hackathon-submission.md and README.md"},
    ]
    report = {"generatedAt": datetime.now(timezone.utc).isoformat(),
              "title": "AI-driven chaos engineering: hackathon deliverable audit",
              "status": "complete" if all(item["status"] == "complete" for item in matrix) else "attention required",
              "baseline": baseline, "topology": topology, "plan": plan,
              "safety": {"passedCases": passed_cases, "totalCases": len(cases), "cases": cases},
              "scenarioScore": score, "findings": findings, "deliverables": matrix,
              "coverage": {"testedApplicationTargets": ["notification-service"],
                           "applicationTargets": sorted(topology["services"]),
                           "faultTypes": ["container-pause", "legacy container-stop"],
                           "limitation": "This score covers one failure scenario. Other services, network, disk, database and traffic-spike faults remain untested; examples in the brief are optional."}}
    (RESULTS / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    write_markdown(report)
    template = (ROOT / "chaos/dashboard.html").read_text()
    embedded = json.dumps(report, allow_nan=False).replace("<", "\\u003c")
    (RESULTS / "report.html").write_text(template.replace("__REPORT_DATA__", embedded))
    return report


def write_markdown(report: dict) -> None:
    baseline = report["baseline"]
    lines = ["# Hackathon deliverable audit and resilience report", "", f"Generated: {report['generatedAt']}", "",
             f"Audit status: **{report['status']}**. The supplied screenshots specify five solution capabilities",
             "and Day 1 guidance checkpoints; they do not require implementing every example fault type.", "",
             "| Deliverable | Status | Evidence |", "| --- | --- | --- |"]
    lines += [f"| {item['deliverable']} | {item['status']} | {item['evidence']} |" for item in report["deliverables"]]
    lines += ["", "## Observed results", "",
              f"- Baseline: {baseline['workload']['httpRequests']} requests, {baseline['http']['failureRate'] * 100:g}% HTTP failures, p95 {baseline['http']['latencyMilliseconds']['p95']:.2f} ms.",
              f"- Checkout: {baseline['businessFlow']['checkoutSuccesses']}/{baseline['businessFlow']['checkoutAttempts']} successful under normal load.",
              f"- Live safety acceptance: {report['safety']['passedCases']}/{report['safety']['totalCases']} checks passed.",
              f"- Normal pause recovery confirmed {report['scenarioScore']['recoverySeconds']:.3f} seconds after fault removal.",
              f"- Tested-scenario score: {report['scenarioScore']['score']}/100.", "",
              "Score weights: checkout availability 40, buffered email delivery 25, verified fault removal 20,",
              "and confirmed recovery within the policy deadline 15. This is an explicit engineering rubric,",
              "not a probability, service-wide SLA, or system-wide resilience score.", "",
              "| Safety case | Result | Total test time |", "| --- | --- | --- |"]
    lines += [f"| {case['name']} | {case['status']} | {case.get('totalSeconds', 0):.3f}s |" for case in report["safety"]["cases"]]
    lines += ["", "The latency drill uses a **stricter 1 ms ceiling held for 3 seconds** to exercise",
              "threshold rollback against real Prometheus measurements. It does not claim the application",
              "breached the normal 2,000 ms ceiling. Crash/restart drills verify actual Docker unpause,",
              "healthy protected traffic, queue drain and the specific canary email.", "",
              "## Ranked weaknesses and fixes", ""]
    lines += ["Ranking puts the observed delivery weakness first, followed by source-review risks",
              "in severity order. Untested source risks are not claimed as measured failures.", ""]
    for finding in report["findings"]:
        lines += [f"{finding['rank']}. **{finding['title']}** ({finding['severity']}; {finding['basis']}).",
                  f"   Recommendation: {finding['recommendation']}", f"   Evidence: `{finding['evidence']}`."]
        if finding.get("limitation"):
            lines.append(f"   Limit: {finding['limitation']}")
    lines += ["", "## Planning model and coverage", "",
              "The local planner combines graph criticality, dependency count, duration cost, and a",
              "Beta-Bernoulli model of adverse outcomes. Beta(1,1) is its explicit prior; each real",
              "completed/recovered run updates the posterior. The posterior mean plus two standard",
              "deviations supplies an exploration weight. Safety drills and cancelled/crashed controllers",
              "are excluded from training. The model works offline and proposes only policy profiles.",
              "The safety controller independently gates every mutation with fresh metrics and probes.", "",
              report["coverage"]["limitation"], "",
              "Original gaps closed: missing measured baseline service counts; unusable actuator ping;",
              "interrupted lifecycle records; graceful-shutdown cleanup; missing live pause/watchdog",
              "acceptance; manual-only topology; heuristic-only planning; and missing final report/demo.", "",
              "Raw evidence is retained in `chaos/results/` and `observability/results/`.",
              "The first failed queue-observation attempt is retained separately; it led to replacing",
              "eventually consistent management counters with direct `rabbitmqctl` observations.", ""]
    (ROOT / "docs/resilience-report.md").write_text("\n".join(lines))


def main() -> int:
    report = generate_report()
    print(json.dumps({"status": report["status"], "scenarioScore": report["scenarioScore"]["score"],
                      "safetyCases": f"{report['safety']['passedCases']}/{report['safety']['totalCases']}"}))
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
