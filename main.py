import logging
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from vpn_manager import VPNManager

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(name)s %(message)s')
logger = logging.getLogger("vpnMan")

app = FastAPI(title="vpn_manager", version="1.0.0")

class NewProxyRequest(BaseModel):
    port_min: Optional[int] = 20000
    port_max: Optional[int] = 40000
    health_timeout: Optional[int] = 45
    request_timeout: Optional[int] = 15
    max_attempts: Optional[int] = 5

@app.post("/new_proxy")
def new_proxy(req: NewProxyRequest):
    try:
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

class NewProxiesRequest(BaseModel):
    port_min: Optional[int] = 20000
    port_max: Optional[int] = 40000
    health_timeout: Optional[int] = 45
    request_timeout: Optional[int] = 15
    max_attempts: Optional[int] = 5
    count: int = 1

@app.post("/new_proxies")
def new_proxies(req: NewProxiesRequest):
    try:
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
