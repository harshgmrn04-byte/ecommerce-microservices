import copy
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from chaos.planner import learn_impact
from safety.actuator import Actuator
from safety.configuration import ROOT, load_policy
from safety.docker_api import DockerApi
from safety.journal import Journal, JournalError


class FakeDocker:
    def __init__(self):
        self.container = {"Id": "sandbox", "State": {"Running": True, "Paused": False},
                          "RestartCount": 0, "Image": "fixture"}

    def resolve_compose_service(self, *_args):
        return copy.deepcopy(self.container)

    def inspect(self, _container_id):
        return copy.deepcopy(self.container)

    def pause(self, _container_id):
        self.container["State"]["Paused"] = True

    def unpause(self, _container_id):
        self.container["State"]["Paused"] = False


class ActuatorRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.directory.cleanup)
        self.journal = Journal(Path(self.directory.name) / "safety.db")
        self.addCleanup(self.journal.connection.close)
        self.docker = FakeDocker()
        self.actuator = Actuator(load_policy(), self.journal, self.docker)
        self.actuator.startup_reconcile()

    def prepare(self, experiment="experiment"):
        generation = self.journal.begin_experiment(experiment, "notification-pause", "notification-service", "fixture")
        return self.actuator.prepare({"experiment_id": experiment, "target_service": "notification-service",
                                     "fault_type": "container-pause", "duration_seconds": 20,
                                     "generation": generation}), generation

    def test_expired_prepared_lease_cannot_activate(self):
        prepared, generation = self.prepare()
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.journal.update_fault(prepared["faultId"], lease_expires_utc=expired)
        with self.assertRaises(JournalError):
            self.actuator.apply(prepared["faultId"], generation)
        self.assertFalse(self.docker.container["State"]["Paused"])
        self.assertTrue(self.journal.fault(prepared["faultId"])["cleanup_verified"])
        self.assertTrue(self.journal.system_state()["kill_latched"])

    def test_restart_unpauses_and_closes_interrupted_controller(self):
        prepared, generation = self.prepare()
        self.actuator.apply(prepared["faultId"], generation)
        self.journal.transition("experiment", "RUNNING")
        self.assertTrue(self.docker.container["State"]["Paused"])
        restarted = Actuator(load_policy(), self.journal, self.docker)
        restarted.startup_reconcile()
        self.assertFalse(self.docker.container["State"]["Paused"])
        self.assertEqual(self.journal.status()["experiments"][0]["state"], "ABORTED")
        self.journal.reset_kill()
        self.journal.begin_experiment("next", "notification-pause", "notification-service", "fixture")

    def test_kill_revokes_generation_and_prevents_reapply(self):
        prepared, generation = self.prepare()
        self.actuator.apply(prepared["faultId"], generation)
        self.actuator.kill_all("MANUAL_KILL")
        self.assertFalse(self.docker.container["State"]["Paused"])
        with self.assertRaises(JournalError):
            self.actuator.apply(prepared["faultId"], generation)
        with self.assertRaises(JournalError):
            self.actuator.renew(prepared["faultId"], generation)

    def test_docker_ping_accepts_actual_plain_text_response(self):
        class Response:
            status = 200
            def read(self):
                return b"OK"
            def getheader(self, *_args):
                return "text/plain; charset=utf-8"
        class Connection:
            def __init__(self, *_args): pass
            def request(self, *_args, **_kwargs): pass
            def getresponse(self): return Response()
            def close(self): pass
        with patch("safety.docker_api.UnixHTTPConnection", Connection):
            DockerApi("/fixture").ping()

    def test_learning_ignores_drills_and_updates_from_real_outcomes(self):
        fixture = {"profile": "p", "policyVersion": "v", "samples": [{"observed": True}],
                   "state": "COMPLETED", "cleanupStatus": "verified", "recoveryStatus": "confirmed"}
        prior = learn_impact([], "p", "v")
        posterior = learn_impact([fixture], "p", "v")
        self.assertLess(posterior["posteriorMean"], prior["posteriorMean"])
        self.assertEqual(posterior["observedExperiments"], 1)
        self.assertEqual(learn_impact([{**fixture, "runKind": "safety-check"}], "p", "v"), prior)
        adverse = {**fixture, "state": "ABORTED", "reasonCode": "LATENCY"}
        self.assertGreater(learn_impact([adverse], "p", "v")["posteriorMean"], prior["posteriorMean"])


if __name__ == "__main__":
    unittest.main()
