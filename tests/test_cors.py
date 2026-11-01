import asyncio

import httpx
import pytest
from wred_cors import cors_application

from wred import App

ORIGIN = "https://app.example.com"


@pytest.mark.parametrize(
    "origin, method, headers, status",
    [
        (ORIGIN, "POST", "Authorization, Content-Type", 200),
        (ORIGIN, "POST", "authorization, CONTENT-TYPE", 200),
        ("https://evil.example", "POST", "authorization", 400),
        ("https://app.example.com.evil.example", "POST", "authorization", 400),
        ("null", "POST", "authorization", 400),
        (ORIGIN, "DELETE", "authorization", 400),
        (ORIGIN, "POST", "x-not-allowed", 400),
    ],
)
async def test_preflight_checks_policy_without_dispatching(
    origin, method, headers, status
):
    async def unreachable(scope, receive, send):
        pytest.fail("Preflight dispatched to the application")

    app = cors_application(
        unreachable,
        allow_origins=[ORIGIN],
        allow_methods=["POST"],
        allow_headers=["Authorization"],
        allow_credentials=True,
        max_age=300,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        result = await client.options(
            "/not-registered",
            headers={
                "origin": origin,
                "access-control-request-method": method,
                "access-control-request-headers": headers,
            },
        )
    assert result.status_code == status
    assert {v.strip().lower() for v in result.headers["vary"].split(",")} == {
        "origin",
        "access-control-request-method",
        "access-control-request-headers",
    }
    if status == 200:
        assert result.headers["access-control-allow-origin"] == ORIGIN
        assert result.headers["access-control-allow-credentials"] == "true"
        assert result.headers["access-control-max-age"] == "300"
        assert result.headers["access-control-allow-methods"] == "POST"
    elif origin != ORIGIN:
        assert "access-control-allow-origin" not in result.headers


@pytest.mark.parametrize("origin", [None, ORIGIN, "https://evil.example"])
async def test_actual_requests_errors_and_plain_options_reach_wred(origin):
    api = App()
    calls = []

    @api.route("/", methods=["POST", "OPTIONS"])
    async def handler(request):
        calls.append(request.method)
        return await request.body(), [("x-result", "visible")], 401

    app = cors_application(api, allow_origins=[ORIGIN], expose_headers=["X-Result"])
    headers = {"origin": origin} if origin else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        result = await client.post("/", headers=headers, content=b"untouched")
        assert (result.status_code, result.content) == (401, b"untouched")
        assert result.headers.get("access-control-allow-origin") == (
            ORIGIN if origin == ORIGIN else None
        )
        assert "origin" in result.headers["vary"].lower()
        assert (await client.options("/", headers=headers)).status_code == 401
        missing = await client.get("/missing", headers=headers)
        assert missing.status_code == 404
        assert missing.headers.get("access-control-allow-origin") == (
            ORIGIN if origin == ORIGIN else None
        )
    assert calls == ["POST", "OPTIONS"]


async def test_concurrent_streams_preserve_chunks_headers_and_origin_isolation():
    shared_start = {
        "type": "http.response.start",
        "status": 206,
        "headers": [
            (b"vary", b"Accept-Encoding"),
            (b"vary", b"Accept-Language"),
            (b"set-cookie", b"a=1"),
            (b"set-cookie", b"b=2"),
        ],
    }

    async def streaming(scope, receive, send):
        await send(shared_start)
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        await asyncio.sleep(0)
        await send({"type": "http.response.body", "body": b"last", "more_body": False})

    origins = [ORIGIN, "https://other.example", "https://denied.example"]
    app = cors_application(streaming, allow_origins=origins[:2], allow_credentials=True)

    async def run(index):
        origin = origins[index % 3]
        messages = []

        async def receive():
            pytest.fail("CORS must not consume the request body")

        async def send(message):
            messages.append(message)
            await asyncio.sleep(0)  # Simulate a transport applying backpressure.

        await app(
            {
                "type": "http",
                "method": "GET",
                "headers": [(b"origin", origin.encode())],
            },
            receive,
            send,
        )
        response_headers = httpx.Headers(messages[0]["headers"])
        assert messages[0]["status"] == 206
        assert response_headers.get("access-control-allow-origin") == (
            origin if index % 3 != 2 else None
        )
        assert response_headers.get_list("set-cookie") == ["a=1", "b=2"]
        assert {v.strip().lower() for v in response_headers["vary"].split(",")} == {
            "origin",
            "accept-encoding",
            "accept-language",
        }
        assert [(m["body"], m["more_body"]) for m in messages[1:]] == [
            (b"first", True),
            (b"last", False),
        ]

    await asyncio.gather(*(run(i) for i in range(300)))
    assert len(shared_start["headers"]) == 4


@pytest.mark.parametrize("kind", ["lifespan", "websocket"])
async def test_non_http_protocols_pass_through(kind):
    scope = {"type": kind}
    message = {"type": kind + ".test"}
    delivered = []

    async def receive():
        return message

    async def send(value):
        delivered.append(value)

    async def inner(actual_scope, actual_receive, actual_send):
        assert actual_scope is scope
        assert await actual_receive() is message
        await actual_send(message)

    await cors_application(inner)(scope, receive, send)
    assert delivered == [message]


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_exceptions_and_cancellation_propagate(failure):
    async def inner(scope, receive, send):
        raise failure("application failed")

    with pytest.raises(failure, match="application failed"):
        await cors_application(inner)(
            {"type": "http", "method": "GET", "headers": []}, None, None
        )


async def test_defaults_deny_and_wildcard_is_explicit():
    api = App()

    @api.route("/")
    async def handler(request):
        return {"public": True}

    for origins, expected in [((), None), (("*",), "*")]:
        app = cors_application(api, allow_origins=origins)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            result = await client.get("/", headers={"origin": ORIGIN})
            assert result.json() == {"public": True}
            assert result.headers.get("access-control-allow-origin") == expected
            assert "access-control-allow-credentials" not in result.headers


@pytest.mark.parametrize(
    "options",
    [
        {"allow_origins": ORIGIN},
        {"allow_headers": "Authorization"},
        {"allow_origins": ["https://app.example.com/"]},
        {"allow_origins": ["https://*.example.com"]},
        {"allow_origins": ["https://user@app.example.com"]},
        {"allow_origins": ["https://app.example.com\r\nx-injected: yes"]},
        {"allow_headers": ["x-header\r\nx-injected: yes"]},
        {"allow_methods": ["GET, POST"]},
        {"expose_headers": [""]},
        {"allow_credentials": "true"},
        {"max_age": -1},
        {"max_age": True},
        *[
            {field: ["*"], "allow_credentials": True}
            for field in (
                "allow_origins",
                "allow_methods",
                "allow_headers",
                "expose_headers",
            )
        ],
    ],
)
def test_invalid_policy_fails_at_configuration_time(options):
    with pytest.raises((ValueError, TypeError)):
        cors_application(App(), **options)


async def test_configuration_is_snapshotted():
    origins = [ORIGIN]
    app = cors_application(App(), allow_origins=origins)
    origins.append("https://evil.example")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        result = await client.get("/", headers={"origin": origins[-1]})
    assert "access-control-allow-origin" not in result.headers


@pytest.mark.parametrize("vary", ["*", "Accept-Encoding, ORIGIN"])
async def test_existing_vary_wildcard_or_origin_is_not_expanded(vary):
    api = App()

    @api.route("/")
    async def handler(request):
        return b"ok", [("vary", vary)], 200

    app = cors_application(api, allow_origins=[ORIGIN])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        result = await client.get("/", headers={"origin": ORIGIN})
    assert result.headers["vary"] == vary
    assert result.headers["access-control-allow-origin"] == ORIGIN
