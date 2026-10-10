import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chaos import run_experiment as runner
from chaos.accept_manual_kill import verify


class ManualKillTests(unittest.TestCase):
    def test_guardrail_completion_does_not_count_as_manual_kill(self):
        result = {"status": "passed", "dryRun": False, "termination": "guardrail_triggered"}
        self.assertFalse(verify(result, 0, True)["manualKillHandled"])
        self.assertFalse(verify(result, 0, True)["faultAndRecoveryAssertionsPassed"])

    def test_dry_run_and_failed_exit_cannot_pass_acceptance(self):
        result = {"status": "passed", "dryRun": True, "termination": "kill_switch"}
        self.assertFalse(verify(result, 1, True)["liveRunPassed"])
        self.assertFalse(verify(result, 1, True)["runnerExitedSuccessfully"])

    def test_external_file_kill_drives_rollback_and_retains_evidence(self):
        experiment = json.loads(runner.DEFAULT_EXPERIMENT.read_text())
        sample = {"applicationTargets": {"notification-service": 0},
                  "rabbitmq": {"messagesReady": 1}, "maximumP95LatencyMilliseconds": 5}
        recovered = {"applicationTargets": {name: 1 for name in runner.APPLICATION_JOBS},
                     "rabbitmq": {"messagesReady": 0}, "mailhogMessages": 2}
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            switch = root / "KILL_SWITCH"
            def compose(*arguments, **kwargs):
                calls.append(arguments)
                return type("Result", (), {"stdout": ""})()
            def log(message):
                if message.startswith("Fault evidence captured:"):
                    switch.touch()
            with patch.object(runner, "KILL_SWITCH", switch), \
                 patch.object(runner, "LOCK_FILE", root / "lock"), \
                 patch.object(runner, "preflight", return_value={"mailhogMessages": 1}), \
                 patch.object(runner, "prepare_canary", return_value={}), \
                 patch.object(runner, "create_canary_order", return_value={"httpStatus": 201, "orderId": "test"}), \
                 patch.object(runner, "sample_guardrails", return_value=copy.deepcopy(sample)), \
                 patch.object(runner, "wait_for_recovery", return_value=recovered), \
                 patch.object(runner, "compose", side_effect=compose), \
                 patch.object(runner, "log", side_effect=log), \
                 patch.object(runner.time, "sleep"):
                self.assertEqual(runner.run_experiment(experiment, dry_run=False,
                                                      result_path=root / "result.json"), 0)
            result = json.loads((root / "result.json").read_text())
            self.assertEqual(result["termination"], "kill_switch")
            self.assertIn(("start", "notification-service"), calls)
            self.assertTrue(all(verify(result, 0, True).values()))
            self.assertTrue(switch.exists())  # runner does not silently reset the switch


if __name__ == "__main__":
    unittest.main()
