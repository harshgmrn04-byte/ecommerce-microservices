from __future__ import annotations

import http.client
import json
import os
import socket
import urllib.parse
from pathlib import Path
from typing import Any


class DockerApiError(RuntimeError):
    pass


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 5):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def discover_socket() -> str:
    docker_host = os.environ.get("DOCKER_HOST", "")
    candidates: list[Path] = []
    if docker_host.startswith("unix://"):
        candidates.append(Path(docker_host.removeprefix("unix://")))
    candidates.extend([Path("/var/run/docker.sock"), Path.home() / ".docker/run/docker.sock"])
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise DockerApiError("Docker Unix socket was not found")


class DockerApi:
    def __init__(self, socket_path: str | None = None, timeout: float = 5):
        self.socket_path = socket_path or discover_socket()
        self.timeout = timeout

    def request(self, method: str, path: str) -> tuple[int, Any]:
        connection = UnixHTTPConnection(self.socket_path, self.timeout)
        try:
            connection.request(method, path, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            parsed = (json.loads(body) if response.getheader("Content-Type", "").startswith("application/json")
                      else body) if body else None
            return response.status, parsed
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as error:
            raise DockerApiError(f"Docker API request failed: {method} {path}: {error}") from error
        finally:
            connection.close()

    def ping(self) -> None:
        status, _ = self.request("GET", "/_ping")
        if status != 200:
            raise DockerApiError(f"Docker ping returned HTTP {status}")

    def resolve_compose_service(
        self, project: str, service: str, working_directory: Path
    ) -> dict[str, Any]:
        filters = urllib.parse.quote(
            json.dumps(
                {
                    "label": [
                        f"com.docker.compose.project={project}",
                        f"com.docker.compose.service={service}",
                    ]
                }
            )
        )
        status, containers = self.request("GET", f"/containers/json?all=1&filters={filters}")
        if status != 200 or not isinstance(containers, list):
            raise DockerApiError(f"Unable to resolve Compose service {service}")
        if len(containers) != 1:
            raise DockerApiError(
                f"Expected exactly one container for {project}/{service}, found {len(containers)}"
            )
        container = self.inspect(containers[0]["Id"])
        labels = container.get("Config", {}).get("Labels", {}) or {}
        resolved_workdir = Path(labels.get("com.docker.compose.project.working_dir", "")).resolve()
        if resolved_workdir != working_directory.resolve():
            raise DockerApiError(
                f"Compose working directory mismatch: {resolved_workdir} != {working_directory.resolve()}"
            )
        return container

    def inspect(self, container_id: str) -> dict[str, Any]:
        safe_id = urllib.parse.quote(container_id, safe="")
        status, container = self.request("GET", f"/containers/{safe_id}/json")
        if status == 404:
            raise DockerApiError(f"Container no longer exists: {container_id}")
        if status != 200 or not isinstance(container, dict):
            raise DockerApiError(f"Container inspection returned HTTP {status}")
        return container

    def pause(self, container_id: str) -> None:
        safe_id = urllib.parse.quote(container_id, safe="")
        status, response = self.request("POST", f"/containers/{safe_id}/pause")
        if status != 204:
            raise DockerApiError(f"Container pause returned HTTP {status}: {response}")

    def unpause(self, container_id: str) -> None:
        safe_id = urllib.parse.quote(container_id, safe="")
        status, response = self.request("POST", f"/containers/{safe_id}/unpause")
        if status not in {204, 304}:
            raise DockerApiError(f"Container unpause returned HTTP {status}: {response}")

