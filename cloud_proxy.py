"""
Perseus Cloud API proxy — mounts the API at /api/cloud/* on the Hermes WebUI server.
Patched into /app/server.py Handler class.
"""
import http.client
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)
PROXY_TARGET = "localhost:8080"
PROXY_PREFIX = "/api/cloud"


def proxy_request(handler, parsed) -> bool:
    """Proxy /api/cloud/* requests to Perseus Cloud API.
    Returns True if request was proxied, False if not a cloud path.
    """
    path = parsed.path
    if not path.startswith(PROXY_PREFIX):
        return False

    target_path = path[len(PROXY_PREFIX):]
    if not target_path:
        target_path = "/"
    
    if parsed.query:
        target_path += "?" + parsed.query
    
    content_length = int(handler.headers.get("Content-Length", 0))
    body = handler.rfile.read(content_length) if content_length > 0 else None
    
    conn = http.client.HTTPConnection(PROXY_TARGET, timeout=30)
    try:
        conn.request(
            handler.command,
            target_path,
            body=body,
            headers={
                "Content-Type": handler.headers.get("Content-Type", "application/json"),
                "X-API-Key": handler.headers.get("X-API-Key", ""),
                "Authorization": handler.headers.get("Authorization", ""),
                "Cookie": handler.headers.get("Cookie", ""),
                "Accept": "application/json",
            },
        )
        resp = conn.getresponse()
        resp_body = resp.read()
        
        handler.send_response(resp.status)
        for key, value in resp.getheaders():
            if key.lower() not in ("transfer-encoding", "connection", "server", "date"):
                handler.send_header(key, value)
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Content-Length", str(len(resp_body)))
        handler.end_headers()
        handler.wfile.write(resp_body)
    except Exception:
        try:
            handler.send_response(502)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(b'{"error":"cloud_api_unreachable"}')
        except Exception:
            pass
    finally:
        conn.close()
    
    return True
