import json
import os
import threading
from pathlib import Path
from urllib.parse import urlparse

import docker
import docker.errors
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Cloud NGINX based load balancer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Set NGINX_MODE=true when running locally with docker-compose (nginx sidecar).
# Leave unset / false when deploying to Render, Railway, Fly etc. — FastAPI
# will act as the load balancer itself on the single exposed port.
NGINX_MODE = os.getenv("NGINX_MODE", "false").lower() == "true"

# Persisted node list
DATA_FILE = Path("/app/data/nodes.json")

# Shared volume path written by app, read by nginx (only used in NGINX_MODE)
NGINX_CONF_FILE = Path("/nginx_conf/default.conf")

# nginx container name (only used in NGINX_MODE)
NGINX_CONTAINER_NAME = "nginx_lb"

# Round-robin counter for the built-in /api proxy (used when NGINX_MODE=false)
_rr_lock  = threading.Lock()
_rr_index = 0


def _next_node(nodes: list[str]) -> str:
    global _rr_index
    with _rr_lock:
        node = nodes[_rr_index % len(nodes)]
        _rr_index += 1
    return node


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_nodes() -> list[str]:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return []


def save_nodes(nodes: list[str]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(nodes, indent=2))


def normalize_node(raw: str) -> str:
    """Normalize any input to scheme://host:port (no path, no trailing slash).

    Accepts:  host:port | http://host:port | https://host.domain.com
    Returns:  http://host:3000  or  https://host.domain.com:443
    """
    raw = raw.strip().rstrip("/")
    if "://" not in raw:
        raw = "http://" + raw
    p = urlparse(raw)
    scheme = (p.scheme or "http").lower()
    host   = p.hostname or ""
    port   = p.port or (443 if scheme == "https" else 80)
    return f"{scheme}://{host}:{port}"


def write_nginx_conf(nodes: list[str]) -> None:
    if not NGINX_MODE:
        return  # standalone mode — no nginx to configure
    NGINX_CONF_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not nodes:
        conf = (
            "server {\n"
            "    listen 80;\n"
            "    location / {\n"
            '        return 503 "No backend nodes configured.";\n'
            "    }\n"
            "}\n"
        )
        NGINX_CONF_FILE.write_text(conf)
        return

    # Parse every node
    entries = []  # (scheme, display_host, upstream_host, port)
    for n in nodes:
        raw = n if "://" in n else "http://" + n
        p = urlparse(raw)
        scheme = (p.scheme or "http").lower()
        host   = p.hostname or ""
        upstream_host = "host.docker.internal" if host in ("localhost", "127.0.0.1") else host
        port = p.port or (443 if scheme == "https" else 80)
        entries.append((scheme, host, upstream_host, port))

    # Build nginx conf.
    # We always use resolver + variable-based proxy_pass so that nginx
    # re-resolves DNS on every request — required for Render / Railway / Fly
    # whose IPs change frequently.
    use_ssl   = any(s == "https" for s, _, _, _ in entries)
    ssl_block = (
        "        proxy_ssl_server_name on;\n"
        "        proxy_ssl_verify     off;\n"
    ) if use_ssl else ""

    if len(entries) == 1:
        scheme, display_host, upstream_host, port = entries[0]
        conf = (
            "server {\n"
            "    listen 80;\n"
            "    # re-resolve DNS every 30 s (required for Render / cloud hosts)\n"
            "    resolver 8.8.8.8 8.8.4.4 valid=30s ipv6=off;\n"
            "    resolver_timeout 5s;\n\n"
            f"    set $lb_host  \"{upstream_host}\";\n"
            f"    set $lb_port  \"{port}\";\n"
            f"    set $lb_scheme \"{scheme}\";\n\n"
            "    location / {\n"
            f"        proxy_pass ${{lb_scheme}}://${{lb_host}}:${{lb_port}};\n"
            f"{ssl_block}"
            f"        proxy_set_header Host              {display_host};\n"
            "        proxy_set_header X-Real-IP         $remote_addr;\n"
            "        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;\n"
            "    }\n"
            "}\n"
        )
    else:
        # split_clients selects an integer key 0..N-1;
        # map directives resolve that to host / port / scheme.
        share       = 100 // len(entries)
        split_lines = ""
        host_lines  = ""
        port_lines  = ""
        scheme_lines = ""

        for i, (scheme, display_host, upstream_host, port) in enumerate(entries):
            pct           = "*" if i == len(entries) - 1 else f"{share}%"
            split_lines  += f"    {pct:<6} \"{i}\";\n"
            host_lines   += f"    \"{i}\"   {upstream_host};\n"
            port_lines   += f"    \"{i}\"   {port};\n"
            scheme_lines += f"    \"{i}\"   {scheme};\n"

        dh = entries[0][1]   # default display_host
        duh = entries[0][2]  # default upstream_host
        dp  = entries[0][3]  # default port
        ds  = entries[0][0]  # default scheme

        conf = (
            "split_clients \"${request_id}\" $lb_key {\n"
            f"{split_lines}"
            "}\n\n"
            "map $lb_key $lb_host {\n"
            f"{host_lines}"
            f"    default   {duh};\n"
            "}\n\n"
            "map $lb_key $lb_port {\n"
            f"{port_lines}"
            f"    default   {dp};\n"
            "}\n\n"
            "map $lb_key $lb_scheme {\n"
            f"{scheme_lines}"
            f"    default   {ds};\n"
            "}\n\n"
            "map $lb_key $lb_display_host {\n"
            + "".join(f'    "{i}"   {e[1]};\n' for i, e in enumerate(entries))
            + f"    default   {dh};\n"
            "}\n\n"
            "server {\n"
            "    listen 80;\n"
            "    resolver 8.8.8.8 8.8.4.4 valid=30s ipv6=off;\n"
            "    resolver_timeout 5s;\n\n"
            "    location / {\n"
            "        proxy_pass ${lb_scheme}://${lb_host}:${lb_port};\n"
            f"{ssl_block}"
            "        proxy_set_header Host              $lb_display_host;\n"
            "        proxy_set_header X-Real-IP         $remote_addr;\n"
            "        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;\n"
            "    }\n"
            "}\n"
        )

    NGINX_CONF_FILE.write_text(conf)


def _exec_nginx_reload() -> None:
    """Reload nginx container. No-op in standalone (non-NGINX_MODE) deployments."""
    if not NGINX_MODE:
        return
    try:
        client    = docker.from_env()
        container = client.containers.get(NGINX_CONTAINER_NAME)
        # test config first so a bad conf never kills the running nginx
        test = container.exec_run("nginx -t")
        if test.exit_code != 0:
            raise HTTPException(
                status_code=500,
                detail=f"nginx config test failed: {test.output.decode(errors='replace')}",
            )
        result = container.exec_run("nginx -s reload")
        if result.exit_code != 0:
            raise HTTPException(
                status_code=500,
                detail=f"nginx reload failed: {result.output.decode(errors='replace')}",
            )
    except docker.errors.NotFound:
        raise HTTPException(
            status_code=404,
            detail=f"Container '{NGINX_CONTAINER_NAME}' not found — is it running?",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class NodeIn(BaseModel):
    url: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def index(request: Request):
    nodes = load_nodes()
    return templates.TemplateResponse(
        "clients.html", {"request": request, "nodes": nodes}
    )


@app.get("/nodes")
def get_nodes():
    return load_nodes()


@app.post("/nodes", status_code=201)
def add_node(body: NodeIn):
    if not body.url.strip():
        raise HTTPException(status_code=400, detail="URL cannot be empty")
    url = normalize_node(body.url)
    nodes = load_nodes()
    if url in nodes:
        raise HTTPException(status_code=409, detail="Node already exists")
    nodes.append(url)
    save_nodes(nodes)
    write_nginx_conf(nodes)
    _exec_nginx_reload()
    return {"nodes": nodes}


@app.delete("/nodes/{node_index}")
def remove_node(node_index: int):
    nodes = load_nodes()
    if node_index < 0 or node_index >= len(nodes):
        raise HTTPException(status_code=404, detail="Node index out of range")
    nodes.pop(node_index)
    save_nodes(nodes)
    write_nginx_conf(nodes)
    _exec_nginx_reload()
    return {"nodes": nodes}


@app.post("/reload")
def reload_nginx():
    """Regenerate nginx.conf and send SIGHUP to the nginx container only."""
    nodes = load_nodes()
    write_nginx_conf(nodes)
    _exec_nginx_reload()
    return {"status": "ok", "message": "nginx reloaded successfully"}


@app.get("/logs")
def get_logs():
    """Return the last 200 lines of nginx logs (NGINX_MODE) or app container logs."""
    if not NGINX_MODE:
        return JSONResponse({"logs": "(nginx not running — standalone mode)"})
    try:
        client = docker.from_env()
        container = client.containers.get(NGINX_CONTAINER_NAME)
        raw = container.logs(tail=200, timestamps=True)
        return JSONResponse({"logs": raw.decode(errors="replace")})
    except docker.errors.NotFound:
        raise HTTPException(
            status_code=404,
            detail=f"Container '{NGINX_CONTAINER_NAME}' not found",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# /api/ — built-in round-robin proxy (active in both modes)
# ---------------------------------------------------------------------------

_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def proxy_api(path: str, request: Request):
    """
    Round-robin proxy. Always active — works in both NGINX_MODE and standalone.
    In NGINX_MODE:   hit :80 for nginx LB  OR  :8000/api/ for direct FastAPI LB
    Standalone mode: hit :8000/api/ — this is your only LB endpoint
    """
    nodes = load_nodes()
    if not nodes:
        raise HTTPException(status_code=503, detail="No backend nodes configured.")

    node = _next_node(nodes)
    # strip trailing port if it was stored as scheme://host:port
    parsed = urlparse(node if "://" in node else "http://" + node)
    base   = f"{parsed.scheme}://{parsed.hostname}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"
    qs     = request.url.query
    target = f"{base}/{path}" + (f"?{qs}" if qs else "")

    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    fwd_headers["Host"]             = parsed.hostname
    fwd_headers["X-Forwarded-For"]  = request.client.host if request.client else "unknown"
    fwd_headers["X-Forwarded-Host"] = request.headers.get("host", "")

    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            resp = await client.request(
                method=request.method, url=target,
                headers=fwd_headers, content=body, follow_redirects=True,
            )
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail=f"Cannot reach node: {node}")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail=f"Node timed out: {node}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP}
    resp_headers["X-Served-By"] = node
    return Response(
        content=resp.content, status_code=resp.status_code,
        headers=resp_headers, media_type=resp.headers.get("content-type"),
    )
