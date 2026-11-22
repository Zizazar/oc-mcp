"""MCP server and OpenComputers HTTP bridge for local and HTTPS setups."""

import argparse
import asyncio
import base64
import hmac
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote_from_bytes, unquote_to_bytes, urlsplit

import uvicorn
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl, Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response


ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
NAME = re.compile(r"[a-z][a-z0-9_-]{0,23}")
ADDRESS = re.compile(rb"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
FILE_LIMIT = 16_384
FRAME_LIMIT = 131_072
POLL_SECONDS = 0.7
ONLINE_SECONDS = 12
FilePath = Annotated[str, Field(min_length=1, max_length=512)]
Key = Literal["enter", "tab", "backspace", "delete", "escape", "up", "down", "left", "right", "home", "end", "ctrl+c"]


def pack(*fields: str | bytes) -> bytes:
    encoded = (quote_from_bytes(value.encode() if isinstance(value, str) else value, safe="/ ") for value in fields)
    return "\t".join(encoded).encode("ascii")


def unpack(frame: bytes) -> list[bytes]:
    return [unquote_to_bytes(field) for field in frame.split(b"\t")]


def as_text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def flag(data: bytes) -> bool:
    return data == b"1"


def access_key() -> bytes:
    path = Path(os.environ.get("OC_MCP_STATE_DIR", ROOT)) / ".oc-mcp-key"
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
            temporary_key = Path(file.name)
            file.write(secrets.token_hex(32).encode("ascii"))
        try:
            path.hardlink_to(temporary_key)
        except FileExistsError:
            pass
        finally:
            temporary_key.unlink()
        key = path.read_bytes()
    key = key.strip()
    if not re.fullmatch(rb"[0-9a-f]{64}", key):
        raise SystemExit(f"Invalid key file: {path}")
    return key


def token(key: bytes, purpose: str, length: int = 32) -> str:
    return hmac.digest(key, purpose.encode(), "sha256").hex()[:length]


def mcp_path(key: bytes) -> str:
    return f"/mcp/{token(key, 'mcp', 64)}"


def mcp_bearer_token(key: bytes) -> str:
    return token(key, "mcp/bearer", 64)


def public_agent_token(key: bytes, url: str, purpose: str, name: str) -> str:
    # Keep existing relay installer and agent URLs usable on the same domain.
    return token(key, f"relay/{url}/{purpose}/{name}")


def https_origin(value: str) -> str:
    value = value.strip().rstrip("/")
    if not re.fullmatch(r"https://[a-z0-9.-]+(?::[0-9]{1,5})?", value, re.IGNORECASE):
        raise ValueError("use an HTTPS origin, such as https://oc.example.com")
    if urlsplit(value).port == 0:
        raise ValueError("the HTTPS origin cannot use port 0")
    return value


def valid_token(value: str, expected: str) -> bool:
    return value.isascii() and hmac.compare_digest(value, expected)


class McpTokenVerifier:
    def __init__(self, key: bytes):
        self.expected = mcp_bearer_token(key)

    async def verify_token(self, token: str) -> AccessToken | None:
        if valid_token(token.lstrip(" "), self.expected):
            return AccessToken(token=self.expected, client_id="oc-mcp", scopes=[])
        return None


def installer_command(url: str) -> str:
    return f"wget -f {url} /tmp/oc-mcp.lua && /tmp/oc-mcp.lua install"


def installer_script(agent: str, endpoint: str, name: str, idle_delay: float = 0.1) -> str:
    return f'local endpoint, computerName, idleDelay = "{endpoint}", "{name}", {idle_delay}\n' + agent


@dataclass
class Job:
    id: str
    frame: bytes
    result: asyncio.Future[list[bytes]]
    timeout: int
    sent: bool = False


@dataclass
class Computer:
    id: str
    name: str
    last_seen: float
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    job: Job | None = None

    def describe(self) -> dict:
        elapsed = time.monotonic() - self.last_seen
        grace = self.job.timeout if self.job and self.job.sent else 0
        online = elapsed <= ONLINE_SECONDS + grace
        return {
            "id": self.id, "name": self.name, "online": online,
            "busy": online and self.job is not None, "last_seen_seconds_ago": round(elapsed, 1),
        }


class Bridge:
    def __init__(self, key: bytes, port: int, public_url: str | None = None):
        self.key = key
        self.public_url = public_url
        self.url = public_url or f"http://{HOST}:{port}"
        self.mcp_path = "/mcp" if public_url else mcp_path(key)
        self.computers: dict[str, Computer] = {}
        self.agent = (ROOT / "oc-mcp.lua").read_text(encoding="utf-8")

    def credential(self, purpose: str, name: str) -> str:
        if self.public_url:
            return public_agent_token(self.key, self.public_url, purpose, name)
        return token(self.key, f"{purpose}/{name}")

    def install_command(self, name: str) -> str:
        url = f"{self.url}/i/{name}/{self.credential('install', name)}"
        return installer_command(url)

    async def install(self, request: Request) -> Response:
        name = request.path_params["name"]
        if not NAME.fullmatch(name) or not valid_token(request.path_params["token"], self.credential("install", name)):
            return PlainTextResponse("Installer not found", status_code=404)
        endpoint = f"{self.url}/a/{name}/{self.credential('agent', name)}"
        idle_delay = 2 if self.public_url else 0.1
        return PlainTextResponse(installer_script(self.agent, endpoint, name, idle_delay), headers={"Cache-Control": "no-store"})

    async def poll(self, request: Request) -> Response:
        name = request.path_params["name"]
        if not NAME.fullmatch(name) or not valid_token(request.path_params["token"], self.credential("agent", name)):
            return PlainTextResponse("Unknown agent", status_code=404)
        frame = bytearray()
        async for chunk in request.stream():
            if len(frame) + len(chunk) > FRAME_LIMIT:
                return PlainTextResponse("Frame too large", status_code=413)
            frame.extend(chunk)
        address, *result = unpack(bytes(frame))
        if not ADDRESS.fullmatch(address) or (result and (len(result) < 2 or result[1] not in (b"ok", b"error"))):
            return PlainTextResponse("Invalid frame", status_code=400)

        computer_id = address.decode("ascii")
        computer = self.computers.get(computer_id)
        if computer is None:
            computer = self.computers[computer_id] = Computer(computer_id, name, time.monotonic())
        computer.last_seen = time.monotonic()
        computer.name = name
        job = computer.job
        if result and job and job.sent and result[0] == job.id.encode():
            computer.job = None
            if not job.result.done():
                job.result.set_result(result[1:])
        elif not result and job and job.sent and job.result.done():
            # A fresh poll after a timeout means this agent is ready for another job.
            computer.job = None

        if computer.job is None:
            computer.wake.clear()
            try:
                await asyncio.wait_for(computer.wake.wait(), POLL_SECONDS)
            except TimeoutError:
                pass
        computer.last_seen = time.monotonic()
        job = computer.job
        if job and not job.sent:
            job.sent = True
            return PlainTextResponse(job.frame, headers={"Cache-Control": "no-store"})
        return PlainTextResponse("idle", headers={"Cache-Control": "no-store"})

    def resolve(self, target: str) -> Computer:
        target = target.strip().lower()
        computer = self.computers.get(target)
        if computer is None:
            matches = [item for item in self.computers.values() if item.name == target]
            if len(matches) > 1:
                ids = ", ".join(item.id for item in matches)
                raise ValueError(f"Several computers are named {target!r}. Use a full ID: {ids}")
            if not matches:
                raise ValueError(f"Unknown computer {target!r}. Call list_computers to see names and IDs.")
            computer = matches[0]
        if not computer.describe()["online"]:
            raise ValueError(f"{computer.name} ({computer.id}) is offline. Check the computer, chunk and bridge.")
        return computer

    async def call(self, target: str, operation: str, *args: str | bytes, timeout: int = 12) -> tuple[dict, list[bytes]]:
        computer = self.resolve(target)
        if computer.job is not None:
            raise ValueError(f"{computer.name} is busy. Try again after it finishes or polls again.")
        job_id = secrets.token_hex(8)
        job = Job(job_id, pack(job_id, operation, str(timeout), *args), asyncio.get_running_loop().create_future(), timeout)
        computer.job = job
        computer.wake.set()
        try:
            status, *values = await asyncio.wait_for(job.result, timeout + ONLINE_SECONDS)
        except TimeoutError as error:
            detail = "The operation may have run. Check its effects before retrying." if job.sent else "The computer did not receive the operation."
            raise ValueError(f"{computer.name} timed out. {detail}") from error
        finally:
            if computer.job is job and not job.sent:
                computer.job = None
        if status != b"ok":
            raise ValueError(as_text(values[0]) if values else "OpenOS operation failed")
        return {"id": computer.id, "name": computer.name}, values


def create_app(bridge: Bridge):
    allowed_hosts = [f"{HOST}:*"]
    auth = None
    token_verifier = None
    if bridge.public_url:
        origin = urlsplit(bridge.public_url)
        allowed_hosts = [origin.netloc.lower()]
        if origin.port in (None, 443) and origin.hostname:
            allowed_hosts = [origin.hostname, f"{origin.hostname}:443"]
        auth = AuthSettings(issuer_url=AnyHttpUrl(bridge.public_url), resource_server_url=None)
        token_verifier = McpTokenVerifier(bridge.key)

    mcp = FastMCP(
        "OpenComputers",
        instructions=("Control installed OpenComputers PCs in the local Minecraft world. Start with list_computers. "
                      "Use the user-selected name or full ID as target on every call. "
                      "Commands run in OpenOS. Filesystem paths and screen output belong to that in-game computer."),
        streamable_http_path=bridge.mcp_path,
        stateless_http=True,
        json_response=True,
        auth=auth,
        token_verifier=token_verifier,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=[],
        ),
    )
    mcp.custom_route("/i/{name}/{token}", ["GET"])(bridge.install)
    mcp.custom_route("/a/{name}/{token}", ["POST"])(bridge.poll)

    @mcp.custom_route("/healthz", ["GET"])
    async def health(_request: Request) -> Response:
        return PlainTextResponse("ok\n")

    @mcp.tool()
    async def list_computers() -> list[dict[str, Any]]:
        """List connected and recently seen in-game computers with names, stable IDs and online/busy state."""
        return [computer.describe() for computer in sorted(bridge.computers.values(), key=lambda item: (item.name, item.id))]

    @mcp.tool()
    async def run_command(
        target: str, command: Annotated[str, Field(min_length=1, max_length=4096)],
        cwd: FilePath = "/home", stdin: Annotated[str, Field(max_length=4096)] = "",
        timeout_seconds: Annotated[int, Field(ge=1, le=40)] = 10,
    ) -> dict[str, Any]:
        """Run a command using the selected computer's OpenOS shell. Capture stdout, stderr and exit code. Use stdin for non-interactive input."""
        computer, (kind, code, stdout, stderr, truncated) = await bridge.call(target, "exec", command, cwd, stdin, timeout=timeout_seconds)
        return {"computer": computer, "exit_code": int(code) if code else None, "timed_out": kind == b"timeout",
                "stdout": as_text(stdout), "stderr": as_text(stderr), "truncated": flag(truncated)}

    @mcp.tool()
    async def list_files(target: str, path: FilePath = "/home", offset: Annotated[int, Field(ge=0)] = 0) -> dict[str, Any]:
        """List up to 100 entries on the selected OpenOS computer. Directories end in /. Pass next_offset to continue."""
        computer, (path_value, more, *entries) = await bridge.call(target, "list", path, str(offset))
        return {"computer": computer, "path": as_text(path_value), "entries": [as_text(entry) for entry in entries],
                "next_offset": offset + len(entries) if flag(more) else None}

    @mcp.tool()
    async def read_file(
        target: str, path: FilePath, offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=FILE_LIMIT)] = 8192, encoding: Literal["utf-8", "base64"] = "utf-8",
    ) -> dict[str, Any]:
        """Read a byte range from an OpenOS file. Use base64 for binary files and for exact byte ranges."""
        computer, (path_value, data, eof) = await bridge.call(target, "read", path, str(offset), str(limit))
        if encoding == "utf-8":
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError:
                encoding = "base64"
        if encoding == "base64":
            content = base64.b64encode(data).decode("ascii")
        return {"computer": computer, "path": as_text(path_value), "content": content, "encoding": encoding,
                "bytes_read": len(data), "next_offset": None if flag(eof) else offset + len(data)}

    @mcp.tool()
    async def write_file(
        target: str, path: FilePath, content: Annotated[str, Field(max_length=22_000)],
        encoding: Literal["utf-8", "base64"] = "utf-8", mode: Literal["replace", "append"] = "replace",
    ) -> dict[str, Any]:
        """Write up to 16 KiB per call to an OpenOS file. Replace by default or append chunks for larger files. The parent directory must exist."""
        try:
            data = base64.b64decode(content, validate=True) if encoding == "base64" else content.encode()
        except ValueError as error:
            raise ValueError("Invalid file content for the selected encoding") from error
        if len(data) > FILE_LIMIT:
            raise ValueError("File content exceeds 16 KiB. Split it into chunks.")
        computer, (path_value,) = await bridge.call(target, "write", path, mode, data)
        return {"computer": computer, "path": as_text(path_value), "bytes_written": len(data), "mode": mode}

    @mcp.tool()
    async def read_screen(
        target: str, row: Annotated[int, Field(ge=1, le=256)] = 1,
        rows: Annotated[int, Field(ge=1, le=50)] = 50,
    ) -> dict[str, Any]:
        """Read character cells from the selected computer's primary GPU screen, including text drawn by apps. Row numbers start at 1."""
        computer, (width, height, text) = await bridge.call(target, "screen", str(row), str(rows), timeout=25)
        return {"computer": computer, "width": int(width), "height": int(height), "first_row": row, "text": as_text(text)}

    @mcp.tool()
    async def send_input(target: str, text: Annotated[str, Field(max_length=2048)] = "", key: Key | None = None) -> dict[str, Any]:
        """Paste text and/or press a key on the selected computer's foreground OpenOS terminal. With both, paste first, then press the key."""
        if not text and key is None:
            raise ValueError("Provide text or a key")
        computer, _ = await bridge.call(target, "input", text, key or "")
        return {"computer": computer, "input_sent": True}

    return mcp.streamable_http_app()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--public-url", default=os.environ.get("OC_MCP_PUBLIC_URL"),
                        help="public HTTPS origin behind a reverse proxy (or set OC_MCP_PUBLIC_URL)")
    subparsers = parser.add_subparsers(dest="action", required=True)
    serve = subparsers.add_parser("serve", help="start the bridge")
    serve.add_argument("--host", default=HOST, help="address to bind (default 127.0.0.1)")
    subparsers.add_parser("mcp-url", help="print the MCP endpoint for Codex")
    subparsers.add_parser("mcp-token", help="print the bearer token for the public MCP endpoint")
    install = subparsers.add_parser("install", help="print the one-line installer for an OpenOS computer")
    install.add_argument("name", help="starts with a-z, then a-z, digits, underscores or hyphens, up to 24 characters")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.action == "install" and not NAME.fullmatch(args.name):
        parser.error("use a name like main or reactor-2 (a-z, 0-9, _, -, up to 24 characters)")
    try:
        public_url = https_origin(args.public_url) if args.public_url else None
    except ValueError as error:
        parser.error(str(error))
    if args.action == "mcp-token":
        print(mcp_bearer_token(access_key()))
        return
    bridge = Bridge(access_key(), args.port, public_url)
    if args.action == "mcp-url":
        print(bridge.url + bridge.mcp_path)
    elif args.action == "install":
        print(bridge.install_command(args.name))
    else:
        uvicorn.run(create_app(bridge), host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
