"""Live Day 1 kill-switch acceptance using a separate experiment process."""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from .run_experiment import ROOT, KILL_SWITCH, utc_timestamp

RESULT = ROOT / "chaos/results/manual-kill-experiment.json"
REPORT = ROOT / "chaos/results/manual-kill-acceptance.json"


def verify(result: dict, exit_code: int, requested: bool) -> dict[str, bool]:
    return {
        "externalKillSwitchRequested": requested,
        "runnerExitedSuccessfully": exit_code == 0,
        "liveRunPassed": result.get("status") == "passed" and result.get("dryRun") is False,
        "manualKillHandled": result.get("termination") == "kill_switch",
        "faultAndRecoveryAssertionsPassed": bool(result.get("assertions"))
            and all(result["assertions"].get(key) is True for key in (
                "faultObservedByPrometheus", "checkoutAvailableDuringFault",
                "rabbitMqBufferedNotification", "automaticRollbackExecuted", "recoveryWithinDeadline")),
        "allTargetsRecovered": len(result.get("after", {}).get("applicationTargets", {})) == 4
            and all(value == 1 for value in result.get("after", {}).get("applicationTargets", {}).values()),
        "queueDrained": result.get("after", {}).get("rabbitmq", {}).get("messagesReady") == 0,
    }


def main() -> int:
    if KILL_SWITCH.exists():
        print("Refusing acceptance: kill switch is already engaged", file=sys.stderr)
        return 1
    # A unique run marker prevents stale evidence from satisfying this test.
    started = utc_timestamp()
    requested = False
    request_time = None
    process = subprocess.Popen([sys.executable, str(ROOT / "chaos/run_experiment.py"),
                                "--result", str(RESULT)], cwd=ROOT,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    messages = queue.Queue()
    def reader():
        for line in process.stdout:
            messages.put(line)
        messages.put(None)
    threading.Thread(target=reader, daemon=True).start()
    deadline = time.monotonic() + 180
    timed_out = False
    try:
        while time.monotonic() < deadline:
            try:
                line = messages.get(timeout=1)
            except queue.Empty:
                continue
            if line is None:
                break
            print(line, end="", flush=True)
            if "Fault evidence captured: target down and notification buffered" in line and not requested:
                KILL_SWITCH.touch(exist_ok=False)
                requested = True
                request_time = utc_timestamp()
                print("Acceptance operator: file kill switch engaged", flush=True)
        if process.poll() is None:
            timed_out = True
            KILL_SWITCH.touch(exist_ok=True)
            requested = True
            try:
                process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    # Do not force-kill a process that owns rollback.
                    print("Runner did not exit; kill switch remains engaged. Inspect Docker.", file=sys.stderr)
                    return 1
        try:
            result = json.loads(RESULT.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result = {}
        assertions = verify(result, process.returncode, requested)
        assertions["freshRunEvidence"] = result.get("startedAt", "") >= started
        assertions["completedBeforeAcceptanceTimeout"] = not timed_out
        passed = all(assertions.values())
        report = {"status": "passed" if passed else "failed", "startedAt": started,
                  "finishedAt": utc_timestamp(), "killRequestedAt": request_time,
                  "environment": "live-docker-compose-sandbox", "assertions": assertions,
                  "experimentResult": str(RESULT.relative_to(ROOT)),
                  "recoverySeconds": result.get("recoverySeconds")}
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        # Leave admission latched if cleanup/recovery is not proven.
        if passed:
            KILL_SWITCH.unlink(missing_ok=True)
        print(json.dumps(report, indent=2))
        return 0 if passed else 1
    except BaseException:
        KILL_SWITCH.touch(exist_ok=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
