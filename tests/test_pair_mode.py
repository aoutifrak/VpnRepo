import sys
import types
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "docker" not in sys.modules:
    class _DummyContainers:
        def list(self, *args, **kwargs):
            return []

    class _DummyDockerClient:
        def __init__(self):
            self.containers = _DummyContainers()

    class _DummyError(Exception):
        pass

    docker_stub = types.ModuleType("docker")
    docker_stub.from_env = lambda: _DummyDockerClient()

    docker_errors_stub = types.ModuleType("docker.errors")
    docker_errors_stub.APIError = _DummyError
    docker_errors_stub.DockerException = _DummyError
    docker_errors_stub.NotFound = _DummyError

    docker_stub.errors = docker_errors_stub

    sys.modules["docker"] = docker_stub
    sys.modules["docker.errors"] = docker_errors_stub

import main


class FakeVPNManager:
    containers = {}
    next_id = 1
    next_port = 9000

    def __init__(self, **config):
        self.config = config

    @classmethod
    def reset(cls):
        cls.containers = {}
        cls.next_id = 1
        cls.next_port = 9000

    def create_vpn_proxy(self):
        name = f"fake-proxy-{type(self).next_id}"
        container_id = f"id-{type(self).next_id}"
        port = type(self).next_port
        ip_seen = f"10.0.0.{type(self).next_id}"
        type(self).next_id += 1
        type(self).next_port += 1
        data = {
            "status": "ok",
            "container_id": container_id,
            "container_name": name,
            "proxy_url": f"http://127.0.0.1:{port}",
            "proxy_port": port,
            "ip_seen": ip_seen,
        }
        stored = data.copy()
        stored["restart_count"] = 0
        type(self).containers[name] = stored
        return data

    def check_container(self, name: str):
        entry = type(self).containers.get(name)
        if not entry:
            return {"status": "error", "message": "not_found"}
        return {
            "status": "ok",
            "container_id": entry["container_id"],
            "container_name": name,
            "proxy_url": entry["proxy_url"],
            "proxy_port": entry["proxy_port"],
            "ip_seen": entry["ip_seen"],
        }

    def restart_and_check(self, name: str):
        entry = type(self).containers.get(name)
        if not entry:
            return {"status": "error", "message": "not_found"}
        entry["restart_count"] += 1
        entry["ip_seen"] = f"{entry['ip_seen'].split('-')[0]}-r{entry['restart_count']}"
        return {
            "status": "ok",
            "container_id": entry["container_id"],
            "container_name": name,
            "proxy_url": entry["proxy_url"],
            "proxy_port": entry["proxy_port"],
            "ip_seen": entry["ip_seen"],
        }

    def delete_proxy(self, name: str):
        if name in type(self).containers:
            del type(self).containers[name]
            return {"status": "ok", "deleted": name}
        return {"status": "error", "message": "not_found"}

    def delete_all_proxies(self):
        deleted = list(type(self).containers.keys())
        type(self).containers.clear()
        return {"status": "ok", "deleted": deleted}


@pytest.fixture(autouse=True)
def patch_manager(monkeypatch):
    FakeVPNManager.reset()
    with main.PAIR_LOCK:
        main.PAIR_STATE["containers"] = []
        main.PAIR_STATE["next_restart_index"] = 0
        main.PAIR_STATE["config"] = None
    monkeypatch.setattr(main, "VPNManager", FakeVPNManager)
    monkeypatch.setattr(main, "_sweeper_loop", lambda *args, **kwargs: None)
    yield
    FakeVPNManager.reset()
    with main.PAIR_LOCK:
        main.PAIR_STATE["containers"] = []
        main.PAIR_STATE["next_restart_index"] = 0
        main.PAIR_STATE["config"] = None


@pytest.fixture
def client():
    with TestClient(main.app) as test_client:
        yield test_client


def test_new_proxy_creates_pair(client):
    response = client.post("/new_proxy")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    with main.PAIR_LOCK:
        assert len(main.PAIR_STATE["containers"]) == 2
        assert data["container_name"] == main.PAIR_STATE["containers"][0]["container_name"]


def test_restart_and_check_alternates_between_pair(client):
    client.post("/new_proxy")
    with main.PAIR_LOCK:
        initial_names = [entry["container_name"] for entry in main.PAIR_STATE["containers"]]
    first = client.post("/restart_and_check")
    assert first.status_code == 200
    first_data = first.json()
    assert first_data["container_name"] == initial_names[1]
    second = client.post("/restart_and_check")
    assert second.status_code == 200
    second_data = second.json()
    assert second_data["container_name"] == initial_names[0]


def test_restart_and_check_compat_endpoint(client):
    client.post("/new_proxy")
    with main.PAIR_LOCK:
        target_name = main.PAIR_STATE["containers"][0]["container_name"]
    response = client.post(f"/proxy/{target_name}/restart_and_check")
    assert response.status_code == 200
    data = response.json()
    assert data["container_name"] in {c["container_name"] for c in FakeVPNManager.containers.values()}
