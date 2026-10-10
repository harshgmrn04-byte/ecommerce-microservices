# Safety review and intelligent experiment planning

Current status: **implemented and verified in the local Docker sandbox**.
The full deliverable audit is in [resilience-report.md](resilience-report.md),
and reproduction/operator commands are in [hackathon-submission.md](hackathon-submission.md).

The earlier review's blockers have been resolved:

- Fresh steady-load baseline capture includes measured per-service request
  counts, current timestamps, real container identities and restart deltas.
- Docker plain-text ping responses are accepted; the actuator runs on Docker
  Desktop's macOS Unix socket as well as Linux.
- Expired prepared leases cannot activate. An exclusive process lock prevents
  a second watchdog from replacing the active socket.
- Interrupted controller records are finalized after verified cleanup;
  conservative restart reconciliation prevents stale faults from blocking later
  admission. Graceful watchdog shutdown removes active faults.
- Controller results retain preflight, fault and recovery snapshots, timing,
  policy version and profile identity. External kill and normal deadline
  removal are classified from actual actuator state.
- All seven live acceptance cases verify actual pause/unpause, continued
  checkout, buffered event delivery and sustained healthy protected traffic.
  The suite tests normal duration, stricter latency threshold, manual kill,
  controller SIGTERM/SIGKILL, lease expiry, and actuator SIGKILL/restart.

The threshold acceptance case intentionally lowers the p95 ceiling to 1 ms
with a 3-second hold against real Prometheus metrics. This is a safety drill,
not evidence of a normal production-limit violation. The raw controller
result, case assertions and persistent journal are in `chaos/results/`.

The planner combines configured graph criticality, dependency count and duration
cost with a Beta-Bernoulli learner of observed adverse outcomes. Its prior and
uncertainty are explicit; one successful scenario is limited evidence. Only
real experiment results train the learner. Drill results, manual cancellation
and controller failures do not distort the model. Plans are proposals, and a
longer profile requires a shorter successful run under the current policy.

The map is now generated from Compose dependencies and source event contracts,
with optional manual corrections. Live business-flow checks separately verify
the order-to-notification path; this is not distributed tracing.

Coverage remains deliberate: notification-service container pause/stop only.
The example network, disk, database and spike faults are not mandatory in the
supplied brief and are not represented as verified. The scenario score is not
an overall production resilience certification. Ranked weaknesses and fixes,
including code-review risks not yet fault-tested, are clearly identified in
the final report.

Run `python3 -m unittest discover -s tests -v` for regression checks. Live
acceptance uses `python3 -m chaos.acceptance`; unit tests alone are not proof of
Docker recovery. The legacy stop runner and file kill switch remain separate
from the newer pause watchdog and journaled latch.
