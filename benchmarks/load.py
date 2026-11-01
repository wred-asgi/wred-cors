"""Finite HTTP load check against real Uvicorn worker processes.

python benchmarks/load.py --mode cors --requests 10000 --concurrency 100
"""

import argparse
import asyncio
import json
import os
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from wred_cors import cors_application

from wred import App

api = App()


@api.route("/")
async def index(request):
    return {"pid": os.getpid()}


app = (
    api
    if os.getenv("WRED_CORS_BENCH_MODE") == "raw"
    else cors_application(
        api,
        allow_origins=[f"https://tenant-{i}.example" for i in range(10000)],
        allow_headers=["Authorization"],
    )
)


async def measure(url, options, transport=None):
    durations, pids, failures = [], set(), []
    limits = httpx.Limits(
        max_connections=options.concurrency,
        max_keepalive_connections=options.concurrency,
    )
    async with httpx.AsyncClient(
        base_url=url, limits=limits, timeout=10, trust_env=False, transport=transport
    ) as client:
        deadline = time.monotonic() + 30
        while True:
            try:
                if (await client.get("/")).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("Server did not become ready")
            await asyncio.sleep(0.05)

        async def worker(number):
            for i in range(number, options.requests, options.concurrency):
                await asyncio.sleep(0)
                allowed = i % 2 == 0
                origin = (
                    f"https://tenant-{i % 10000}.example"
                    if allowed
                    else "https://denied.example"
                )
                headers = {"origin": origin}
                method = "GET"
                expected = 200
                if options.mode == "preflight":
                    method = "OPTIONS"
                    headers.update(
                        {
                            "access-control-request-method": "GET",
                            "access-control-request-headers": "Authorization",
                        }
                    )
                    expected = 200 if allowed else 400
                started = time.perf_counter()
                try:
                    result = await client.request(method, "/", headers=headers)
                    if result.status_code != expected:
                        raise AssertionError(f"Unexpected status {result.status_code}")
                    if options.mode != "raw":
                        if result.headers.get("access-control-allow-origin") != (
                            origin if allowed else None
                        ):
                            raise AssertionError("Origin policy mismatch")
                        if "origin" not in result.headers.get("vary", "").lower():
                            raise AssertionError("Missing Vary")
                    if options.mode != "preflight":
                        pids.add(result.json()["pid"])
                except (httpx.HTTPError, AssertionError, ValueError, KeyError) as exc:
                    failures.append(type(exc).__name__ + ": " + str(exc))
                durations.append(time.perf_counter() - started)

        started = time.perf_counter()
        await asyncio.gather(*(worker(i) for i in range(options.concurrency)))
        elapsed = time.perf_counter() - started
    durations.sort()
    report = {
        "mode": options.mode,
        "transport": options.transport,
        "requests": options.requests,
        "concurrency": options.concurrency,
        "workers": options.workers,
        "configured_origins": 10000,
        "seconds": round(elapsed, 3),
        "requests_per_second": round(options.requests / elapsed, 1),
        "p50_ms": round(statistics.median(durations) * 1000, 3),
        "p95_ms": round(durations[int((len(durations) - 1) * 0.95)] * 1000, 3),
        "errors": len(failures),
        "error_samples": failures[:3],
        "workers_observed": len(pids),
        "python": sys.version.split()[0],
    }
    print(json.dumps(report, indent=2))
    if failures:
        raise RuntimeError("Load check failed")
    if (
        options.transport == "http"
        and options.mode != "preflight"
        and len(pids) != options.workers
    ):
        raise RuntimeError("Load did not exercise every configured worker")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["raw", "cors", "preflight"], default="cors")
    parser.add_argument("--transport", choices=["http", "inprocess"], default="http")
    parser.add_argument("--requests", type=int, default=10000)
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--workers", type=int, default=2)
    options = parser.parse_args()
    if min(options.requests, options.concurrency, options.workers) < 1:
        parser.error("counts must be positive")
    if options.transport == "inprocess":
        options.workers = 1
        target = api if options.mode == "raw" else app
        asyncio.run(measure("http://test", options, httpx.ASGITransport(target)))
        return
    # Pass a bound socket to Uvicorn to avoid a free-port lookup race.
    with socket.socket() as listener, tempfile.TemporaryFile(mode="w+") as log:
        listener.bind(("127.0.0.1", 0))
        listener.listen(2048)
        port = listener.getsockname()[1]
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "load:app",
                "--app-dir",
                str(Path(__file__).parent),
                "--fd",
                str(listener.fileno()),
                "--workers",
                str(options.workers),
                "--lifespan",
                "off",
                "--no-access-log",
                "--log-level",
                "warning",
            ],
            pass_fds=(listener.fileno(),),
            stdout=log,
            stderr=log,
            env={**os.environ, "WRED_CORS_BENCH_MODE": options.mode},
            start_new_session=True,
        )
        try:
            asyncio.run(measure(f"http://127.0.0.1:{port}", options))
        finally:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            log.seek(0)
            diagnostic = log.read()
            if diagnostic:
                print(diagnostic, file=sys.stderr)


if __name__ == "__main__":
    main()
