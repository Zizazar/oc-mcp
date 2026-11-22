import asyncio
import logging
import re
import unittest
from html import unescape
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient

from bridge import Bridge, create_app, mcp_bearer_token, pack, unpack
from relay import Relay, create_relay_app


KEY = b"a" * 64
PUBLIC_URL = "https://oc.example.test"
ADDRESS = "00000000-0000-0000-0000-000000000001"
OTHER_ADDRESS = "00000000-0000-0000-0000-000000000002"


def code_block(html, id):
    return unescape(re.search(rf'<code id="{id}">(.*?)</code>', html).group(1))


class MultiuserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch("bridge.POLL_SECONDS", 0.05))
        logger = logging.getLogger("httpx")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.WARNING)

    async def call_tool(self, client, credential, name, arguments=None, path="/mcp"):
        headers = {"Accept": "application/json, text/event-stream"}
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        return await client.post(path, headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })

    async def enroll(self, client, address, name):
        signup = await client.post("/setup", data={"action": "register"})
        self.assertEqual(signup.status_code, 200)
        self.assertEqual(signup.headers["cache-control"], "no-store")
        credential = code_block(signup.text, "personal-token")
        install = await client.post("/setup", data={"action": "install", "token": credential, "name": name})
        self.assertEqual(install.status_code, 200)
        script = await client.get(code_block(install.text, "openos-install").split()[2])
        self.assertEqual(script.status_code, 200)
        agent = re.search(r'local endpoint, computerName, idleDelay = "([^"]+)"', script.text).group(1)
        self.assertEqual((await client.post(agent, content=address)).text, "idle")
        return credential, agent

    async def check_isolation(self, client):
        first, first_agent = await self.enroll(client, ADDRESS, "shared-name")
        second, second_agent = await self.enroll(client, OTHER_ADDRESS, "shared-name")
        self.assertNotEqual(first, second)

        unauthenticated = await self.call_tool(client, None, "list_computers")
        self.assertEqual(unauthenticated.status_code, 401)
        modified = await self.call_tool(client, first + "0", "list_computers")
        self.assertEqual(modified.status_code, 401)
        owner = await self.call_tool(client, mcp_bearer_token(KEY), "list_computers")
        self.assertEqual(owner.json()["result"]["structuredContent"]["result"], [])

        for credential, own, other in [(first, ADDRESS, OTHER_ADDRESS), (second, OTHER_ADDRESS, ADDRESS)]:
            result = await self.call_tool(client, credential, "list_computers")
            computers = result.json()["result"]["structuredContent"]["result"]
            self.assertEqual([(pc["id"], pc["name"]) for pc in computers], [(own, "shared-name")])
            result = await self.call_tool(client, credential, "read_screen", {"target": other})
            self.assertTrue(result.json()["result"]["isError"])
            self.assertIn("Unknown computer", result.text)

        # Even a second agent reporting the exact same ID remains in its own account.
        self.assertEqual((await client.post(second_agent, content=ADDRESS)).text, "idle")
        screen = asyncio.create_task(self.call_tool(client, first, "read_screen", {"target": "shared-name"}))
        try:
            self.assertEqual((await client.post(second_agent, content=ADDRESS)).text, "idle")
            job = await client.post(first_agent, content=ADDRESS)
            job_id, operation, *_ = unpack(job.content)
            self.assertEqual(operation, b"screen")
            await client.post(first_agent, content=pack(ADDRESS, job_id, "ok", "80", "25", "first screen"))
            result = await asyncio.wait_for(screen, 2)
            self.assertEqual(result.json()["result"]["structuredContent"]["text"], "first screen")
        finally:
            if not screen.done():
                screen.cancel()
                await asyncio.gather(screen, return_exceptions=True)

        parts = first_agent.split("/")
        parts[4] = second_agent.split("/")[4]
        self.assertEqual((await client.post("/".join(parts), content=ADDRESS)).status_code, 404)

    async def test_direct_https_bridge(self):
        app = create_app(Bridge(KEY, 8765, PUBLIC_URL))
        async with app.router.lifespan_context(app), AsyncClient(transport=ASGITransport(app=app), base_url=PUBLIC_URL) as client:
            self.assertIn("codex mcp add", (await client.get("/")).text)
            await self.check_isolation(client)

    async def test_tunnel_relay_and_legacy_local_mcp(self):
        bridge = Bridge(KEY, 8765)
        bridge_app = create_app(bridge)
        relay_app = create_relay_app(Relay(KEY, 8765), 8766, PUBLIC_URL, None, expose_mcp=True)
        async with (
            bridge_app.router.lifespan_context(bridge_app),
            relay_app.router.lifespan_context(relay_app),
            AsyncClient(transport=ASGITransport(app=bridge_app), base_url=bridge.url) as upstream,
            AsyncClient(transport=ASGITransport(app=relay_app), base_url=PUBLIC_URL) as client,
        ):
            relay_app.state.client = upstream
            self.assertIn("codex mcp add", (await client.get("/", headers={"Accept": "text/html"})).text)
            self.assertIn("OpenComputers relay", (await client.get("/")).text)
            await self.check_isolation(client)

            owner = await self.call_tool(upstream, None, "list_computers", path=bridge.mcp_path)
            self.assertEqual(owner.json()["result"]["structuredContent"]["result"], [])
            invalid = await self.call_tool(upstream, "invalid", "list_computers", path=bridge.mcp_path)
            self.assertEqual(invalid.status_code, 401)
