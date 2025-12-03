import os
import time
import random
import socket
import logging
from pathlib import Path
from typing import Optional, Dict, Tuple
import json

import requests
import docker
from docker.errors import APIError, DockerException, NotFound

logger = logging.getLogger(__name__)

HEALTH_INDICATORS = [
    "connected",
    "vpn is up",
    "healthy!",
    "openvpn: initialization sequence completed",
]
ERROR_INDICATORS = [
    "auth_failed",
    "fatal",
    "cannot connect",
    "connection refused",
    "tls-error",
]

class VPNManager:
    """Create and validate Gluetun HTTP proxy backed by OpenVPN."""

    def __init__(self,
                 configs_dir: str = "./openvpn",
                 port_min: int = 8887,
                 port_max: int = 20000,
                 health_timeout: int = 30,
                 request_timeout: int = 10,
                 max_attempts: int = 3) -> None:
        self.configs_dir = Path(configs_dir)
        # Enforce allowed port range 8887-20000
        ALLOWED_MIN, ALLOWED_MAX = 8887, 20000
        self.port_min = max(ALLOWED_MIN, int(port_min))
        self.port_max = min(ALLOWED_MAX, int(port_max))
        if self.port_min > self.port_max:
            raise ValueError(f"Invalid port range. Allowed range is {ALLOWED_MIN}-{ALLOWED_MAX}.")
        self.health_timeout = health_timeout
        self.request_timeout = request_timeout
        self.max_attempts = max_attempts
        self.client = docker.from_env()

        # Load runtime config
        self.config_path = Path("./config.json")
        self.runtime = self._load_runtime_config()
        self.vpn_provider = (self.runtime.get("vpn_service_provider") or "custom").lower()
        self.vpn_user = self.runtime.get("openvpn_user")
        self.vpn_pass = self.runtime.get("openvpn_password")

        # Initialize bad connections DB
        self.db_dir = Path("./db")
        self.db_dir.mkdir(exist_ok=True)
        self.bad_db_path = self.db_dir / "bad_connections.json"
        self._ensure_bad_db()
        self.bad_list = set(self._load_bad_list())

        # Prepare ovpn list only if using custom provider
        self.ovpn_files = []
        if self.vpn_provider == "custom":
            if not self.configs_dir.exists():
                raise FileNotFoundError(f"VPN configs directory not found: {self.configs_dir}")
            all_files = [p for p in self.configs_dir.glob("*.ovpn")] + [p for p in self.configs_dir.glob("*.conf")]
            if not all_files:
                raise FileNotFoundError(f"No .ovpn or .conf files found in {self.configs_dir}")
            # Prefer reliable servers (UK, DE, NL, CH) for better connection rates
            preferred = [f for f in all_files if any(x in f.name for x in ['uk', 'de', 'nl', 'ch', 'fr', 'se'])]
            # Filter out bad configs
            self.ovpn_files = [f for f in (preferred if preferred else all_files) if f.name not in self.bad_list]
            if not self.ovpn_files:
                raise FileNotFoundError("All available configs are marked bad; clear bad list or add new configs.")
            logger.info(f"Loaded {len(self.ovpn_files)} configs ({len(preferred)} preferred, {len(self.bad_list)} bad)")

    def create_vpn_proxy(self) -> Dict:
        """Create a validated proxy or return error JSON."""
        attempt = 0
        last_error = None
        logs_tail = []
        container = None

        while attempt < self.max_attempts:
            attempt += 1
            try:
                chosen = random.choice(self.ovpn_files) if self.vpn_provider == "custom" else None
                host_port = self._choose_free_port()
                name = f"vpn-proxy-{int(time.time())}-{random.randint(1000,9999)}"
                if chosen:
                    logger.info(f"Attempt {attempt}: launching {name} using {chosen.name} on port {host_port}")
                else:
                    logger.info(f"Attempt {attempt}: launching {name} (provider {self.vpn_provider}) on port {host_port}")

                container = self._launch_gluetun_container(name=name, ovpn_file=chosen, host_port=host_port)
                if not container:
                    last_error = "container_launch_failed"
                    continue

                healthy, logs_tail = self._wait_for_healthy(container, host_port)
                if not healthy:
                    last_error = "health_timeout"
                    logger.warning("Health check failed; trying restart")
                    if not self._restart_container(container):
                        last_error = "restart_failed"
                    else:
                        healthy, logs_tail = self._wait_for_healthy(container, host_port)
                        if not healthy:
                            last_error = "post_restart_health_timeout"

                if healthy:
                    proxy_url, ip_seen = self._validate_proxy(host_port)
                    if proxy_url and ip_seen:
                        logger.info(f"Proxy validated: {proxy_url} (IP {ip_seen})")
                        # Return 127.0.0.1 for local/API access, user should use server's public IP for external
                        return {
                            "status": "ok",
                            "container_id": container.id,
                            "container_name": name,
                            "proxy_url": f"http://127.0.0.1:{host_port}",
                            "proxy_port": host_port,
                            "ip_seen": ip_seen,
                        }
                    else:
                        last_error = "proxy_validation_failed"
                        logger.warning("Proxy validation failed; attempting restart and revalidate")
                        if self._restart_container(container):
                            healthy, logs_tail = self._wait_for_healthy(container, host_port)
                            if healthy:
                                proxy_url, ip_seen = self._validate_proxy(host_port)
                                if proxy_url and ip_seen:
                                    return {
                                        "status": "ok",
                                        "container_id": container.id,
                                        "container_name": name,
                                        "proxy_url": f"http://127.0.0.1:{host_port}",
                                        "proxy_port": host_port,
                                        "ip_seen": ip_seen,
                                    }
                                else:
                                    last_error = "proxy_validation_failed_after_restart"
                        else:
                            last_error = "restart_failed_before_recreate"

                # If we reach here, recreate with new port
                logger.info("Removing container and retrying with new port/config")
                self._remove_container_safe(container)
                container = None

            except Exception as e:
                last_error = str(e)
                logger.exception("Unhandled error during proxy creation")
                self._remove_container_safe(container)
                container = None

        # Final failure
        return {
            "status": "error",
            "message": str(last_error or "unknown_error"),
        }

    def create_multiple_proxies(self, count: int = 1, sequential: bool = True) -> Dict:
        """Create multiple validated proxies. Returns successes and errors.

        If sequential=True, runs one-by-one to reduce load and improve reliability.
        """
        if count < 1:
            return {"status": "error", "message": "count must be >= 1"}

        results = []
        errors = []

        # Simple sequential implementation for stability; parallel can be added later
        for i in range(count):
            res = self.create_vpn_proxy()
            if res.get("status") == "ok":
                results.append(res)
            else:
                errors.append(res)

        return {
            "status": "partial" if (results and errors) else ("ok" if results else "error"),
            "count_requested": count,
            "count_ok": len(results),
            "count_error": len(errors),
            "proxies": results,
            "errors": errors,
        }

    # Management helpers
    def list_proxies(self) -> Dict:
        try:
            containers = self.client.containers.list(all=True, filters={"ancestor": "qmcgaw/gluetun:latest"})
            items = []
            for c in containers:
                ports = c.attrs.get("NetworkSettings", {}).get("Ports", {})
                http_port = self._extract_host_port(ports)
                items.append({
                    "id": c.id,
                    "name": c.name,
                    "status": c.status,
                    "http_port": http_port,
                })
            return {"status": "ok", "items": items}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def get_proxy(self, name: str) -> Dict:
        try:
            c = self.client.containers.get(name)
            ports = c.attrs.get("NetworkSettings", {}).get("Ports", {})
            http_port = self._extract_host_port(ports)
            return {"status": "ok", "id": c.id, "name": c.name, "state": c.status, "http_port": http_port}
        except NotFound:
            return {"status": "error", "message": "not_found"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def delete_proxy(self, name: str) -> Dict:
        try:
            c = self.client.containers.get(name)
            c.remove(force=True)
            return {"status": "ok", "deleted": name}
        except NotFound:
            return {"status": "error", "message": "not_found"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def delete_all_proxies(self) -> Dict:
        try:
            containers = self.client.containers.list(all=True, filters={"ancestor": "qmcgaw/gluetun:latest"})
            deleted = []
            for c in containers:
                try:
                    c.remove(force=True)
                    deleted.append(c.name)
                except Exception:
                    continue
            return {"status": "ok", "deleted": deleted}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _launch_gluetun_container(self, name: str, ovpn_file: Optional[Path], host_port: int):
        env = {
            "HTTPPROXY": "on",
        }
        # Provider selection
        if self.vpn_provider == "custom" and ovpn_file is not None:
            env["VPN_SERVICE_PROVIDER"] = "custom"
            env["OPENVPN_CUSTOM_CONFIG"] = f"/gluetun/custom/{ovpn_file.name}"
        else:
            # Use given provider (e.g., nordvpn) per config.json
            env["VPN_SERVICE_PROVIDER"] = self.vpn_provider
        # Credentials
        if self.vpn_user:
            env["OPENVPN_USER"] = self.vpn_user
        if self.vpn_pass:
            env["OPENVPN_PASSWORD"] = self.vpn_pass

        # Mount custom configs only when using custom
        volumes = {}
        if self.vpn_provider == "custom" and ovpn_file is not None:
            volumes[str(self.configs_dir.resolve())] = {
                "bind": "/gluetun/custom",
                "mode": "ro",
            }

        ports = {
            "8888/tcp": ("0.0.0.0", host_port),
        }
        try:
            container = self.client.containers.run(
                image="qmcgaw/gluetun:latest",
                name=name,
                cap_add=["NET_ADMIN"],
                devices=["/dev/net/tun:/dev/net/tun"],
                environment=env,
                volumes=volumes,
                ports=ports,
                detach=True,
                restart_policy={"Name": "unless-stopped"},
                network_mode="bridge",
            )
            logger.info(f"Launched container {name}")
            return container
        except (APIError, DockerException) as e:
            logger.error(f"Failed to run container: {e}")
            return None

    def _wait_for_healthy(self, container, host_port: int) -> Tuple[bool, list]:
        start = time.time()
        logs_tail = []
        while time.time() - start < self.health_timeout:
            try:
                container.reload()
                # Try proxy validation instead of log parsing
                proxy_url, ip_seen = self._validate_proxy(host_port)
                if proxy_url and ip_seen:
                    logger.info(f"Proxy healthy and validated: {ip_seen}")
                    return True, logs_tail
            except Exception as e:
                logger.debug(f"Health check attempt failed: {e}")
            time.sleep(3)
        logger.error("Health check timed out")
        return False, logs_tail

    def _restart_container(self, container) -> bool:
        try:
            container.restart(timeout=30)
            logger.info("Container restarted")
            return True
        except Exception as e:
            logger.error(f"Failed to restart container: {e}")
            return False

    def restart_and_check(self, name: str) -> Dict:
        """Restart a proxy container by name and validate via ipify through its HTTP proxy.

        Returns {status: ok, http_port: int, ip_seen: str} on success, or {status: error, message}.
        """
        try:
            c = self.client.containers.get(name)
        except NotFound:
            return {"status": "error", "message": "not_found"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

        ports = c.attrs.get("NetworkSettings", {}).get("Ports", {})
        http_port = self._extract_host_port(ports)
        if http_port is None:
            return {"status": "error", "message": "http_port_not_found"}

        if not self._restart_container(c):
            return {"status": "error", "message": "restart_failed"}

        healthy, _ = self._wait_for_healthy(c, http_port)
        if not healthy:
            return {"status": "error", "message": "health_timeout"}

        proxy_url, ip_seen = self._validate_proxy(http_port)
        if proxy_url and ip_seen:
            return {"status": "ok", "http_port": http_port, "ip_seen": ip_seen}
        return {"status": "error", "message": "proxy_validation_failed"}

    def _remove_container_safe(self, container) -> None:
        if not container:
            return
        try:
            name = getattr(container, "name", "unknown")
            logger.info(f"Removing container {name}")
            container.remove(force=True)
        except Exception as e:
            logger.warning(f"Failed removing container: {e}")

    def _validate_proxy(self, host_port: int) -> Tuple[Optional[str], Optional[str]]:
        proxy = f"http://127.0.0.1:{host_port}"
        try:
            r = requests.get(
                "https://api.ipify.org?format=json",
                proxies={"http": proxy, "https": proxy},
                timeout=self.request_timeout,
            )
            if r.status_code == 200:
                data = r.json()
                ip = data.get("ip")
                if ip:
                    return proxy, ip
        except Exception as e:
            logger.debug(f"Proxy validation error: {e}")
        return None, None

    @staticmethod
    def _extract_host_port(ports: Dict) -> Optional[int]:
        if not ports:
            return None
        preferred_keys = ["8888/tcp"]
        for key in preferred_keys:
            bindings = ports.get(key)
            if bindings:
                host_port = bindings[0].get("HostPort")
                if host_port:
                    try:
                        return int(host_port)
                    except (TypeError, ValueError):
                        continue
        for bindings in ports.values():
            if not bindings:
                continue
            host_port = bindings[0].get("HostPort")
            if host_port:
                try:
                    return int(host_port)
                except (TypeError, ValueError):
                    continue
        return None

    def _choose_free_port(self) -> int:
        for _ in range(50):
            port = random.randint(self.port_min, self.port_max)
            if self._is_port_free(port):
                return port
        # fallback linear scan
        for port in range(self.port_min, self.port_max):
            if self._is_port_free(port):
                return port
        raise RuntimeError("No free port available in configured range")

    @staticmethod
    def _is_port_free(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
                return True
            except OSError:
                return False

    def _load_runtime_config(self) -> Dict:
        try:
            if self.config_path.exists():
                return json.loads(self.config_path.read_text())
            return {}
        except Exception as e:
            logger.warning(f"Failed to read config.json: {e}")
            return {}

    # Bad connection DB management
    def _ensure_bad_db(self) -> None:
        if not self.bad_db_path.exists():
            try:
                self.bad_db_path.write_text(json.dumps({"items": []}, indent=2))
            except Exception as e:
                logger.error(f"Failed to initialize bad DB: {e}")

    def _load_bad_list(self) -> list:
        try:
            data = json.loads(self.bad_db_path.read_text())
            items = data.get("items", [])
            return [x.get("config_name") for x in items if x.get("config_name")]
        except Exception as e:
            logger.warning(f"Failed to read bad DB, defaulting to empty: {e}")
            return []

    def _save_bad_list(self, entries: list) -> bool:
        try:
            self.bad_db_path.write_text(json.dumps({"items": entries}, indent=2))
            return True
        except Exception as e:
            logger.error(f"Failed saving bad DB: {e}")
            return False

    def mark_bad_connection(self, config_name: str, reason: Optional[str] = None) -> Dict:
        try:
            # Load current
            try:
                data = json.loads(self.bad_db_path.read_text())
            except Exception:
                data = {"items": []}
            items = data.get("items", [])
            # Check exists
            if any(i.get("config_name") == config_name for i in items):
                return {"status": "ok", "message": "already_marked", "config_name": config_name}
            items.append({
                "config_name": config_name,
                "reason": reason,
                "timestamp": int(time.time()),
            })
            ok = self._save_bad_list(items)
            if ok:
                return {"status": "ok", "config_name": config_name}
            return {"status": "error", "message": "save_failed"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def list_bad_connections(self) -> Dict:
        try:
            data = json.loads(self.bad_db_path.read_text())
            return {"status": "ok", "items": data.get("items", [])}
        except Exception as e:
            return {"status": "error", "message": str(e)}
