from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import hashlib
from pathlib import Path


class IPCError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def ipc_socket_path(data_dir: Path) -> Path:
    """Keep Unix socket under macOS' short sockaddr_un limit."""
    digest = hashlib.sha256(str(Path(data_dir).resolve()).encode()).hexdigest()[:20]
    return Path("/tmp") / f"ecourse-{digest}.sock"


async def _read_message(reader: asyncio.StreamReader) -> dict:
    size = int.from_bytes(await reader.readexactly(4), "big")
    if size < 2 or size > 1_048_576:
        raise IPCError("invalid_request", "IPC 訊息大小無效。")
    return json.loads(await reader.readexactly(size))


async def _write_message(writer: asyncio.StreamWriter, payload: dict) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    writer.write(len(encoded).to_bytes(4, "big") + encoded)
    await writer.drain()


class WorkerIPCClient:
    def __init__(self, socket_path: Path, token: str, timeout: float = 3.0):
        self.socket_path, self.token, self.timeout = Path(socket_path), token, timeout

    async def request(self, action: str, *, timeout: float | None = None, **payload):
        request_timeout = self.timeout if timeout is None else timeout
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(self.socket_path), request_timeout)
            try:
                await _write_message(writer, {"token": self.token, "action": action, **payload})
                response = await asyncio.wait_for(_read_message(reader), request_timeout)
            finally:
                writer.close(); await writer.wait_closed()
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
            raise IPCError("worker_unavailable", "背景 worker 無法連線。") from exc
        if not response.get("ok"):
            raise IPCError(response.get("error_code", "worker_error"), response.get("message", "worker 拒絕請求。"))
        return response

    async def challenge_image(self, owner_id, attempt_id, challenge_id):
        response = await self.request("challenge.get", owner_id=owner_id, attempt_id=attempt_id, challenge_id=challenge_id)
        return base64.b64decode(response["image_b64"], validate=True)

    async def answer(self, owner_id, attempt_id, challenge_id, answer, request_id):
        return await self.request("challenge.answer", owner_id=owner_id, attempt_id=attempt_id, challenge_id=challenge_id, answer=answer, request_id=request_id)

    async def refresh(self, owner_id, attempt_id, challenge_id):
        return await self.request("challenge.refresh", owner_id=owner_id, attempt_id=attempt_id, challenge_id=challenge_id)

    async def open_platform(self, owner_id, platform, target_url):
        return await self.request(
            "platform.open", timeout=90, owner_id=owner_id,
            platform=platform, target_url=target_url,
        )

    async def prepare_platform_handoff(self, owner_id, platform, target_url):
        return await self.request(
            "platform.prepare_handoff", timeout=45, owner_id=owner_id,
            platform=platform, target_url=target_url,
        )

    async def export_platform_cookies(self, owner_id, platform):
        return await self.request(
            "platform.export_cookies", timeout=10,
            owner_id=owner_id, platform=platform,
        )


class WorkerIPCServer:
    def __init__(self, socket_path: Path, token: str, handler):
        self.socket_path, self.token, self.handler = Path(socket_path), token, handler
        self.server = None

    async def start(self):
        self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self._serve, self.socket_path)
        os.chmod(self.socket_path, stat.S_IRUSR | stat.S_IWUSR)
        return self

    async def close(self):
        if self.server:
            self.server.close(); await self.server.wait_closed()
        self.socket_path.unlink(missing_ok=True)

    async def _serve(self, reader, writer):
        try:
            request = await _read_message(reader)
            if request.pop("token", None) != self.token:
                raise IPCError("forbidden", "服務 token 不正確。")
            action = request.pop("action", "")
            if action not in {
                "challenge.get", "challenge.answer", "challenge.refresh",
                "platform.open", "platform.prepare_handoff", "platform.export_cookies",
            }:
                raise IPCError("invalid_action", "不支援的 IPC 操作。")
            response = await self.handler(action, request)
            await _write_message(writer, {"ok": True, **response})
        except (IPCError, ValueError, KeyError, json.JSONDecodeError) as exc:
            code = exc.code if isinstance(exc, IPCError) else "invalid_request"
            await _write_message(writer, {"ok": False, "error_code": code, "message": str(exc)})
        finally:
            writer.close(); await writer.wait_closed()
