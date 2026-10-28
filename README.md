# Root RTSP Bridge

A Python RTSP proxy for clients such as Bosch TouchOne keypads that request only
`rtsp://username:password@bridge-ip:port/`. Each listening port selects a different
upstream camera URL, including its stream path and query string.

The proxy relays encoded RTP/RTCP packets unchanged. It does not decode, transcode,
select audio tracks, or decrypt SRTP media. RTSP control uses TCP. Media transport
is independently configurable on each side: UDP ↔ UDP, UDP ↔ TCP, TCP ↔ TCP,
and TCP ↔ UDP. Multiple tracks and client sessions have separate transport state.

## First run on a Linux device

Install Docker Engine and the Docker Compose v2 plugin. Raspberry Pi **64-bit**
Linux (ARM64) and x86-64 Linux are intended deployment targets; build on the device.
The image was validated on x86-64; ARM64 hardware validation is still needed.

```sh
git clone https://github.com/camdenyoung/root-rtsp-bridge.git
cd root-rtsp-bridge
cp config/config.example.yaml config/config.yaml
chmod 600 config/config.yaml
mkdir -p logs
printf 'BRIDGE_UID=%s\nBRIDGE_GID=%s\n' "$(id -u)" "$(id -g)" > .env
```

Edit `config/config.yaml` and replace the upstream example stream path. Use your
actual Protect URL, preserving its query string. The committed example contains
no live camera token. Start with `downstream_transport: udp` and upstream
`transport: tcp` for the first Bosch test.

```sh
docker compose up -d --build
docker compose logs -f --tail=100
```

Point the keypad at `rtsp://BRIDGE-IP:8554` (or include its configured credentials).
If the keypad requires port 554, change `listen_port` to 554 and add
`cap_add: [NET_BIND_SERVICE]` to the service in `compose.yaml` if the host requires
that capability for low ports. Ensure no other service occupies the chosen port.

Compose runs as your configured UID/GID (default 1000:1000), allowing access to
private configuration and log files without running the application as root.

The Compose file uses **Linux host networking** so negotiated UDP addresses and
ports are reachable without Docker NAT. There are no `ports:` mappings to edit.
Allow the configured RTSP TCP listening ports and UDP media range (default
20000–20199) through the device firewall, restricted to the keypad/camera network.
The device must also reach the camera RTSP port and negotiated upstream UDP ports
when using upstream UDP. Docker Desktop is not the supported initial target.

## Configuration and persistent data

Compose bind-mounts `./config` read-only at `/config` and `./logs` read-write at
`/logs`. These host directories persist across container replacement and builds.
The live `config.yaml`, certificate files, and logs are Git-ignored and excluded
from the Docker image. Back them up separately. Restart after configuration edits:

```sh
docker compose restart
```

Add entries under `rules` to map additional incoming ports:

```yaml
rules:
  - listen_port: 8554
    downstream_transport: udp
    upstream:
      url: rtsp://protect-ip:7447/camera-stream-path?enableSrtp
      transport: tcp
    credentials:
      mode: drop
  - listen_port: 8555
    downstream_transport: tcp
    upstream:
      url: rtsps://protect.example:7441/another-stream-path
      transport: tcp
      tls_ca_file: /config/protect-ca.pem
    credentials:
      mode: replace
      username: camera-user
      password: camera-password
```

Upstream RTSPS validates certificates and hostnames. For a private/self-signed
Protect certificate, mount the appropriate CA certificate in `config` and set
`tls_ca_file`; use a hostname/IP covered by that certificate. Verification cannot
be disabled. RTSPS encrypts RTSP control and interleaved TCP media; UDP media does
not automatically become encrypted. The incoming Bosch connection is plain RTSP.

Credential modes apply separately to every rule:

| Mode | Upstream behavior |
| --- | --- |
| `drop` | Discard incoming credentials and send no upstream authentication. |
| `replace` | Use the rule's configured username/password, with upstream Basic or Digest authentication. |
| `passthrough` | Request Basic authentication from the keypad, then use those credentials for upstream Basic or Digest authentication. |

In `drop` and `replace` modes, downstream clients are **not authenticated** by the
bridge. Deploy on a trusted LAN and restrict inbound access with a firewall.
`passthrough` requires the keypad to respond to a Basic challenge; Digest-only
keypads cannot supply a recoverable password for authentication to another URI.
Basic credentials are not encrypted on incoming plain RTSP. Credentials belong in
`credentials`, not in the upstream URL. Quote YAML passwords with special characters.

The downstream transport is enforced: an incompatible SETUP receives RTSP 461.
No automatic fallback is attempted. Each connection opens its own upstream session;
there is no shared camera subscription, automatic reconnect, or transcoding yet.
After a connection fails, the client must reconnect. Unicast IPv4 is the initial
supported media mode. Upstream UDP must use the connected server's IP and advertised
server ports; multicast and third-party media source addresses are unsupported.

## Logging and fault finding

Both console and `/logs/bridge.log` receive UTC timestamps, a session ID and the
incoming rule port. File logging rotates at `logging.max_bytes` with
`logging.backups` retained copies. Docker console logging also rotates.

- `INFO`: connection lifecycle, RTSP response codes, authentication challenges,
  track setup, first media packet, packet/byte totals, and inactivity warnings.
- `DEBUG`: adds RTSP request methods/CSeq, upstream responses and media endpoints.
- `TRACE`: currently an alias for the same sanitized detail as DEBUG.

No passwords, authentication headers, request URLs, stream paths, query tokens,
raw SDP, or raw exception strings are logged. Failures identify the operation
stage, exception type, and OS error number. Periodic statistics use
`logging.stats_interval` seconds; they report packets/bytes per track and per session
in both directions (RTP and RTCP together), not decoded frame counts or packet-loss
estimates. A warning reports intervals without upstream media.

```sh
tail -f logs/bridge.log
docker compose logs --tail=200
docker compose down
```

If SETUP fails, check the transport settings and response code. If PLAY succeeds
but media counters remain zero, check UDP reachability and the advertised ports.
If video packets arrive but the keypad shows no picture, investigate codec/profile
compatibility; this version forwards H.264/AAC/Opus as received without conversion.
Your Protect tests established TCP and UDP video reception, but Bosch playback and
audio compatibility still need hardware testing.

## Development and validation

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
CONFIG_PATH="$PWD/config/config.yaml" .venv/bin/python -m bridge.proxy
```

For native runs, set `logging.directory` to a writable local path. Tests run a
local mock RTSP camera and a real proxy over TCP/UDP sockets. They verify all four
transport combinations, SDP track rewriting, byte-for-byte media forwarding,
reverse RTCP feedback, Basic pass-through/replacement, Digest challenge handling,
and configuration validation. They do not establish physical keypad compatibility.

Optional container startup/persistence smoke test (requires Docker access):

```sh
docker build -t root-rtsp-bridge:test .
python3 tests/container_smoke.py
```
