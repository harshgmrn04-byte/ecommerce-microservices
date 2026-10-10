# Bosch hackathon submission and demo

## Project and sample application

**AI-Driven Chaos Engineering for Cloud-Native Resilience** applied to a local
e-commerce application with a Next.js storefront, four Node.js services,
PostgreSQL, MongoDB, Redis, RabbitMQ and MailHog.

- Submission repository: https://github.com/harshgmrn04-byte/ecommerce-microservices
- Original public sample: https://github.com/andrelima28439/ecommerce-microservices
- License: MIT; original attribution is retained in `LICENSE`.
- Local deployment: Docker Compose. Python 3.10+ runs the chaos tooling without
  third-party Python packages. Docker Desktop on macOS and Linux Docker with a
  Unix socket are supported by the watchdog.

The screenshots describe five capabilities and Day 1 guidance checkpoints.
Failure types such as full disk, database overload and network loss are examples,
not a requirement to implement all six. This submission implements bounded
container pause, and retains the earlier container-stop demonstration.

## Deploy and reproduce the evidence

```bash
git clone https://github.com/harshgmrn04-byte/ecommerce-microservices.git
cd ecommerce-microservices
docker compose up -d --build --wait --wait-timeout 240
python3 -m chaos.map_application
python3 -m observability.capture_baseline
python3 -m chaos.acceptance
python3 -m chaos.report
python3 -m chaos.dashboard
```

These commands generate the dependency/data-flow map, measure three minutes of
steady browsing and local checkout, exercise seven live safety cases, and generate
the scored report from real evidence.

Open http://localhost:8070 for the evidence dashboard, http://localhost:3000 for
the storefront, http://localhost:3005 for Grafana (`admin` / `admin`), and
http://localhost:8025 for local email. The default local app account is
`admin@test.com` / `123456`. The demo uses local checkout and MailHog; no Stripe,
Cloudinary or hosted AI credentials are needed. Optional uploaded/catalog images
may reference external URLs, while service processing and fault execution stay local.

The baseline expires after 24 hours. Refresh it before future experiments.
The acceptance command maintains a browse workload throughout fault observation
and recovery. It refuses to start when another watchdog is running.

## How the intelligence works

The planner scores configured experiments using service criticality, upstream
reachability, dependencies, duration cost, and a Beta-Bernoulli learner updated
from real experiment outcomes. It exposes its prior, posterior, uncertainty and
score components. Cancelled runs and safety drills do not become training data.
The planner proposes a longer experiment only after a successful shorter run
under the same policy.
All choices remain proposals; the controller independently verifies live health,
baseline freshness and policy before applying a fault.

```bash
python3 -m chaos.planner --history chaos/results/history.json
```

The graph is discovered from declared dependencies and source event contracts.
It is not advertised as distributed tracing. Live checkout, queue and email
checks validate the asynchronous data flow exercised in the demonstration.

## Short Day 1 presentation

1. Open the storefront and sign in. Show the product catalog and order history.
2. Open the evidence dashboard. Show the discovered dependencies and baseline.
3. Show the ranked notification-pause proposal, hypothesis and safety bounds.
4. Run the acceptance command. During the pause, show checkout availability and
   RabbitMQ's buffered/unacknowledged message; after rollback show its specific
   confirmation email in MailHog.
5. Show the automatic latency-threshold, manual kill, controller crash, lease
   expiry and actuator restart evidence. Each verifies actual Docker unpause.
6. Show the scenario score, ranked fixes and explicit limits of test coverage.

## Interactive operator commands

For a separate live demonstration, start a watchdog in one terminal:

```bash
python3 -m safety.actuator --serve
```

The controller needs a valid baseline and at least 100 product requests in the
current 60-second window. Keep `docker compose --profile tools run --rm k6`
running in another terminal before and throughout recovery. Then:

```bash
python3 -m safety run --profile notification-pause-acceptance
python3 -m safety status

# From another terminal, engage the persistent emergency kill switch.
python3 -m safety kill

# Reset after the controller finishes and fresh live health passes.
python3 -m safety reset
python3 -m safety shutdown
```

The legacy stop demo is reproducible with `python3 -m chaos.accept_manual_kill`.
Its file kill switch and evidence are distinct from the pause watchdog's
journaled kill latch; do not run both runners concurrently.

## Evidence and limitations

- `docs/resilience-report.md`: per-deliverable status, observed measurements,
  ranked findings, recommendations and score rubric.
- `docs/application-map.md`: generated dependency and message-flow diagrams.
- `observability/baseline.json`: fresh baseline, measured service traffic and
  restart deltas; raw k6 and health observations are in `observability/results/`.
- `chaos/results/safety-acceptance.json`: seven live cases, assertions, queue
  evidence, service metrics and canary IDs.
- `chaos/results/safety-journal.json`: persistent fault/lifecycle evidence.
- `chaos/results/pause-*.json`: controller samples and recovery timings.
- `chaos/results/plan.json`, `report.json`, `report.html`: generated submission artifacts.

The 100-point score describes only notification-outage checkout/delivery and
recovery. It does not establish resilience to other service failures or network,
disk, database and spike faults. The latency acceptance drill intentionally uses
a stricter 1 ms ceiling with a 3-second hold against real measurements; it does
not claim the normal application latency limit was exceeded.

## Verification

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q chaos safety observability
```

CI checks these tests and TypeScript compilation. The `Hackathon live acceptance`
workflow deploys a fresh disposable stack, regenerates all evidence, and uploads
it even on failure. Its volume cleanup applies only to its disposable Actions
environment. Local commands preserve the team's application volumes.
