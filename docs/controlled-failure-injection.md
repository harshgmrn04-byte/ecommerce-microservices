# Controlled failure injection

For the separate external file kill-switch test, see
[Day 1 kill-switch acceptance](day1-kill-switch-acceptance.md).

The first experiment stops only `notification-service` inside the local Docker
Compose sandbox. Checkout remains available while RabbitMQ buffers one
`order.created` event. The safety controller then starts the service and proves
that the queued email is delivered.

## Safety controls

- The only allow-listed target is `notification-service`.
- A lock prevents concurrent experiments.
- Docker labels must match this repository and the requested Compose service.
- Every application target must be healthy before injection.
- RabbitMQ must be drained with exactly one notification consumer.
- No Prometheus safety alert may already be firing.
- The controller checks non-target availability, HTTP error rate, p95 latency,
  RabbitMQ backlog, and Prometheus alerts every two seconds.
- Synthetic authenticated probes exercise auth, product, and order APIs during
  the outage. Rate-based guardrails evaluate services receiving at least 0.1
  requests/second, and the deliberately expensive login route has its own
  baseline rather than using the 100 ms general API threshold.
- Target unavailability is limited to 15 seconds.
- The experiment duration is limited to 20 seconds.
- Recovery must complete within 30 seconds.
- Rollback runs from a `finally` block on success, error, Ctrl-C, or SIGTERM.

## Run it

Validate the preconditions without injecting a fault:

```bash
python3 chaos/run_experiment.py --dry-run
```

Run the controlled notification-service outage:

```bash
python3 chaos/run_experiment.py
```

To request an immediate rollback from another terminal:

```bash
touch chaos/KILL_SWITCH
```

Remove that file before the next run. The latest machine-readable report is
written to `chaos/results/notification-service-crash.json`.

## Expected result

1. The notification container stops.
2. A checkout still returns HTTP 201.
3. RabbitMQ shows one ready notification.
4. The 15-second unavailability guardrail requests rollback.
5. The notification container restarts, consumes the event, and sends the email
   to MailHog.
6. All Prometheus targets return to `up = 1` with no firing safety alerts.

## Verified run

The experiment passed on 2026-10-10:

| Observation | Result |
|---|---:|
| Notification outage observed by Prometheus | Yes |
| Checkout during outage | HTTP 201 |
| Maximum RabbitMQ ready backlog | 1 message |
| Automatic rollback | Triggered at 15.0 seconds |
| Recovery time | 5.1 seconds |
| Buffered email delivered after recovery | Yes |
| Final application targets up | 4 / 4 |
| Final firing alerts | 0 |

All five experiment assertions passed. The complete timestamps, guardrail
samples, canary order ID, and before/after state are stored in
[`chaos/results/notification-service-crash.json`](../chaos/results/notification-service-crash.json).
