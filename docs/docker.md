# Docker distribution

The Dockerfile defines two container profiles for the MCP HTTP transport:

| Profile | Build target | Includes | Use it when |
| --- | --- | --- | --- |
| `lite` | `--target lite` | Core Sieve, MCP, HTTP transport | You want the smallest image and can keep browser escalation outside the container. |
| `browser` | `--target browser` | `lite` plus Patchright and Chromium | You need JavaScript rendering or browser-backed escalation in the container. |

The planned GHCR image is `ghcr.io/shy-tangerine/sieve`. It is not available
until the first versioned release workflow completes. Once published, use an
immutable version tag for deployments and reserve `lite`/`browser` for local
development or deliberate upgrades.

The image intentionally runs one process and does not bundle a queue, database,
proxy, or browser profile. That keeps the default deployment light and makes
state and credentials explicit to the operator.

## Build and run

```bash
docker build --target lite -t sieve-cli:lite .
docker run --rm -p 127.0.0.1:8765:8765 sieve-cli:lite
```

For browser-backed fetching:

```bash
docker build --target browser -t sieve-cli:browser .
docker run --rm --shm-size=1g -p 127.0.0.1:8765:8765 sieve-cli:browser
```

The MCP endpoint is `http://127.0.0.1:8765/mcp` from the host. Set a strong
`SIEVE_AUTH_TOKEN` before exposing it beyond loopback; remote exposure without authentication is unsupported. The container listens on
`0.0.0.0` internally, but documented host publication binds to loopback by
default. For deliberate public publication, use an explicit host bind and a
token, for example:

```bash
docker run --rm -e SIEVE_AUTH_TOKEN -p 0.0.0.0:8765:8765 sieve-cli:lite
```

Clients must send `Authorization: Bearer <token>`. Do not publish an
unauthenticated MCP endpoint. `SIEVE_AUTH_TOKEN` is the same bearer-token
contract used by `sieve mcp serve` and is intentionally supplied at runtime.

For production deployments, keep the root filesystem read-only where practical, drop Linux capabilities, set memory/CPU/PID limits, and add a health probe against the MCP endpoint through an authenticated local client. Mount only the state/profile paths the selected capability needs; do not mount the Docker socket or broad host directories.

The Python package remains the standard installation for CLI use. The browser
profile in the container is optional, so users who only need subprocess JSON
can use the smaller image. Official registry locations will be listed in the
README when artifacts are available.
