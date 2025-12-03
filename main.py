import logging
from typing import Optional

from fastapi import FastAPI, HTTPException
from threading import Thread
import uuid
import time
import requests
import docker

# Simple in-memory job store
JOBS = {}
from pydantic import BaseModel

from vpn_manager import VPNManager

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(name)s %(message)s')
logger = logging.getLogger("vpnMan")

app = FastAPI(title="vpn_manager", version="1.0.0")

class NewProxyRequest(BaseModel):
    port_min: int = 8887
    port_max: int = 20000
    health_timeout: int = 45
    request_timeout: int = 15
    max_attempts: int = 5

@app.post("/new_proxy")
def new_proxy(req: Optional[NewProxyRequest] = None):
    try:
        if req is None:
            req = NewProxyRequest()
        mgr = VPNManager(
            port_min=req.port_min,
            port_max=req.port_max,
            health_timeout=req.health_timeout,
            request_timeout=req.request_timeout,
            max_attempts=req.max_attempts,
        )
        result = mgr.create_vpn_proxy()
        if result.get("status") == "ok":
            return result
        raise HTTPException(status_code=502, detail=result)
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail={"status": "error", "message": str(e)})
    except Exception as e:
        logger.exception("Failed to create new proxy")
        raise HTTPException(status_code=500, detail={"status": "error", "message": str(e)})

@app.post("/new_proxy_async")
def new_proxy_async(req: Optional[NewProxyRequest] = None):
    try:
        if req is None:
            req = NewProxyRequest()
        job_id = str(uuid.uuid4())
        JOBS[job_id] = {"status": "queued", "result": None, "created_at": int(time.time())}

        def worker():
            try:
                mgr = VPNManager(
                    port_min=req.port_min,
                    port_max=req.port_max,
                    health_timeout=req.health_timeout,
                    request_timeout=req.request_timeout,
                    max_attempts=req.max_attempts,
                )
                JOBS[job_id]["status"] = "running"
                res = mgr.create_vpn_proxy()
                JOBS[job_id]["result"] = res
                JOBS[job_id]["status"] = "done" if res.get("status") == "ok" else "error"
            except Exception as e:
                JOBS[job_id]["result"] = {"status": "error", "message": str(e)}
                JOBS[job_id]["status"] = "error"

        Thread(target=worker, daemon=True).start()
        return {"status": "accepted", "job_id": job_id}
    except Exception as e:
        logger.exception("Failed to enqueue new proxy job")
        raise HTTPException(status_code=500, detail={"status": "error", "message": str(e)})

@app.get("/job/{job_id}")
def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"status": "error", "message": "job_not_found"})
    return {"status": job["status"], "result": job["result"], "job_id": job_id}

# --- Background sweeper for unhealthy proxies ---

def _validate_port(port: int, timeout: int = 8) -> bool:
    proxy = f"http://127.0.0.1:{port}"
    try:
        r = requests.get(
            "https://api.ipify.org?format=json",
            proxies={"http": proxy, "https": proxy},
            timeout=timeout,
        )
        return r.status_code == 200 and bool(r.json().get("ip"))
    except Exception:
        return False

def _sweep_once() -> dict:
    client = docker.from_env()
    removed = []
    healthy = []
    errors = []
    try:
        containers = client.containers.list(all=True, filters={"ancestor": "qmcgaw/gluetun:latest"})
        for c in containers:
            try:
                ports = c.attrs.get("NetworkSettings", {}).get("Ports", {})
                mapping = ports.get("8888/tcp")
                host_port = None
                if mapping:
                    host_port = mapping[0].get("HostPort")
                if not host_port:
                    # No http proxy exposed -> remove (unusable)
                    c.remove(force=True)
                    removed.append(c.name)
                    continue
                port_int = int(host_port)
                if _validate_port(port_int):
                    healthy.append({"name": c.name, "port": port_int})
                else:
                    c.remove(force=True)
                    removed.append(c.name)
            except Exception as e:
                errors.append({"name": getattr(c, "name", "unknown"), "error": str(e)})
        return {"status": "ok", "checked": len(containers), "healthy": healthy, "removed": removed, "errors": errors}
    except Exception as e:
        return {"status": "error", "message": str(e), "healthy": healthy, "removed": removed, "errors": errors}

def _sweeper_loop(interval_seconds: int = 120):
    while True:
        try:
            res = _sweep_once()
            if res.get("status") != "ok":
                logger.warning(f"sweeper error: {res}")
            else:
                logger.info(f"sweeper: checked={res['checked']} healthy={len(res['healthy'])} removed={len(res['removed'])}")
        except Exception:
            logger.exception("sweeper loop failure")
        time.sleep(interval_seconds)

@app.on_event("startup")
def start_sweeper():
    try:
        t = Thread(target=_sweeper_loop, args=(120,), daemon=True)
        t.start()
        logger.info("Unhealthy proxy sweeper started (every 120s)")
    except Exception:
        logger.exception("failed to start sweeper thread")

class NewProxiesRequest(BaseModel):
    port_min: int = 8887
    port_max: int = 20000
    health_timeout: int = 45
    request_timeout: int = 15
    max_attempts: int = 5
    count: int = 1

@app.post("/new_proxies")
def new_proxies(req: Optional[NewProxiesRequest] = None):
    try:
        if req is None:
            req = NewProxiesRequest()
        mgr = VPNManager(
            port_min=req.port_min,
            port_max=req.port_max,
            health_timeout=req.health_timeout,
            request_timeout=req.request_timeout,
            max_attempts=req.max_attempts,
        )
        result = mgr.create_multiple_proxies(count=req.count)
        if result.get("status") in ("ok", "partial"):
            return result
        raise HTTPException(status_code=502, detail=result)
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail={"status": "error", "message": str(e)})
    except Exception as e:
        logger.exception("Failed to create multiple proxies")
        raise HTTPException(status_code=500, detail={"status": "error", "message": str(e)})

@app.get("/proxies")
def list_proxies():
    try:
        mgr = VPNManager()
        return mgr.list_proxies()
    except Exception as e:
        logger.exception("Failed to list proxies")
        raise HTTPException(status_code=500, detail={"status": "error", "message": str(e)})

@app.get("/proxy/{name}")
def get_proxy(name: str):
    try:
        mgr = VPNManager()
        res = mgr.get_proxy(name)
        if res.get("status") == "ok":
            return res
        raise HTTPException(status_code=404, detail=res)
    except Exception as e:
        logger.exception("Failed to get proxy")
        raise HTTPException(status_code=500, detail={"status": "error", "message": str(e)})

@app.post("/proxy/{name}/restart_and_check")
def restart_and_check(name: str):
    """Restart the proxy container and validate via ipify through its HTTP proxy.

    Returns status ok with ip_seen on success, or error details.
    """
    try:
        mgr = VPNManager()
        res = mgr.restart_and_check(name)
        if res.get("status") == "ok":
            return res
        raise HTTPException(status_code=502, detail=res)
    except Exception as e:
        logger.exception("Failed to restart and check proxy")
        raise HTTPException(status_code=500, detail={"status": "error", "message": str(e)})

@app.post("/maintenance/sweep")
def maintenance_sweep():
    try:
        res = _sweep_once()
        if res.get("status") == "ok":
            return res
        raise HTTPException(status_code=500, detail=res)
    except Exception as e:
        logger.exception("Failed maintenance sweep")
        raise HTTPException(status_code=500, detail={"status": "error", "message": str(e)})

@app.delete("/proxy/{name}")
def delete_proxy(name: str):
    try:
        mgr = VPNManager()
        res = mgr.delete_proxy(name)
        if res.get("status") == "ok":
            return res
        raise HTTPException(status_code=404, detail=res)
    except Exception as e:
        logger.exception("Failed to delete proxy")
        raise HTTPException(status_code=500, detail={"status": "error", "message": str(e)})

@app.delete("/proxies")
def delete_all_proxies():
    try:
        mgr = VPNManager()
        return mgr.delete_all_proxies()
    except Exception as e:
        logger.exception("Failed to delete all proxies")
        raise HTTPException(status_code=500, detail={"status": "error", "message": str(e)})

class ReportBadRequest(BaseModel):
    config_name: str
    reason: Optional[str] = None

@app.post("/report_bad")
def report_bad(req: ReportBadRequest):
    try:
        mgr = VPNManager()
        res = mgr.mark_bad_connection(req.config_name, req.reason)
        if res.get("status") == "ok":
            return res
        raise HTTPException(status_code=400, detail=res)
    except Exception as e:
        logger.exception("Failed to report bad connection")
        raise HTTPException(status_code=500, detail={"status": "error", "message": str(e)})

@app.get("/bad_connections")
def list_bad_connections():
    try:
        mgr = VPNManager()
        return mgr.list_bad_connections()
    except Exception as e:
        logger.exception("Failed to list bad connections")
        raise HTTPException(status_code=500, detail={"status": "error", "message": str(e)})
