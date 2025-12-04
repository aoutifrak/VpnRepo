import sys
import types
from pathlib import Path
from queue import Queue
from typing import Dict, Optional

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "docker" not in sys.modules:
    docker_stub = types.ModuleType("docker")
    docker_errors_stub = types.ModuleType("docker.errors")

    class _DummyError(Exception):
        pass

    class _DummyContainers:
        def list(self, *args, **kwargs):
            return []

    class _DummyClient:
        def __init__(self):
            self.containers = _DummyContainers()

    docker_stub.from_env = lambda: _DummyClient()
    docker_errors_stub.APIError = _DummyError
    docker_errors_stub.DockerException = _DummyError
    docker_errors_stub.NotFound = _DummyError
    docker_stub.errors = docker_errors_stub
    sys.modules["docker"] = docker_stub
    sys.modules["docker.errors"] = docker_errors_stub


import main


class FakeVPNManager:
    containers: Dict[str, Dict] = {}
    next_id: int = 1
    next_port: int = 9000
    restart_failures = set()
    bad_entries = []

    def __init__(self, **config):
        self.config = config

    @classmethod
    def reset(cls):
        cls.containers = {}
        cls.next_id = 1
        cls.next_port = 9000
        cls.restart_failures = set()
        cls.bad_entries = []

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

    def restart_and_check(self, name: str):
        entry = type(self).containers.get(name)
        if not entry:
            return {"status": "error", "message": "not_found"}
        if name in type(self).restart_failures:
            return {"status": "error", "message": "forced_failure"}
        entry["restart_count"] += 1
        entry["ip_seen"] = f"10.0.0.{entry['restart_count']}"
        return {
            "status": "ok",
            "container_id": entry["container_id"],
            "container_name": name,
            "proxy_url": entry["proxy_url"],
            "proxy_port": entry["proxy_port"],
            "ip_seen": entry["ip_seen"],
        }

    def list_proxies(self):
        items = []
        for entry in type(self).containers.values():
            items.append({
                "id": entry["container_id"],
                "name": entry["container_name"],
                "status": "running",
                "http_port": entry["proxy_port"],
            })
        return {"status": "ok", "items": items}

    def get_proxy(self, name: str):
        entry = type(self).containers.get(name)
        if not entry:
            return {"status": "error", "message": "not_found"}
        return {
            "status": "ok",
            "id": entry["container_id"],
            "name": entry["container_name"],
            "state": "running",
            "http_port": entry["proxy_port"],
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

    def mark_bad_connection(self, config_name: str, reason: Optional[str] = None):
        type(self).bad_entries.append({"config_name": config_name, "reason": reason})
        return {"status": "ok", "config_name": config_name}

    def list_bad_connections(self):
        return {"status": "ok", "items": list(type(self).bad_entries)}


def _reset_pool_state():
    main.POOL.start_worker = False
    with main.POOL.condition:
        main.POOL.registry.clear()
        main.POOL.valid_queue.clear()
        main.POOL.valid_set.clear()
        main.POOL.needs_restart.clear()
        main.POOL.pending_repairs.clear()
        main.POOL.pending_creates = 0
        main.POOL.started = False
    main.POOL.task_queue = Queue()


@pytest.fixture(autouse=True)
def patch_manager(monkeypatch):
    FakeVPNManager.reset()
    _reset_pool_state()
    main.POOL.target_size = 2
    main.POOL.manager_kwargs = main._build_manager_kwargs(main.POOL.request_config)
    monkeypatch.setattr(main, "VPNManager", FakeVPNManager)
    yield
    FakeVPNManager.reset()


@pytest.fixture
def client():
    with TestClient(main.app) as test_client:
        main.POOL.wait_until_ready(minimum=2, timeout=1)
        yield test_client


def test_new_proxy_returns_valid_container(client):
    response = client.post("/new_proxy")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    name = data["container_name"]
    assert name in main.POOL.registry
    assert main.POOL.registry[name]["state"] == "valid"


def test_restart_success_revalidates_container(client):
    first_valid = client.post("/new_proxy").json()["container_name"]
    second_valid = client.post("/new_proxy").json()["container_name"]
    result = client.post("/restart_and_check", json={"container_name": first_valid})
    assert result.status_code == 200
    payload = result.json()
    assert payload["replacement"]["container_name"] == second_valid
    assert first_valid in main.POOL.needs_restart

    sweep = client.post("/maintenance/sweep")
    assert sweep.status_code == 200
    data = sweep.json()
    recovered = next(item for item in data["processed"] if item["container_name"] == first_valid)
    assert recovered["status"] == "recovered"
    assert main.POOL.registry[first_valid]["state"] == "valid"
    assert first_valid not in main.POOL.needs_restart


def test_restart_failure_marks_container_for_repair(client):
    target = client.post("/new_proxy").json()["container_name"]
    backup = client.post("/new_proxy").json()["container_name"]
    assert backup != target

    FakeVPNManager.restart_failures.add(target)
    response = client.post("/restart_and_check", json={"container_name": target})
    assert response.status_code == 200
    assert target in main.POOL.needs_restart

    sweep = client.post("/maintenance/sweep")
    assert sweep.status_code == 200
    data = sweep.json()
    outcome = next(item for item in data["processed"] if item["container_name"] == target)
    assert outcome["status"] == "replaced"
    assert target not in main.POOL.registry
    assert target not in FakeVPNManager.containers
    assert len(main.POOL.registry) == main.POOL.target_size
