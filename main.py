import json
import logging
import time
import uuid
from collections import deque
from pathlib import Path
from queue import Empty, Queue
from threading import Condition, Lock, Thread
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from vpn_manager import VPNManager

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(name)s %(message)s')
logger = logging.getLogger("vpnMan")

app = FastAPI(title="vpn_manager", version="3.0.0")

CONFIG_PATH = Path("./config.json")
DEFAULT_POOL_SIZE = 8
MAX_REPAIR_ATTEMPTS = 3

JOBS: Dict[str, Dict] = {}


class NewProxyRequest(BaseModel):
    port_min: int = 8887
    port_max: int = 20000
    health_timeout: int = 45
    request_timeout: int = 15
    max_attempts: int = 5


class RestartRequest(BaseModel):
    container_name: str


class ReportBadRequest(BaseModel):
    config_name: str
    reason: Optional[str] = None


def _load_pool_target_size(default: int = DEFAULT_POOL_SIZE) -> int:
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except Exception:
        return default
    try:
        raw_value = data.get("container_pool_size", default)
        value = int(raw_value)
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _build_manager_kwargs(config: Dict) -> Dict:
    return {
        "port_min": config.get("port_min", 8887),
        "port_max": config.get("port_max", 20000),
        "health_timeout": config.get("health_timeout", 45),
        "request_timeout": config.get("request_timeout", 15),
        "max_attempts": config.get("max_attempts", 5),
    }


def _sanitize_entry(entry: Optional[Dict]) -> Optional[Dict]:
    if not entry:
        return None
    return {
        "status": entry.get("status", "ok"),
        "container_id": entry.get("container_id"),
        "container_name": entry.get("container_name"),
        "proxy_port": entry.get("proxy_port"),
        "proxy_url": entry.get("proxy_url"),
        "ip_seen": entry.get("ip_seen"),
    }


class ContainerPool:
    def __init__(self, target_size: int, request_config: Dict, max_repair_attempts: int) -> None:
        self.target_size = max(int(target_size), 0)
        self.request_config = dict(request_config)
        self.manager_kwargs = _build_manager_kwargs(self.request_config)
        self.max_repair_attempts = max(1, int(max_repair_attempts))

        self.lock = Lock()
        self.condition = Condition(self.lock)
        self.registry: Dict[str, Dict] = {}
        self.valid_queue = deque()
        self.valid_set = set()
        self.pending_repairs = set()
        self.pending_creates = 0
        self.task_queue: "Queue[Dict]" = Queue()
        self.started = False
        self.start_worker = True
        self.needs_restart = set()
        self.restart_wait_seconds = 15

    def start(self) -> None:
        with self.lock:
            if self.started:
                return
            self.started = True
            if not self.start_worker:
                return
        Thread(target=self._initial_fill, name="pool-initial-fill", daemon=True).start()
        Thread(target=self._worker_loop, name="pool-worker", daemon=True).start()

    def wait_until_ready(self, minimum: int = 1, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        with self.condition:
            while self._count_valid_locked() < minimum:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                self.condition.wait(timeout=remaining)
            return True

    def create_sync(self) -> Optional[Dict]:
        entry = self._direct_create()
        return _sanitize_entry(entry)

    def get_valid(self) -> Optional[Dict]:
        with self.condition:
            while self.valid_queue:
                name = self.valid_queue.popleft()
                if name not in self.valid_set:
                    continue
                entry = self.registry.get(name)
                if not entry or entry.get("state") != "valid":
                    self.valid_set.discard(name)
                    continue
                self.valid_queue.append(name)
                return _sanitize_entry(entry)
            return None

    def schedule_restart(self, name: str) -> Dict:
        return self.mark_for_restart(name)

    def mark_for_restart(self, name: str) -> Dict:
        self._flag_container_for_restart(name)
        replacement = self.get_valid()
        if replacement:
            return replacement
        replacement = self.create_sync()
        if replacement:
            return replacement
        raise RuntimeError("no_available_container")

    def _flag_container_for_restart(self, name: str) -> None:
        with self.condition:
            entry = self.registry.get(name)
            if not entry:
                raise KeyError(name)
            self._mark_invalid_locked(name)
            self.needs_restart.add(name)

    def trigger_repair(self, name: str) -> bool:
        try:
            self._flag_container_for_restart(name)
        except KeyError:
            return False
        return True

    def remove_container(self, name: str) -> bool:
        with self.condition:
            existed = self._remove_container_locked(name)
        if existed:
            self._schedule_create()
        return existed

    def reset_state(self) -> None:
        with self.condition:
            self.registry.clear()
            self.valid_queue.clear()
            self.valid_set.clear()
            self.needs_restart.clear()
            self.pending_repairs.clear()
            self.pending_creates = 0
        while True:
            try:
                self.task_queue.get_nowait()
                self.task_queue.task_done()
            except Empty:
                break
        for _ in range(self.target_size):
            self._schedule_create()

    def list_names(self) -> Dict[str, Dict]:
        with self.condition:
            return {name: dict(entry) for name, entry in self.registry.items()}

    def _new_manager(self) -> VPNManager:
        return VPNManager(**self.manager_kwargs)

    def _initial_fill(self) -> None:
        if self.target_size <= 0:
            return
        while True:
            with self.condition:
                deficit = self.target_size - self._count_valid_locked()
            if deficit <= 0:
                break
            entry = self._direct_create()
            if not entry:
                time.sleep(3)

    def _direct_create(self) -> Optional[Dict]:
        try:
            manager = self._new_manager()
            result = manager.create_vpn_proxy()
        except Exception as exc:
            logger.exception("Container creation failed: %s", exc)
            return None
        if result.get("status") != "ok":
            logger.warning("Container creation returned error: %s", result)
            return None
        with self.condition:
            entry = self._store_valid_locked(result)
        return entry

    def _store_valid_locked(self, result: Dict) -> Dict:
        name = result.get("container_name")
        if not name:
            return {}
        entry = dict(result)
        entry.setdefault("status", "ok")
        entry["state"] = "valid"
        entry["last_updated"] = int(time.time())
        self.registry[name] = entry
        try:
            self.valid_queue.remove(name)
        except ValueError:
            pass
        self.valid_queue.append(name)
        self.valid_set.add(name)
        self.needs_restart.discard(name)
        self.pending_repairs.discard(name)
        self.condition.notify_all()
        return entry

    def _mark_invalid_locked(self, name: str) -> None:
        entry = self.registry.get(name)
        if not entry:
            return
        entry["state"] = "invalid"
        entry["last_updated"] = int(time.time())
        self.valid_set.discard(name)
        try:
            self.valid_queue.remove(name)
        except ValueError:
            pass

    def _pop_next_valid_locked(self) -> Optional[str]:
        while self.valid_queue:
            candidate = self.valid_queue.popleft()
            if candidate in self.valid_set:
                return candidate
        return None

    def _enqueue_repair(self, name: str, attempts: int = 0) -> None:
        with self.condition:
            if name in self.pending_repairs:
                return
            self.pending_repairs.add(name)
        self.task_queue.put({"type": "repair", "name": name, "attempts": attempts})

    def _schedule_create(self) -> None:
        if self.target_size <= 0:
            return
        with self.condition:
            if len(self.registry) + self.pending_creates >= self.target_size:
                return
            self.pending_creates += 1
        if not self.start_worker:
            try:
                entry = self._direct_create()
                if not entry:
                    logger.warning("Synchronous create failed during schedule")
            finally:
                with self.condition:
                    if self.pending_creates > 0:
                        self.pending_creates -= 1
            return
        self.task_queue.put({"type": "create", "attempts": 0})

    def _worker_loop(self) -> None:
        while True:
            try:
                task = self.task_queue.get(timeout=1.0)
            except Empty:
                continue
            try:
                task_type = task.get("type")
                if task_type == "repair":
                    self._handle_repair_task(task)
                elif task_type == "create":
                    self._handle_create_task(task)
            except Exception:
                logger.exception("Pool worker task failure")
            finally:
                self.task_queue.task_done()

    def _handle_create_task(self, task: Dict) -> None:
        attempts = int(task.get("attempts", 0))
        try:
            manager = self._new_manager()
            result = manager.create_vpn_proxy()
        except Exception as exc:
            logger.exception("Background creation error: %s", exc)
            result = {"status": "error", "message": str(exc)}
        if result.get("status") == "ok":
            with self.condition:
                self._store_valid_locked(result)
                if self.pending_creates > 0:
                    self.pending_creates -= 1
            return
        if attempts + 1 >= self.max_repair_attempts:
            logger.warning("Background creation failed %s times; retrying", attempts + 1)
            attempts = -1
        time.sleep(3)
        self.task_queue.put({"type": "create", "attempts": attempts + 1})

    def _handle_repair_task(self, task: Dict) -> None:
        name = task.get("name")
        attempts = int(task.get("attempts", 0))
        if not name:
            return
        try:
            manager = self._new_manager()
            result = manager.restart_and_check(name)
        except Exception as exc:
            logger.warning("Repair restart failed for %s: %s", name, exc)
            result = {"status": "error", "message": str(exc)}
        if result.get("status") == "ok":
            with self.condition:
                self._store_valid_locked(result)
                self.pending_repairs.discard(name)
            return
        if attempts + 1 >= self.max_repair_attempts:
            logger.warning("Repair exhausted for %s; replacing container", name)
            try:
                manager.delete_proxy(name)
            except Exception as exc:
                logger.warning("Failed to delete container %s: %s", name, exc)
            with self.condition:
                self._remove_container_locked(name)
                self.pending_repairs.discard(name)
            self._schedule_create()
            return
        time.sleep(2)
        self.task_queue.put({"type": "repair", "name": name, "attempts": attempts + 1})

    def _remove_container_locked(self, name: str) -> bool:
        removed = False
        if name in self.registry:
            self.registry.pop(name, None)
            removed = True
        self.valid_set.discard(name)
        try:
            self.valid_queue.remove(name)
        except ValueError:
            pass
        self.pending_repairs.discard(name)
        self.needs_restart.discard(name)
        return removed

    def _count_valid_locked(self) -> int:
        return len(self.valid_set)

    def run_sweeper(self) -> Dict:
        targets = self._gather_sweep_targets()
        results = []
        for name in targets:
            outcome = self._restart_with_retries(name)
            if outcome:
                results.append(outcome)
        return {"status": "ok", "processed": results}

    def _gather_sweep_targets(self) -> list:
        with self.condition:
            targets = set(self.needs_restart)
            for name, entry in self.registry.items():
                if entry.get("state") != "valid":
                    targets.add(name)
        return list(targets)

    def _restart_with_retries(self, name: str) -> Optional[Dict]:
        with self.condition:
            if name not in self.registry:
                self.needs_restart.discard(name)
                return {"container_name": name, "status": "missing"}
        manager = self._new_manager()
        attempts = 0
        deadline = time.time() + self.restart_wait_seconds
        last_error = None
        while attempts < self.max_repair_attempts and time.time() <= deadline:
            attempts += 1
            try:
                result = manager.restart_and_check(name)
            except Exception as exc:
                last_error = str(exc)
                result = {"status": "error", "message": last_error}
            else:
                last_error = result.get("message")
            if result.get("status") == "ok":
                with self.condition:
                    entry = self._store_valid_locked(result)
                return {
                    "container": _sanitize_entry(entry),
                    "status": "recovered",
                    "attempts": attempts,
                    "container_name": name,
                }
            remaining = deadline - time.time()
            if attempts >= self.max_repair_attempts or remaining <= 0:
                break
            time.sleep(min(0.5, max(0.0, remaining)))
        try:
            manager.delete_proxy(name)
        except Exception as exc:
            logger.warning("Failed to delete container %s: %s", name, exc)
        with self.condition:
            removed = self._remove_container_locked(name)
        if removed:
            self._schedule_create()
        error_message = last_error or "restart_failed"
        return {
            "container_name": name,
            "status": "replaced",
            "attempts": attempts or self.max_repair_attempts,
            "error": error_message,
        }


POOL = ContainerPool(
    target_size=_load_pool_target_size(),
    request_config=NewProxyRequest().model_dump(),
    max_repair_attempts=MAX_REPAIR_ATTEMPTS,
)


def _ensure_config_matches(req: Optional[NewProxyRequest]) -> None:
    requested = (req or NewProxyRequest()).model_dump()
    if requested != POOL.request_config:
        raise HTTPException(status_code=400,
                            detail={"status": "error", "message": "pool_config_is_static"})


def _get_manager() -> VPNManager:
    return VPNManager(**POOL.manager_kwargs)


@app.on_event("startup")
def startup_pool() -> None:
    POOL.start()


@app.post("/new_proxy")
def new_proxy(req: Optional[NewProxyRequest] = None):
    try:
        _ensure_config_matches(req)
        container = POOL.get_valid()
        if container:
            return container
        created = POOL.create_sync()
        if created:
            return created
        raise HTTPException(status_code=503,
                            detail={"status": "error", "message": "no_available_container"})
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail={"status": "error", "message": str(exc)})
    except Exception as exc:
        logger.exception("new_proxy failure")
        raise HTTPException(status_code=500, detail={"status": "error", "message": str(exc)})


@app.post("/new_proxy_async")
def new_proxy_async(req: Optional[NewProxyRequest] = None):
    _ensure_config_matches(req)
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"status": "queued", "result": None, "created_at": int(time.time())}

    def worker():
        try:
            container = POOL.get_valid()
            if not container:
                container = POOL.create_sync()
            if container:
                JOBS[job_id]["result"] = container
                JOBS[job_id]["status"] = "done"
            else:
                JOBS[job_id]["result"] = {"status": "error", "message": "no_available_container"}
                JOBS[job_id]["status"] = "error"
        except Exception as exc:
            JOBS[job_id]["result"] = {"status": "error", "message": str(exc)}
            JOBS[job_id]["status"] = "error"

    Thread(target=worker, daemon=True).start()
    return {"status": "accepted", "job_id": job_id}


@app.get("/job/{job_id}")
def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404,
                            detail={"status": "error", "message": "job_not_found"})
    return {"status": job["status"], "result": job["result"], "job_id": job_id}


@app.post("/restart_and_check")
def restart_and_check(request: RestartRequest):
    try:
        replacement = POOL.mark_for_restart(request.container_name)
    except KeyError as exc:
        key = exc.args[0] if exc.args else request.container_name
        raise HTTPException(status_code=404,
                            detail={"status": "error", "message": f"container_not_found: {key}"})
    except RuntimeError as exc:
        raise HTTPException(status_code=503,
                            detail={"status": "error", "message": str(exc)})
    return {
        "status": "ok",
        "scheduled_for_restart": request.container_name,
        "replacement": replacement,
    }


@app.post("/proxy/{name}/restart_and_check")
def restart_and_check_named(name: str):
    return restart_and_check(RestartRequest(container_name=name))


@app.post("/new_proxies")
def new_proxies():
    raise HTTPException(status_code=400,
                        detail={"status": "error", "message": "bulk proxy creation unsupported in pool mode"})


@app.get("/proxies")
def list_proxies():
    try:
        manager = _get_manager()
        return manager.list_proxies()
    except Exception as exc:
        logger.exception("Failed to list proxies")
        raise HTTPException(status_code=500, detail={"status": "error", "message": str(exc)})


@app.get("/proxy/{name}")
def get_proxy(name: str):
    try:
        manager = _get_manager()
        res = manager.get_proxy(name)
        if res.get("status") == "ok":
            return res
        raise HTTPException(status_code=404, detail=res)
    except Exception as exc:
        logger.exception("Failed to get proxy")
        raise HTTPException(status_code=500, detail={"status": "error", "message": str(exc)})


@app.post("/maintenance/sweep")
def maintenance_sweep():
    return POOL.run_sweeper()


@app.delete("/proxy/{name}")
def delete_proxy(name: str):
    try:
        manager = _get_manager()
        res = manager.delete_proxy(name)
        if res.get("status") == "ok":
            POOL.remove_container(name)
            return res
        raise HTTPException(status_code=404, detail=res)
    except Exception as exc:
        logger.exception("Failed to delete proxy")
        raise HTTPException(status_code=500, detail={"status": "error", "message": str(exc)})


@app.delete("/proxies")
def delete_all_proxies():
    try:
        manager = _get_manager()
        res = manager.delete_all_proxies()
        POOL.reset_state()
        return res
    except Exception as exc:
        logger.exception("Failed to delete all proxies")
        raise HTTPException(status_code=500, detail={"status": "error", "message": str(exc)})


@app.post("/report_bad")
def report_bad(req: ReportBadRequest):
    try:
        manager = _get_manager()
        res = manager.mark_bad_connection(req.config_name, req.reason)
        if res.get("status") == "ok":
            return res
        raise HTTPException(status_code=400, detail=res)
    except Exception as exc:
        logger.exception("Failed to report bad connection")
        raise HTTPException(status_code=500, detail={"status": "error", "message": str(exc)})


@app.get("/bad_connections")
def list_bad_connections():
    try:
        manager = _get_manager()
        return manager.list_bad_connections()
    except Exception as exc:
        logger.exception("Failed to list bad connections")
        raise HTTPException(status_code=500, detail={"status": "error", "message": str(exc)})
