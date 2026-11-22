"""Connect OpenComputers on remote Minecraft servers to the local bridge."""

import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
import os
import re
import shutil
import socket
import subprocess
from urllib.parse import urlsplit

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from bridge import FRAME_LIMIT, HOST, NAME, ROOT, access_key, installer_command, installer_script, token, valid_token


QUICK_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
RELAY_STATUS = "OpenComputers relay\n"


def https_origin(value: str) -> str:
    value = value.strip().rstrip("/")
    if not re.fullmatch(r"https://[a-z0-9.-]+(?::[0-9]{1,5})?", value, re.IGNORECASE):
        raise ValueError("use an HTTPS origin, such as https://relay.example.com")
    if urlsplit(value).port == 0:
        raise ValueError("the HTTPS origin cannot use port 0")
    return value


class Relay:
    def __init__(self, key: bytes, bridge_port: int, bridge_host: str = HOST):
        self.key = key
        self.bridge_url = f"http://{bridge_host}:{bridge_port}"
        self.url: str | None = None
        self.agent = (ROOT / "oc-mcp.lua").read_text(encoding="utf-8")

    def credential(self, url: str, purpose: str, name: str) -> str:
        return token(self.key, f"relay/{url}/{purpose}/{name}")

    def command(self, url: str, name: str) -> str:
        return installer_command(f"{url}/i/{name}/{self.credential(url, 'install', name)}")

    async def status(self, request: Request) -> Response:
        if self.url is None:
            return PlainTextResponse("Relay is not connected", status_code=503)
        return PlainTextResponse(RELAY_STATUS + self.url, headers={"Cache-Control": "no-store"})

    async def install(self, request: Request) -> Response:
        url, name = self.url, request.path_params["name"]
        if not url or not NAME.fullmatch(name) or not valid_token(request.path_params["token"], self.credential(url, "install", name)):
            return PlainTextResponse("Installer not found", status_code=404)
        endpoint = f"{url}/a/{name}/{self.credential(url, 'agent', name)}"
        return PlainTextResponse(installer_script(self.agent, endpoint, name, idle_delay=2), headers={"Cache-Control": "no-store"})

    async def poll(self, request: Request) -> Response:
        url, name = self.url, request.path_params["name"]
        if not url or not NAME.fullmatch(name) or not valid_token(request.path_params["token"], self.credential(url, "agent", name)):
            return PlainTextResponse("Unknown agent", status_code=404)
        frame = bytearray()
        async for chunk in request.stream():
            if len(frame) + len(chunk) > FRAME_LIMIT:
                return PlainTextResponse("Frame too large", status_code=413)
            frame.extend(chunk)
        endpoint = f"{self.bridge_url}/a/{name}/{token(self.key, 'agent/' + name)}"
        try:
            response = await request.app.state.client.post(endpoint, content=bytes(frame))
        except httpx.RequestError:
            return PlainTextResponse("Local bridge unavailable", status_code=503)
        if response.status_code not in (200, 400, 413) or len(response.content) > FRAME_LIMIT:
            return PlainTextResponse("Invalid response from local bridge", status_code=502)
        return Response(response.content, status_code=response.status_code, media_type="text/plain",
                        headers={"Cache-Control": "no-store"})


async def wait_for_quick_tunnel(process: asyncio.subprocess.Process) -> str:
    assert process.stdout is not None
    url = None
    connected = False
    diagnostics = []
    try:
        async with asyncio.timeout(35):
            while line := await process.stdout.readline():
                message = line.decode(errors="replace").strip()
                if match := QUICK_URL.search(message):
                    url = match.group()
                connected = connected or "Registered tunnel connection" in message
                if url and connected:
                    return url
                if " ERR " in message or " FTL " in message:
                    diagnostics.append(message)
    except TimeoutError:
        pass
    detail = diagnostics[-1] if diagnostics else "cloudflared did not connect within 35 seconds"
    raise RuntimeError(f"Could not start Cloudflare Quick Tunnel. {detail}")


async def watch_tunnel(process: asyncio.subprocess.Process, relay: Relay) -> None:
    assert process.stdout is not None
    while await process.stdout.readline():
        pass
    await process.wait()
    relay.url = None
    print("Quick Tunnel stopped. Restart the relay to get a new HTTPS address.", flush=True)


def create_relay_app(relay: Relay, relay_port: int, public_url: str | None, cloudflared: str | None):
    @asynccontextmanager
    async def lifespan(app: Starlette):
        process = watcher = None
        async with httpx.AsyncClient(timeout=6, trust_env=False) as client:
            app.state.client = client
            try:
                if cloudflared:
                    hide_window = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                    process = await asyncio.create_subprocess_exec(
                        cloudflared, "tunnel", "--no-autoupdate", "--protocol", "http2",
                        "--url", f"http://{HOST}:{relay_port}",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                        creationflags=hide_window,
                    )
                    relay.url = await wait_for_quick_tunnel(process)
                    watcher = asyncio.create_task(watch_tunnel(process, relay))
                else:
                    relay.url = public_url
                print(f"Relay HTTPS address: {relay.url}", flush=True)
                yield
            finally:
                relay.url = None
                if watcher:
                    watcher.cancel()
                    with suppress(asyncio.CancelledError):
                        await watcher
                if process and process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.terminate()
                    try:
                        await asyncio.wait_for(process.communicate(), 5)
                    except TimeoutError:
                        with suppress(ProcessLookupError):
                            process.kill()
                        await process.communicate()

    return Starlette(lifespan=lifespan, routes=[
        Route("/", relay.status, methods=["GET"]),
        Route("/i/{name}/{token}", relay.install, methods=["GET"]),
        Route("/a/{name}/{token}", relay.poll, methods=["POST"]),
    ])


def current_url(relay_port: int) -> str:
    try:
        response = httpx.get(f"http://{HOST}:{relay_port}/", timeout=3, trust_env=False)
        response.raise_for_status()
        if not response.text.startswith(RELAY_STATUS):
            raise ValueError("unexpected relay status")
        return https_origin(response.text.removeprefix(RELAY_STATUS))
    except (httpx.HTTPError, ValueError) as error:
        raise SystemExit("Relay is unavailable. Start it first with python relay.py serve.") from error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766, help="relay port (default 8766)")
    parser.add_argument("--bridge-port", type=int, default=8765, help="bridge port (default 8765)")
    actions = parser.add_subparsers(dest="action", required=True)
    serve = actions.add_parser("serve", help="start a Cloudflare Quick Tunnel and the agent gateway")
    serve.add_argument("--public-url", help="use an existing HTTPS tunnel to this relay instead of starting cloudflared")
    serve.add_argument("--host", default=HOST, help="address to bind (default 127.0.0.1)")
    serve.add_argument("--bridge-host", default=HOST, help="address of the bridge (default 127.0.0.1)")
    install = actions.add_parser("install", help="print the one-line OpenOS installer for the active tunnel")
    install.add_argument("name")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.bridge_port <= 65535 or args.port == args.bridge_port:
        parser.error("the relay and bridge need different ports between 1 and 65535")
    if args.action == "install":
        if not NAME.fullmatch(args.name):
            parser.error("use a name like server-main (a-z, 0-9, _, -, up to 24 characters)")
        relay = Relay(access_key(), args.bridge_port)
        print(relay.command(current_url(args.port), args.name))
        return

    public_url = None
    cloudflared = None
    if args.public_url:
        try:
            public_url = https_origin(args.public_url)
        except ValueError as error:
            parser.error(str(error))
    else:
        cloudflared = shutil.which("cloudflared")
        if cloudflared is None:
            parser.error("cloudflared was not found in PATH. Install it from https://developers.cloudflare.com/tunnel/downloads/")
    relay = Relay(access_key(), args.bridge_port, args.bridge_host)
    app = create_relay_app(relay, args.port, public_url, cloudflared)
    config = uvicorn.Config(app, host=args.host, port=args.port, access_log=False, proxy_headers=False)
    server = uvicorn.Server(config)
    try:
        with socket.create_server((args.host, args.port)) as listener:
            server.run(sockets=[listener])
    except OSError as error:
        raise SystemExit(f"Could not bind relay to {args.host}:{args.port}. {error}") from error
    if not server.started:
        raise SystemExit("Relay did not start.")


if __name__ == "__main__":
    main()
