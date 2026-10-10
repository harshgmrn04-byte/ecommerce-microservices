"""Discover Compose dependencies and code-backed message flows in this sandbox."""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from chaos.run_experiment import compose
from safety.configuration import ROOT


def build_map(config: dict, overrides: dict | None = None) -> dict:
    services = config["services"]
    graph = {name: sorted(service.get("depends_on", {})) for name, service in services.items()
             if "build" in service}
    for name, dependencies in (overrides or {}).items():
        if name not in services or any(dep not in services for dep in dependencies):
            raise ValueError(f"Unknown service in mapping correction: {name}")
        graph[name] = sorted(set(dependencies))
    publisher_path = "order-service/src/controllers/orderController.ts"
    consumer_path = "notification-service/src/consumers/orderConsumer.ts"
    publisher = (ROOT / publisher_path).read_text()
    consumer = (ROOT / consumer_path).read_text()
    publishes = set(re.findall(r'publishEvent\("([a-z.]+)"', publisher))
    if "publishEvent(eventMap[status]" in publisher:
        publishes.update(re.findall(r'\w+: "(order\.[a-z]+)"', publisher))
    consumes = set(re.findall(r'"(order\.[a-z]+|payment\.[a-z]+)"', consumer))
    flows = [{"from": "order-service", "via": "rabbitmq", "to": "notification-service",
              "event": event, "exchange": "orders", "queue": "notifications",
              "evidence": [publisher_path, consumer_path]} for event in sorted(publishes & consumes)]
    return {"schemaVersion": 1, "generatedAt": datetime.now(timezone.utc).isoformat(),
            "method": "docker compose config dependencies plus source-code event extraction",
            "services": graph, "dataFlows": flows,
            "optionalExternalIntegrations": ["Stripe payment (local payment used in demo)",
                                             "Cloudinary image upload (not used in demo)"],
            "evidence": {"compose": "docker-compose.yml", "orderPublisher": publisher_path,
                         "notificationConsumer": consumer_path},
            "limits": ["Declared and code-backed topology; this is not distributed tracing",
                       "Asynchronous order-to-notification flow is verified by live checkout and MailHog acceptance"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--overrides", type=Path, help="JSON mapping service to corrected dependencies")
    args = parser.parse_args()
    config = json.loads(compose("config", "--format", "json").stdout)
    overrides = json.loads(args.overrides.read_text()) if args.overrides else None
    result = build_map(config, overrides)
    (ROOT / "safety/config/dependency-map.json").write_text(json.dumps(result, indent=2) + "\n")
    lines = ["# Discovered application map", "", f"Captured: {result['generatedAt']}", "",
             "Generated with `python -m chaos.map_application`. Optional `--overrides` supports manual corrections.",
             "", "```mermaid", "graph LR"]
    for service, dependencies in result["services"].items():
        for dependency in dependencies:
            lines.append(f"    {service.replace('-', '_')} --> {dependency.replace('-', '_')}")
    lines += ['    order_service -->|"orders exchange / notifications queue"| notification_service',
              "```", "", "| Event | Publisher | Broker | Consumer |", "| --- | --- | --- | --- |"]
    lines += [f"| {flow['event']} | {flow['from']} | {flow['via']} | {flow['to']} |" for flow in result["dataFlows"]]
    lines += ["", "Checkout stores the order in PostgreSQL, publishes a persistent RabbitMQ event,",
              "then the notification consumer sends to local MailHog and records delivery in MongoDB.",
              "Frontend browsing reads products and categories; Redis caches catalog results.", "",
              "The graph describes declared dependencies and extracted event contracts.",
              "Live acceptance separately verifies checkout, buffered delivery, and recovery.", ""]
    (ROOT / "docs/application-map.md").write_text("\n".join(lines))
    print(json.dumps({"services": len(result["services"]), "dataFlows": len(result["dataFlows"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
