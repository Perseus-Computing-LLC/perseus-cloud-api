"""
mimir_client.py — MCP client wrapper for the Mimir CLI binary.

Maintains a persistent MCP stdio connection to the mimir binary.
Uses the MCP JSON-RPC protocol for tool calls.
"""

import asyncio
import json
import logging
import os
import subprocess
import time
from typing import Any

logger = logging.getLogger("perseus_cloud.mimir")

MIMIR_BINARY = os.getenv(
    "MIMIR_BINARY_PATH",
    "/opt/data/webui/minions/.minions-data/mimir/mimir",
)
MIMIR_DB = os.getenv("MIMIR_DB_PATH", "/opt/data/webui/minions/.minions-data/mimir/mimir.db")
MIMIR_ENCRYPTION_KEY = os.getenv("MIMIR_ENCRYPTION_KEY", "")


class MimirClient:
    """Async wrapper around the Mimir MCP binary via stdio subprocess."""

    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()
        self._connected = False

    async def start(self) -> None:
        """Start the Mimir MCP process and perform handshake."""
        cmd = [MIMIR_BINARY, "serve", "--db", MIMIR_DB]
        if MIMIR_ENCRYPTION_KEY:
            cmd.extend(["--encryption-key", MIMIR_ENCRYPTION_KEY])

        logger.info("Starting Mimir process", extra={"cmd": cmd})

        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # MCP initialize handshake
        init_result, err = await self._call("initialize", {
            "protocolVersion": "2025-06-18",
            "clientInfo": {"name": "perseus-cloud-api", "version": "1.0.0"},
            "capabilities": {},
        })
        if err or not init_result:
            raise RuntimeError(f"Mimir handshake failed: {err}")

        # Send initialized notification
        self._send_notification("notifications/initialized", {})
        self._connected = True
        logger.info("Mimir connected successfully")

    async def stop(self) -> None:
        """Stop the Mimir process."""
        if self._process:
            try:
                self._process.stdin.close()
                self._process.stdout.close()
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None
            self._connected = False

    async def call_tool(self, tool_name: str, arguments: dict) -> tuple[Any, str | None]:
        """Call an MCP tool and return (result, error_string)."""
        async with self._lock:
            result, err = await self._call("tools/call", {
                "name": tool_name,
                "arguments": arguments,
            })
            if err:
                return None, err
            if result is None:
                return None, "no result"

            # MCP tool result wraps content in result.content[0].text (JSON string)
            content = result.get("content", [])
            if content and isinstance(content, list):
                first = content[0]
                if isinstance(first, dict) and "text" in first:
                    try:
                        return json.loads(first["text"]), None
                    except (json.JSONDecodeError, TypeError):
                        return {"text": first["text"]}, None
            return result, None

    async def _call(self, method: str, params: dict) -> tuple[dict | None, str | None]:
        """Send a JSON-RPC request and return the result."""
        if not self._process or self._process.poll() is not None:
            return None, "MCP process not running"

        self._request_id += 1
        req_id = self._request_id
        request = json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        })

        loop = asyncio.get_event_loop()

        try:
            self._process.stdin.write(request + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            return None, f"MCP write failed: {e}"

        # Read response line (run blocking read in executor)
        try:
            line = await loop.run_in_executor(None, self._process.stdout.readline)
            if not line:
                return None, "MCP EOF (process may have crashed)"
            response = json.loads(line)
        except (json.JSONDecodeError, Exception) as e:
            return None, f"MCP read/parse failed: {e}"

        if "error" in response:
            err = response["error"]
            return None, f"MCP error {err.get('code', '')}: {err.get('message', str(err))}"
        return response.get("result"), None

    def _send_notification(self, method: str, params: dict) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        if self._process and self._process.stdin:
            try:
                self._process.stdin.write(msg + "\n")
                self._process.stdin.flush()
            except Exception:
                pass

    @property
    def is_connected(self) -> bool:
        return self._connected and self._process is not None and self._process.poll() is None


# Global singleton
mimir_client = MimirClient()
