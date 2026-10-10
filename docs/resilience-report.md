# Hackathon deliverable audit and resilience report

Generated: 2026-10-10T11:40:17.386433+00:00

Audit status: **complete**. The supplied screenshots specify five solution capabilities
and Day 1 guidance checkpoints; they do not require implementing every example fault type.

| Deliverable | Status | Evidence |
| --- | --- | --- |
| Public, free open-source sample application | complete | Public upstream repository and local MIT LICENSE |
| Containerized application running end to end locally | complete | baseline.json: browse, login, checkout, local email |
| Service, dependency and data-flow mapping | complete | dependency-map.json and docs/application-map.md; generated from Compose/source |
| Steady-load error, latency and health baseline | complete | baseline.json, k6-summary.json, baseline-observations.json |
| Intelligent experiment choice and explanation | complete | plan.json: graph scoring, Bayesian outcome learning, uncertainty, escalation gates |
| At least one controlled failure observed | complete | pause-completed.json and direct Docker/RabbitMQ observations |
| Single-target blast radius and bounded duration | complete | Policy allowlist, project identity, lease, absolute deadline, safety-journal.json |
| Automatic threshold stop and rollback tested | complete | Real p95 exceeds a stricter 1 ms test ceiling; legacy outage-limit rollback also retained |
| Working manual kill switch tested | complete | pause-manual-kill.json; persistent latch and verified unpause |
| Capture errors, slowdowns and recovery time | complete | Controller phase samples: per-service errors/p95, probes, timestamps and recovery durations |
| Analysis, ranked fixes and resilience score | complete | report.json, report.html and docs/resilience-report.md; scoped score and coverage limits |
| Repository links, deployment steps and Day 1 demo | complete | docs/hackathon-submission.md and README.md |

## Observed results

- Baseline: 2682 requests, 0% HTTP failures, p95 25.83 ms.
- Checkout: 10/10 successful under normal load.
- Live safety acceptance: 7/7 checks passed.
- Normal pause recovery confirmed 5.316 seconds after fault removal.
- Tested-scenario score: 100/100.

Score weights: checkout availability 40, buffered email delivery 25, verified fault removal 20,
and confirmed recovery within the policy deadline 15. This is an explicit engineering rubric,
not a probability, service-wide SLA, or system-wide resilience score.

| Safety case | Result | Total test time |
| --- | --- | --- |
| completed | passed | 23.352s |
| latency-threshold | passed | 16.225s |
| manual-kill | passed | 15.995s |
| controller-sigterm | passed | 15.940s |
| controller-crash | passed | 13.632s |
| lease-expiry | passed | 14.213s |
| actuator-restart | passed | 6.482s |

The latency drill uses a **stricter 1 ms ceiling held for 3 seconds** to exercise
threshold rollback against real Prometheus measurements. It does not claim the application
breached the normal 2,000 ms ceiling. Crash/restart drills verify actual Docker unpause,
healthy protected traffic, queue drain and the specific canary email.

## Ranked weaknesses and fixes

Ranking puts the observed delivery weakness first, followed by source-review risks
in severity order. Untested source risks are not claimed as measured failures.

1. **Single notification consumer delays all delivery during its outage** (medium; observed).
   Recommendation: Run redundant consumers, alert on oldest message age, and bound consumer prefetch.
   Evidence: `pause-completed.json and safety-acceptance.json`.
2. **Order storage and event publication lack an atomic outbox** (high; source review).
   Recommendation: Persist an outbox row with the order, relay with publisher confirms, and make delivery idempotent.
   Evidence: `order-service/src/controllers/orderController.ts; order-service/src/utils/rabbitmq.ts`.
   Limit: Broker failure was not injected; event loss is a source-based risk.
3. **Failed notification messages can be requeued indefinitely** (medium; source review).
   Recommendation: Use bounded retries and a dead-letter queue; deduplicate by order ID and event type.
   Evidence: `notification-service/src/consumers/orderConsumer.ts`.
   Limit: SMTP failure and poison-message load were not injected.

## Planning model and coverage

The local planner combines graph criticality, dependency count, duration cost, and a
Beta-Bernoulli model of adverse outcomes. Beta(1,1) is its explicit prior; each real
completed/recovered run updates the posterior. The posterior mean plus two standard
deviations supplies an exploration weight. Safety drills and cancelled/crashed controllers
are excluded from training. The model works offline and proposes only policy profiles.
The safety controller independently gates every mutation with fresh metrics and probes.

This score covers one failure scenario. Other services, network, disk, database and traffic-spike faults remain untested; examples in the brief are optional.

Original gaps closed: missing measured baseline service counts; unusable actuator ping;
interrupted lifecycle records; graceful-shutdown cleanup; missing live pause/watchdog
acceptance; manual-only topology; heuristic-only planning; and missing final report/demo.

Raw evidence is retained in `chaos/results/` and `observability/results/`.
The first failed queue-observation attempt is retained separately; it led to replacing
eventually consistent management counters with direct `rabbitmqctl` observations.
