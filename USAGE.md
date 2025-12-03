# VPN Proxy Manager - Quick Usage Guide

## Start the API Server

```bash
cd /home/kali/Desktop/project/vpnMan
. .venv/bin/activate
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

## Create a Proxy (Async - Recommended)

**No parameters needed** - all defaults are set:

```bash
curl -X POST http://localhost:8000/new_proxy_async \
  -H 'Content-Type: application/json' \
  -d '{}'
```

Response:
```json
{"status":"accepted","job_id":"<uuid>"}
```

## Check Job Status

```bash
curl http://localhost:8000/job/<job_id>
```

Response when done:
```json
{
  "status": "done",
  "result": {
    "status": "ok",
    "container_id": "...",
    "container_name": "vpn-proxy-...",
    "proxy_url": "http://127.0.0.1:17967",
    "proxy_port": 17967,
    "ip_seen": "45.84.137.248"
  }
}
```

## Test Your Proxy

```bash
curl -x http://127.0.0.1:<proxy_port> https://api.ipify.org?format=json
```

## List All Proxies

```bash
curl http://localhost:8000/proxies
```

## Get Specific Proxy

```bash
curl http://localhost:8000/proxy/<container_name>
```

## Restart + Validate Proxy

Restart a proxy's container and verify it's ready (via ipify):

```bash
curl -X POST http://localhost:8000/proxy/<container_name>/restart_and_check
```

Example response:
```json
{
  "status": "ok",
  "http_port": 17967,
  "ip_seen": "45.84.137.248"
}
```

To test after restart:
```bash
curl -x http://127.0.0.1:<http_port> https://api.ipify.org?format=json
```

## Delete Proxy

```bash
curl -X DELETE http://localhost:8000/proxy/<container_name>
```

## Port Range

All proxies are created within ports **8887 - 20000** as configured.

## Optional Parameters

If you need custom settings:

```bash
curl -X POST http://localhost:8000/new_proxy_async \
  -H 'Content-Type: application/json' \
  -d '{
    "port_min": 8887,
    "port_max": 20000,
    "health_timeout": 45,
    "request_timeout": 15,
    "max_attempts": 5
  }'
```

## Health Check Method

The system validates proxies by making actual HTTP requests through them (like `curl -x http://127.0.0.1:PORT https://api.ipify.org`) instead of parsing Docker logs. This ensures proxies are truly functional before returning success.

## Error Responses

Errors are clean and don't include verbose logs:

```json
{
  "status": "error",
  "message": "health_timeout"
}
```

## External Access

For external access (e.g., from your local machine to Vultr instance):
- Replace `127.0.0.1` with your server's public IP
- Keep the same `proxy_port`
- Example: `http://45.76.248.249:17967`

Restart + validate from another machine:
```bash
curl -X POST http://<server-ip>:8000/proxy/<container_name>/restart_and_check
curl -x http://<server-ip>:<http_port> https://api.ipify.org?format=json
```
