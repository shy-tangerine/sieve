# Authenticated remote MCP pilot

This runbook prepares a maintainer who later chooses to operate Sieve over
HTTPS. It does not deploy a service or create provider accounts. Local stdio
remains the default integration.

## Preconditions

1. Publish and pull a versioned `lite` image from GHCR. Use the `browser`
   image only when JavaScript rendering is required and its memory budget has
   been measured.
2. Run the container behind a TLS terminating proxy or managed HTTPS service.
   Keep the container port private to that service.
3. Set `SIEVE_AUTH_TOKEN` from the host's secret store. Never place the token
   in an image, committed command, URL, or client example.
4. Define an allowlist of client origins at the proxy. Sieve's MCP endpoint
   authenticates bearer requests; origin policy and rate limiting belong at
   the deployment boundary.

## Smoke test

Use a disposable token supplied by the operator and keep response bodies and
headers out of logs:

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${SIEVE_AUTH_TOKEN}" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  https://mcp.example.invalid/mcp
```

Also verify that the same request without `Authorization` returns `401`, an
incorrect token returns `401`, and a disallowed origin is rejected by the
proxy. The example hostname is reserved and is not a live endpoint.

## Operational gates

Before accepting remote traffic, record bounded request timeouts, crawl/page
limits, browser concurrency, memory and egress ceilings, redacted request
logging, spend alerts, and a rollback image tag. Exercise reconnects, a
crashed worker, an expired token, and an upstream block. Do not enable browser
escalation until the `lite` pilot is stable and the operator has a lawful use
case for authenticated browser state.

Smithery evaluation starts only after this direct endpoint passes the gates.
The official MCP Registry provides discovery metadata; it does not host this
service.
