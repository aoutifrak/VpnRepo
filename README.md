# VPN Proxy Manager

Automated VPN proxy creation and management using Gluetun containers. Designed for Vultr and cloud deployments with public IP support.

## Features

- ✅ Create HTTP proxies backed by VPN connections  
- ✅ Custom OpenVPN config support
- ✅ Bad connection tracking database
- ✅ Binds to 0.0.0.0 for public IP usage
- ✅ REST API for full proxy lifecycle
- ✅ Automatic health validation
- ✅ Multiple proxy creation

## Quick Start

### 1. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

Edit `config.json`:

```json
{
  "vpn_service_provider": "custom",
  "openvpn_user": "your-username",
  "openvpn_password": "your-password"
}
```

**For NordVPN:** Use service credentials from https://my.nordaccount.com/dashboard/nordvpn/

### 3. Add Configs

Place `.ovpn` files in `./openvpn/` directory.

### 4. Run

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

## API Endpoints

### Create Proxy

```bash
curl -X POST http://localhost:8000/new_proxy
```

Returns:
```json
{
  "status": "ok",
  "proxy_url": "http://0.0.0.0:20015",
  "ip_seen": "185.161.203.184"
}
```

### Create Multiple

```bash
curl -X POST http://localhost:8000/new_proxies -d '{"count":3}'
```

### List Proxies

```bash
curl http://localhost:8000/proxies
```

### Report Bad Config

```bash
curl -X POST http://localhost:8000/report_bad \
  -d '{"config_name":"server.ovpn","reason":"auth_failure"}'
```

### View Bad List

```bash
curl http://localhost:8000/bad_connections
```

## Test Proxy

```bash
curl -x http://127.0.0.1:20015 https://api.ipify.org
```

## Vultr Deployment

### Setup

```bash
sudo apt update && sudo apt install -y docker.io
sudo systemctl enable --now docker
sudo mkdir -p /opt/vpnMan && cd /opt/vpnMan
# Upload files, then:
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Systemd Service

Create `/etc/systemd/system/vpnman.service`:

```ini
[Unit]
Description=VPN Manager
After=docker.service
Requires=docker.service

[Service]
WorkingDirectory=/opt/vpnMan
Environment=PATH=/opt/vpnMan/.venv/bin:/usr/bin
ExecStart=/opt/vpnMan/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always
User=root

[Install]
WantedBy=multi-user.target
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vpnman
```

### Firewall

Open ports `8000` (API) and `20000-40000` (proxies).

## Configuration

| Param | Default | Description |
|-------|---------|-------------|
| port_min | 20000 | Min proxy port |
| port_max | 40000 | Max proxy port |
| health_timeout | 45 | Connection timeout (s) |
| max_attempts | 5 | Retry count |

## Troubleshooting

**Auth failures:** Verify NordVPN service credentials (not account password) in `config.json`.

**No connections:** Mark bad servers via `/report_bad`. System auto-prefers EU servers.

**Logs:**
```bash
docker logs <container_name>
sudo journalctl -u vpnman -f
```

## Architecture

- FastAPI: REST API
- Gluetun: VPN containers
- Bad-DB: `db/bad_connections.json`
- Config: `config.json`
- Servers: `openvpn/` directory

## License

MIT
# VpnRepo
