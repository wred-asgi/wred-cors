"""Explicit CORS policy for Wred, backed by Starlette's ASGI middleware."""

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

__version__ = "0.1.0"
__all__ = ["cors_application"]

_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", re.ASCII)


def _values(values: Iterable[str], name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{name} must be an iterable of strings, not a string")
    result = tuple(values)
    if any(not isinstance(value, str) or not value for value in result):
        raise ValueError(f"{name} must contain nonempty strings")
    return tuple(dict.fromkeys(result))


class _CORS(CORSMiddleware):
    def __init__(self, app: ASGIApp, **options):
        super().__init__(app, **options)
        self._origins = frozenset(self.allow_origins)

    def is_allowed_origin(self, origin: str) -> bool:
        return self.allow_all_origins or origin in self._origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Include denied and origin-less variants too: shared caches must not
        # serve one origin's response headers to another origin.
        vary = ["Origin"]
        if scope["method"] == "OPTIONS":
            vary += ["Access-Control-Request-Method", "Access-Control-Request-Headers"]

        async def send_with_vary(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": list(message.get("headers", []))}
                headers = MutableHeaders(scope=message)
                existing = {
                    token.strip().lower()
                    for value in headers.getlist("vary")
                    for token in value.split(",")
                }
                if "*" not in existing:
                    missing = [name for name in vary if name.lower() not in existing]
                    if missing:
                        # Preserve all existing Vary fields, including duplicates.
                        message["headers"].append(
                            (b"vary", ", ".join(missing).encode("ascii"))
                        )
            await send(message)

        await super().__call__(scope, receive, send_with_vary)

    async def send(self, message: Message, send: Send, request_headers) -> None:
        if message["type"] == "http.response.start":
            message = {**message, "headers": list(message.get("headers", []))}
            headers = MutableHeaders(scope=message)
            vary = headers.getlist("vary")
            if len(vary) > 1:
                headers["vary"] = ", ".join(vary)
        await super().send(message, send, request_headers)

    @staticmethod
    def allow_explicit_origin(headers: MutableHeaders, origin: str) -> None:
        headers["Access-Control-Allow-Origin"] = origin
        vary = {
            token.strip().lower()
            for value in headers.getlist("vary")
            for token in value.split(",")
        }
        if "*" not in vary and "origin" not in vary:
            headers.add_vary_header("Origin")


def cors_application(
    app: ASGIApp,
    *,
    allow_origins: Iterable[str] = (),
    allow_methods: Iterable[str] = ("GET",),
    allow_headers: Iterable[str] = (),
    allow_credentials: bool = False,
    expose_headers: Iterable[str] = (),
    max_age: int = 600,
) -> ASGIApp:
    """Wrap an ASGI app with an immutable, explicit browser sharing policy.

    Origins match exactly. Credentials require explicit origins, methods, and
    headers. Preflights terminate here; other requests reach the wrapped app.
    """
    origins = _values(allow_origins, "allow_origins")
    methods = _values(allow_methods, "allow_methods")
    headers = _values(allow_headers, "allow_headers")
    exposed = _values(expose_headers, "expose_headers")
    if type(allow_credentials) is not bool:
        raise TypeError("allow_credentials must be a bool")
    if type(max_age) is not int or max_age < 0:
        raise ValueError("max_age must be a nonnegative integer")
    for origin in origins:
        if origin in ("*", "null"):
            continue
        try:
            parsed = urlsplit(origin)
            port = parsed.port
            valid = (
                origin.isascii()
                and not any(
                    character.isspace() or ord(character) < 32 or ord(character) == 127
                    for character in origin
                )
                and parsed.scheme in ("http", "https")
                and origin.startswith(parsed.scheme + "://")
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
                and not any(
                    character in origin for character in ("*", "?", "#", "\\", "@")
                )
                and (port is None or 0 < port <= 65535)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(
                f"Expected an exact http(s) origin without a path: {origin!r}"
            )
    for name, values in (
        ("allow_methods", methods),
        ("allow_headers", headers),
        ("expose_headers", exposed),
    ):
        if any(_TOKEN.fullmatch(value) is None for value in values):
            raise ValueError(f"{name} must contain HTTP tokens")
    if allow_credentials and any(
        "*" in values for values in (origins, methods, headers, exposed)
    ):
        raise ValueError(
            "Credentials require explicit origins, methods, headers, and exposed headers"
        )
    return _CORS(
        app,
        allow_origins=origins,
        allow_methods=tuple(method.upper() for method in methods),
        allow_headers=headers,
        allow_credentials=allow_credentials,
        expose_headers=exposed,
        max_age=max_age,
    )
