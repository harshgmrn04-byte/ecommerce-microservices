# Monitoring baseline

Captured at 2026-10-10T10:48:11.688609+00:00 in the local Docker Compose sandbox.
Regenerate with `python3 -m observability.capture_baseline`.

The capture command runs the existing k6 workload for three minutes: five
concurrent browsing users and three local checkout arrivals per minute. It
records service request counts from Prometheus, health and queue observations
throughout the run, actual container identities/restart deltas, and the raw k6
summary. It rejects an unhealthy baseline rather than inventing missing values.

| Signal | Measured baseline |
| --- | --- |
| Requests | 2687 |
| Request rate | 14.47/s |
| HTTP failures | 0 (0%) |
| Overall p95 latency | 17.76 ms |
| Product p95 latency (Prometheus histogram) | 14.12 ms |
| Checkout | 9/9 successful |
| Local emails during capture | 9 |
| Restart deltas during capture | 0 |

Measured per-service traffic counts (Prometheus `increase` over the capture
interval): `{"auth-service": 1, "order-service": 9, "product-service": 1848}`. These
can include concurrent local probes and are separate from k6's exact request
count. HTTP metrics exclude health and metrics endpoints. Histogram p95 values
are estimates based on the configured buckets.

## Current pause-controller policy

The artifact expires after 24 hours. Product traffic must have at least 100
requests in the artifact and each live 60-second monitoring window. Admission
requires error fraction below 1% and p95 below 750 ms, fresh sources, all required
targets/probes healthy, and zero restart deltas in the captured baseline.

During a fault, the controller aborts for sustained errors above 5%, sustained
latency above its baseline-relative ceiling (floor 1,000 ms) or absolute ceiling
of 2,000 ms, failed critical probes, unexpected non-target unavailability, stale
or missing telemetry, and traffic invalidity when required by a profile. The
independent watchdog enforces a single sandbox application target, renewable
8-second lease and absolute duration deadline. The normal notification profile
lasts 20 seconds; acceptance lasts 12 seconds. Recovery requires healthy traffic
and a sustained hold. See `safety/config/policy.json` for exact bounds.

The earlier stop demo retains separate legacy thresholds: 100 ms general API
p95, a 15-second outage ceiling, 20-second maximum experiment, and 30-second
recovery deadline. These must not be confused with the pause controller policy.

Raw evidence: `observability/results/k6-summary.json` and
`observability/results/baseline-observations.json`. Current controller admission
reads `observability/baseline.json` directly.
