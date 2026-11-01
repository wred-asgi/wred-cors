# wred-cors

Explicit CORS policy for Wred, using Starlette's ASGI CORS implementation.
Requires Python 3.11+ and Wred 0.14. Wred's core remains unchanged.

Install from GitHub:

```sh
python -m pip install git+https://github.com/wred-asgi/wred-cors.git
```

From a source checkout, use `python -m pip install .`.

```python
from wred import App
from wred_cors import cors_application

api = App()


@api.route("/items")
async def items(request):
    return {"items": []}


app = cors_application(
    api,
    allow_origins=["https://app.example.com"],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=True,
    expose_headers=["X-Request-ID"],
    max_age=600,
)
```

Register routes on `api`; serve `app`. Put CORS outside other HTTP middleware so
their responses receive the policy. It composes with `lifespan_application`:
lifespan messages pass through unchanged. Use one CORS owner; do not also set CORS
headers in handlers, a proxy, or another wrapper.

## Policy

Origins match exact scheme, host, and port, without trailing slashes or paths.
No subdomain patterns or regular expressions are accepted. The default empty
origin list grants no cross-origin browser access. `"null"` must be explicitly
listed to allow opaque origins; normally leave it out.

Methods default to `GET`; configured names are uppercased. Header names are
case-insensitive; CORS safelisted headers are always allowed. `allow_headers`
governs request headers; `expose_headers` makes response headers readable by
JavaScript. Neither option adds those application headers.

Wildcard origins, methods, and request headers are opt-in. Wildcard methods expand
to Starlette's standard HTTP methods. With `allow_credentials=True`, wildcard
origins, methods, headers, and exposed headers are rejected at startup. `max_age`
is a nonnegative integer in seconds; browsers may cap it. Configuration inputs
are copied, so changing an input list does not change live policy.

`OPTIONS` with both `Origin` and `Access-Control-Request-Method` is a preflight.
Allowed preflights return 200; disallowed ones return 400 without calling Wred,
auth, or database handlers. Plain OPTIONS reaches the application. Preflight
policy is app-wide and does not imply a path exists or accepts that method.

Actual requests always reach the app. Disallowed origins receive no origin grant;
the browser decides whether JavaScript can read the response. CORS is not auth
or CSRF protection and does not prevent requests from being sent. See the
[Fetch CORS protocol](https://fetch.spec.whatwg.org/#http-cors-protocol).

## Production behavior

- No network calls, locks, background tasks, body reads, or response buffering.
  Exact origin membership uses a frozen set. Policy needs no cross-worker state.
- Streaming preserves chunks and backpressure. Status codes, duplicate cookies,
  and application headers survive. Exceptions/cancellation propagate; this wrapper
  does not manufacture a 500. Put error middleware inside CORS for headers on 500s.
- Responses vary by Origin, including denied and origin-less requests. OPTIONS
  also varies by requested method and headers. Existing Vary fields are preserved.
  Configure CDNs/proxies to honor these fields. CORS wraps handler caching so
  grants are added per request after cache lookup.
- Lock and update the tested Starlette version in your deployment. Configure
  TLS, header/body limits, concurrency, and timeouts at ingress. Benchmark your
  complete deployment before assigning a production capacity target.

Follow the [shared extension contract](https://github.com/wred-asgi/.github/blob/main/EXTENSIONS.md)
for handler composition, ASGI wrappers, and compatibility.

## Development

```sh
python -m pip install -e '.[test]'
python -m pytest
python benchmarks/load.py --mode cors --requests 10000 --concurrency 100
python -m build
```

Tests cover preflight policy, credentials, errors, protocol forwarding, and
concurrent streaming. The load script runs real HTTP against two Uvicorn workers with 10,000 configured
origins. Run `--mode raw`, `--mode cors`, and `--mode preflight` for comparison.
It is a finite local check, not a production capacity guarantee or soak test.
