# Optional server assistance

Server assistance moves supported world-generation work to a separate Python/native
worker service. BeamMP continues to handle its normal multiplayer protocol. Clients
still render the world, install collision and simulate their vehicles locally.

The first implementation accepts distant geometry and geographic road-plan jobs.
It checks generator identity, world settings and known source metadata. It does not
establish a complete immutable snapshot of every raw elevation tile. Near collision
geometry remains local. Imagery and music are not redistributed by this service.

This feature is opt-in. A configured client can fall back to its local generator
when the helper is busy, unreachable or incompatible. A slow first request can time
out locally while the bounded server job finishes and becomes reusable later.
Performance and capacity must be measured with the intended route and player count.
Offline transport tests do not demonstrate improved FPS or 100-player capacity.

## Host setup from source

Use the exact same server source and native version as participating clients.
Build the native extension for the host OS; a Windows `.pyd` cannot run on Linux.
Create a virtual environment, install `server/requirements.txt`, and run the existing
`python tools/build_native.py` with a Rust toolchain installed. Keep source and
dependencies read-only to the service account and give it a dedicated writable cache.

Provide three private configuration files outside the repository and release assets:

- A random assistance token of at least 32 ASCII characters; generate it with
  Python's `secrets.token_urlsafe(32)`. This is separate from the BeamMP AuthKey.
- A TLS certificate and private key. The certificate must include the IP clients
  connect to as an IP Subject Alternative Name. Clients verify it against their
  configured CA file. Distribute only the certificate/CA, never the private key.
- A JSON settings file with all six booleans below, matching participating clients.

```json
{
  "roads": true,
  "grading": true,
  "road_widths": true,
  "airports": true,
  "sea": true,
  "roads_in_ground": false
}
```

Example foreground startup (replace paths and address with the host's configuration):

```sh
python tools/serve_assist.py --host 192.168.86.101 --port 47623 \
  --cert /etc/beam-earth-assist/server.crt \
  --key /etc/beam-earth-assist/server.key \
  --token-file /etc/beam-earth-assist/token \
  --settings /etc/beam-earth-assist/settings.json \
  --cache-dir /var/cache/beam-earth-assist \
  --workers 4 --max-jobs 8 --max-connections 16 \
  --reply-cache-mb 128 --disk-budget-gb 8
```

Use a non-login service account. On Linux, make the token and private key readable
only by that account, or root plus a dedicated service group. On Windows, restrict
their file ACLs to the administrator and service identity. Do not pass tokens in
command arguments or commit them. The command exposes only file paths.

Start on a LAN or protected network for acceptance testing. TCP 47623 is a separate
asset-generation endpoint; TCP/UDP 30814 remains BeamMP traffic. Opening the helper
to additional clients needs explicit firewall/network configuration. Its launcher
does not change firewall rules or publicly advertise an endpoint.

## Client configuration

Store an absolute-path JSON configuration outside the mod/release package:

```json
{
  "schema": 1,
  "host": "192.168.86.101",
  "port": 47623,
  "ca_file": "C:/Users/YourName/beam-earth-assist/server.crt",
  "token_file": "C:/Users/YourName/beam-earth-assist/token",
  "slots": 2,
  "timeout_s": 4
}
```

Set `BNGE_ASSIST_CONFIG` to that file's absolute path before launching the matching
companion. The destination must be a literal IP with a matching certificate IP SAN;
DNS lookups cannot extend the client's absolute request deadline. Each process has
bounded assistance slots. An invalid configuration disables the optional helper.
Removing the variable restores ordinary local generation on the next launch.

## Resource and recovery behavior

- One aggregate pool has four workers by default. Requests cannot create a pool
  for each settings combination; incompatible settings are rejected.
- At most eight unique jobs are admitted by default. Identical in-flight requests
  join one job. Further work receives a busy reply and can run locally.
- At most sixteen connection handlers run, including TLS handshakes. Handshakes
  and frames have deadlines; a slow byte drip cannot hold a frame indefinitely.
- Headers are limited to 64 KiB; request blobs are refused. Replies allow up to
  eight blobs and 64 MiB total framing, metadata and blob bytes. The client performs
  additional array, geometry and road-artifact validation before using the data.
- Only explicitly complete, cacheable successful results enter the in-memory reply
  cache. It has a 128 MiB byte budget and five-minute TTL by default; failures and
  incomplete results remain retryable.
- A response can say `pending` after three seconds. The server retains that job's
  admission until the actual worker future completes. A disconnected client does
  not create an unbounded queue of abandoned jobs.
- A job running for 90 seconds triggers worker-pool recycling. Other jobs in that
  pool may fail and fall back locally. Worker restart throttling avoids rapid loops.
- The disk guard scans the dedicated cache and refuses new jobs when its configured
  budget is reached or less than 1 GB remains free. Cached replies can still be served.
  It does not delete active files. Running jobs may overshoot the admission budget;
  use a filesystem quota for a hard disk cap. Stop the helper before maintaining
  its dedicated cache. Do not point it at a client's active cache directory.
- SIGINT/SIGTERM stop accepting work, close connections, allow a short completion
  grace period and terminate remaining worker processes. BeamMP is independent.

On an 8-CPU/16-GB host, start with four workers and a helper memory limit around
8 GB, then measure before increasing it. Reserve capacity for BeamMP, DNS and the OS.
The existing BeamMP 14-GiB ceiling is not a reservation and must not be added to a
second 14-GiB helper allowance as though the machine had 28 GiB available.

## Diagnostics and validation

The service logs aggregate submitted/completed/failed jobs, busy/pending replies,
joined duplicate requests, reply-cache hits/bytes and active jobs every 30 seconds.
It does not log authentication tokens, client request bodies or coordinates.

Run the focused transport and scheduling tests:

```sh
python -m unittest discover -s server/tests -p test_assistservice.py -v
python -m unittest discover -s server/tests -p test_assistclient.py -v
```

TLS tests generate disposable test certificates with OpenSSL. Missing OpenSSL
produces an explicit TLS-test skip; install it before treating transport verification
as complete. Tests cover trusted/untrusted certificates, authentication, version
rejection, malformed payloads, simultaneous requests, busy/pending fallback,
single-flight reuse, cache bounds and slow-client deadlines.

Live acceptance should compare the same route and source settings with assistance
off/on, first-player versus subsequent-player loads, and a deliberately unavailable
helper. Record client CPU, local versus remote jobs, total terrain latency, response
bytes, cache hit rate and frame-time spikes. A successful server response alone is
not proof of smoother driving.
