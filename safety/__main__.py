"""Operate the local safety controller and its independent watchdog."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .client import ActuatorClient
from .configuration import ROOT, load_policy
from .controller import SafetyController
from .monitoring import PrometheusMonitoringAdapter


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["run", "status", "kill", "reset", "reconcile", "shutdown"])
    parser.add_argument("--profile", default="notification-pause-acceptance")
    parser.add_argument("--result", type=Path, default=ROOT / "chaos/results/last-controller.json")
    parser.add_argument("--policy", type=Path)
    args = parser.parse_args()
    policy = load_policy(args.policy) if args.policy else load_policy()
    client = ActuatorClient()
    controller = SafetyController(policy, PrometheusMonitoringAdapter(policy), client)
    try:
        if args.action == "run":
            result = controller.run(args.profile)
            args.result.parent.mkdir(parents=True, exist_ok=True)
            args.result.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
            print(json.dumps({key: value for key, value in result.items() if key != "samples"}))
            return 0 if result["state"] == "COMPLETED" else 2
        if args.action == "reset":
            controller.verify_reset_health()
            result = client.call("reset", healthVerified=True)
        else:
            result = client.call(args.action)
        print(json.dumps(result, indent=2))
        return 0
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
