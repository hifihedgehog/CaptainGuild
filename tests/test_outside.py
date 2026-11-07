"""The real outside client against a small server on this machine. Nothing here leaves it."""
import unittest
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestServer

from captainguild.core import outside as outside_module
from captainguild.core.outside import USER_AGENT, Outside, OutsideError, Reply, _PublicResolver, check_public_link


class OutsideTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.hits: list[str] = []
        self.seen_agent = None

        async def hello(request: web.Request) -> web.Response:
            self.hits.append(request.path)
            self.seen_agent = request.headers.get("User-Agent")
            return web.json_response({"hello": "there", "q": request.query.getall("q", [])}, headers={"Retry-After": "12"})

        async def huge(request: web.Request) -> web.Response:
            return web.Response(body=b"x" * 5000)

        async def bounce(request: web.Request) -> web.Response:
            return web.Response(status=302, headers={"Location": "/secret"})

        async def secret(request: web.Request) -> web.Response:
            self.hits.append("secret")
            return web.Response(text="internal")

        async def form(request: web.Request) -> web.Response:
            return web.json_response(dict(await request.post()))

        app = web.Application()
        app.router.add_get("/hello", hello)
        app.router.add_get("/huge", huge)
        app.router.add_get("/bounce", bounce)
        app.router.add_get("/secret", secret)
        app.router.add_post("/form", form)
        self.server = TestServer(app)
        await self.server.start_server()
        self.base = f"http://127.0.0.1:{self.server.port}"
        self.web = Outside()
        self.web.offline = False  # this one test file talks to the server above

    async def asyncTearDown(self) -> None:
        await self.web.close()
        await self.server.close()

    async def test_tests_are_kept_offline_by_default(self):
        import tests.fakes  # noqa: F401  importing the fakes turns it on
        guarded = Outside()
        with self.assertRaises(OutsideError):
            await guarded.get(f"{self.base}/hello")
        self.assertEqual(self.hits, [])

    async def test_it_names_itself_and_reads_answers(self):
        answer = await self.web.get(f"{self.base}/hello", params=[("q", "1"), ("q", "2")])
        self.assertEqual(answer.status, 200)
        self.assertEqual(answer.json(), {"hello": "there", "q": ["1", "2"]})
        self.assertEqual(answer.retry_after(1.0), 12.0)
        self.assertEqual(answer.header("RETRY-AFTER"), "12")
        self.assertEqual(self.seen_agent, USER_AGENT)
        self.assertNotIn("aiohttp", USER_AGENT.lower())

    async def test_post_sends_a_form(self):
        answer = await self.web.post(f"{self.base}/form", data={"a": "b"})
        self.assertEqual(answer.json(), {"a": "b"})

    async def test_too_much_is_refused(self):
        with self.assertRaises(OutsideError):
            await self.web.get(f"{self.base}/huge", max_bytes=100)
        self.assertEqual(len((await self.web.get(f"{self.base}/huge", max_bytes=5000)).body), 5000)

    async def test_a_dead_address_and_bad_json_are_outside_errors(self):
        await self.server.close()
        with self.assertRaises(OutsideError):
            await self.web.get(f"{self.base}/hello", timeout=2.0)
        with self.assertRaises(OutsideError):
            Reply(200, b"<html>").json()
        self.assertIsNone(Reply(200, b"").json())
        self.assertEqual(Reply(200, b"", {"retry-after": "soon"}).retry_after(9.0), 9.0)

    async def test_a_strangers_link_never_reaches_this_machine(self):
        for link in (f"{self.base}/hello", "http://127.0.0.1/hello", "http://localhost/hello", "http://[::1]/hello"):
            with self.assertRaises(OutsideError, msg=link):
                await self.web.get(link, stranger=True, timeout=5.0)
        self.assertEqual(self.hits, [])

    async def test_an_address_in_lookalike_digits_is_read_the_way_the_client_reads_it(self):
        for link in (
            "http://\uff11\uff12\uff17.\uff10.\uff10.\uff11/x", "http://127\u30020\u30020\u30021/x", "http://\u2460\u2461\u2466.0.0.1/x",
            "http://\uff11\uff16\uff19.\uff12\uff15\uff14.\uff11\uff16\uff19.\uff12\uff15\uff14/latest/meta-data/", "http://169.254.169.254\u3002/x", "http://[::ffff:127.0.0.1]/x",
        ):
            with self.assertRaises(OutsideError, msg=ascii(link)):
                check_public_link(link)
        check_public_link("http://\uff45\uff58\uff41\uff4d\uff50\uff4c\uff45.com/x")  # a name, which the resolver checks when the client connects
        check_public_link("https://example.com/x")

    async def test_a_redirect_from_a_public_address_to_a_private_one_is_refused(self):
        allowed = f"{self.base}/bounce"
        real_check = check_public_link

        def let_only_the_first_hop_through(url: str) -> None:
            if url != allowed:
                real_check(url)

        class AnyResolver(_PublicResolver):
            pass

        with mock.patch.object(outside_module, "check_public_link", let_only_the_first_hop_through):
            with self.assertRaises(OutsideError):
                await self.web.get(allowed, stranger=True, timeout=5.0)
        self.assertEqual(self.hits, [], "the redirect's target was never asked for")

    async def test_names_that_point_inside_are_refused_by_the_resolver(self):
        resolver = _PublicResolver()
        with self.assertRaises(OSError):
            await resolver.resolve("localhost", 80)
        answer = [{"hostname": "x", "host": "93.184.216.34", "port": 80, "family": 2, "proto": 0, "flags": 0}, {"hostname": "x", "host": "10.0.0.1", "port": 80, "family": 2, "proto": 0, "flags": 0}]
        with mock.patch("aiohttp.resolver.ThreadedResolver.resolve", mock.AsyncMock(return_value=answer)):
            kept = await resolver.resolve("mixed.example", 80)
        self.assertEqual([row["host"] for row in kept], ["93.184.216.34"])
