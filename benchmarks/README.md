# CORS load validation

Run the finite HTTP check on a machine that permits local TCP sockets:

```sh
python benchmarks/load.py --mode raw --requests 10000 --concurrency 100
python benchmarks/load.py --mode cors --requests 10000 --concurrency 100
python benchmarks/load.py --mode preflight --requests 10000 --concurrency 100
```

Each run starts two real Uvicorn workers, uses a pool of keep-alive HTTP clients,
and shuts the process group down afterwards. CORS modes configure 10,000 exact
origins and alternate allowed/denied origins. Every response is checked; the
script fails on unexpected responses, transport errors, origin leakage, or
ordinary HTTP traffic that did not exercise all workers. Reported latency includes
client scheduling and connection contention. Linux/POSIX is required for this
worker harness. CI runs the CORS and preflight modes on Ubuntu.

For environments without sockets, add `--transport inprocess`. That mode uses
HTTPX's ASGI transport, one process, and no network. Its configured concurrency
is the number of client tasks, not parallel CPU execution.

## Local measurement, 2026-11-01

Python 3.14.4, Wred 0.14, Starlette 0.52.1, HTTPX 0.28.1. One in-process run per
mode, 20,000 requests and 100 client tasks, with zero errors in each run:

| Mode | Requests/s | Median ms | p95 ms |
|---|---:|---:|---:|
| Raw Wred | 7,120.5 | 0.134 | 0.148 |
| CORS | 6,308.2 | 0.151 | 0.166 |
| Preflight | 6,042.9 | 0.157 | 0.175 |

These figures include HTTPX overhead and are a local comparison, not a production
capacity target. Also test your actual TLS/proxy topology, payload sizes, worker counts, and
sustained traffic before rollout. The separate protocol test interleaves 300 streams
and checks origin isolation, duplicate headers, and chunk preservation.

## Real HTTP check, 2026-11-01

Two Uvicorn workers, 10,000 requests per mode, 100 concurrent clients, and
10,000 configured origins. Allowed and denied origins alternate. Both workers
served application requests; preflights terminate before the handler.

| Mode | Requests/s | Median ms | p95 ms | Errors |
|---|---:|---:|---:|---:|
| CORS | 270.8 | 244.544 | 1021.415 | 0 |
| Preflight | 286.4 | 227.146 | 984.730 | 0 |

These finite checks validate policy under concurrent HTTP traffic. The client
and server share one machine; latency includes client and connection-pool
contention. These figures do not establish a production throughput ceiling or SLO.
