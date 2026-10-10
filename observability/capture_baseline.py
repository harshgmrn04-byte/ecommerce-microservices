"""Run the real k6 workload and capture measured service counts and health."""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from chaos.run_experiment import (APPLICATION_JOBS, application_up, compose,
                                  firing_safety_alerts, mailhog_total,
                                  prometheus_query, rabbitmq_state)
from safety.configuration import ROOT, load_policy
from safety.controller import load_baseline


def query_map(query: str) -> dict[str, float]:
    return {row["metric"].get("service", row["metric"].get("job", "all")): float(row["value"][1])
            for row in prometheus_query(query) if math.isfinite(float(row["value"][1]))}


def containers() -> dict:
    ids = compose("ps", "-q").stdout.split()
    result = subprocess.run(["docker", "inspect", *ids], cwd=ROOT, check=True,
                            capture_output=True, text=True)
    return {item["Config"]["Labels"]["com.docker.compose.service"]:
            {"id": item["Id"], "restartCount": item["RestartCount"]}
            for item in json.loads(result.stdout)}


def capture(output: Path, duration: str = "3m") -> dict:
    if any(value != 1 for value in application_up().values()):
        raise RuntimeError("All application services must be up before baseline capture")
    before = containers()
    before_mail = mailhog_total()
    started = time.time()
    raw_path = ROOT / "observability/results/baseline-observations.json"
    observations = []
    workload = subprocess.Popen(
        ["docker", "compose", "--profile", "tools", "run", "--rm", "-e", f"DURATION={duration}", "k6"],
        cwd=ROOT,
    )
    try:
        while workload.poll() is None:
            observations.append({"observedAt": time.time(), "up": application_up(),
                                 "rabbitmq": rabbitmq_state(), "alerts": firing_safety_alerts()})
            time.sleep(5)
        if workload.returncode:
            raise RuntimeError(f"k6 workload failed with exit code {workload.returncode}")
        # Include the final workload requests in the next Prometheus scrape.
        time.sleep(6)
        elapsed = math.ceil(time.time() - started)
        counts = query_map(f"sum by (service) (increase(http_requests_total[{elapsed}s]))")
        p95 = query_map("histogram_quantile(0.95, sum by (service, le) "
                        f"(rate(http_request_duration_seconds_bucket[{elapsed}s]))) * 1000")
        after = containers()
        summary_path = ROOT / "observability/results/k6-summary.json"
        summary = json.loads(summary_path.read_text())
        metrics = summary["metrics"]
        latency = metrics["http_req_duration"]
        checks = metrics["checks"]
        failed = metrics["http_req_failed"]
        checkout = metrics["checkout_success"]
        end_up = application_up()
        end_queue = rabbitmq_state()
        restarts = {name: after[name]["restartCount"] - before[name]["restartCount"]
                    if after[name]["id"] == before[name]["id"] else 1
                    for name in [*APPLICATION_JOBS, "frontend"]}
        artifact = {
            "capturedAt": datetime.now(timezone.utc).isoformat(),
            "environment": "local-docker-compose-sandbox", "status": "passed",
            "workload": {"duration": duration, "browseVirtualUsers": 5, "checkoutsPerMinute": 3,
                         "iterations": metrics["iterations"]["count"],
                         "httpRequests": metrics["http_reqs"]["count"],
                         "requestRatePerSecond": metrics["http_reqs"]["rate"],
                         "serviceRequestCounts": {name: int(value) for name, value in counts.items()},
                         "countMethod": "Prometheus increase over capture interval; includes concurrent local probes"},
            "availability": {"applicationTargetsUp": sum(end_up.values()),
                             "applicationTargetsExpected": len(APPLICATION_JOBS),
                             "checksPassed": checks["passes"], "checksFailed": checks["fails"],
                             "checkSuccessRate": checks["value"], "containerRestarts": restarts},
            "http": {"failedRequests": failed["passes"], "failureRate": failed["value"],
                     "latencyMilliseconds": {"average": latency["avg"], "median": latency["med"],
                                             "p90": latency["p(90)"], "p95": latency["p(95)"],
                                             "maximum": latency["max"]},
                     "serviceP95Milliseconds": p95},
            "businessFlow": {"checkoutAttempts": checkout["passes"] + checkout["fails"],
                             "checkoutSuccesses": checkout["passes"], "checkoutSuccessRate": checkout["value"],
                             "mailhogMessagesDuringBaseline": mailhog_total() - before_mail},
            "rabbitmq": {"consumers": end_queue["consumers"], "messagesReadyAtEnd": end_queue["messagesReady"],
                         "maximumMessagesReady": max(item["rabbitmq"]["messagesReady"] for item in observations)},
            "alerts": {"firingAtEnd": len(firing_safety_alerts())},
            "rawK6Summary": str(summary_path.relative_to(ROOT)),
            "rawObservations": str(raw_path.relative_to(ROOT)),
        }
        healthy = (all(value == 1 for value in end_up.values()) and not any(restarts.values())
                   and checks["fails"] == 0 and failed["value"] < .01 and checkout["value"] == 1
                   and not end_queue["messagesReady"] and end_queue["consumers"] == 1
                   and artifact["alerts"]["firingAtEnd"] == 0
                   and all(all(value == 1 for value in item["up"].values()) and not item["alerts"]
                           for item in observations))
        artifact["status"] = "passed" if healthy else "failed"
        raw_path.write_text(json.dumps({"startedAtEpoch": started, "containersBefore": before,
                                       "containersAfter": after, "samples": observations}, indent=2) + "\n")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(artifact, indent=2, allow_nan=False) + "\n")
        policy = load_policy()
        policy["admission"]["baselineArtifact"] = str(output)
        load_baseline(policy)
        return artifact
    finally:
        if workload.poll() is None:
            workload.terminate()
            workload.wait(timeout=15)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", default="3m")
    parser.add_argument("--output", type=Path, default=ROOT / "observability/baseline.json")
    args = parser.parse_args()
    artifact = capture(args.output, args.duration)
    print(json.dumps({"status": artifact["status"], "requests": artifact["workload"]["httpRequests"],
                      "serviceRequestCounts": artifact["workload"]["serviceRequestCounts"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
