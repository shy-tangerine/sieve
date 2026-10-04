# Optional OSINT adapter

Sieve provides one opt-in OSINT integration for a separately installed
[Maigret](https://github.com/soxoj/maigret) executable. It is a username-only
workflow for public account checks. Sieve does not install Maigret, enable
proxies, Tor/I2P, recursion, enrichment, AI, or bypass services.

```bash
sieve osint maigret example_user --allow-osint --tags programming
```

The command emits one JSON envelope. Results use the source-neutral
`identity_account_match` shape and include adapter, Maigret version, source,
and observation time provenance. Usernames are redacted in output by default;
the SDK's `include_username=True` is an explicit operator choice. The process
has a hard deadline (60 seconds by default, 180 maximum), output-byte limit,
and site/result limit. Missing executables, timeouts, nonzero exits, malformed
NDJSON, and overflow return a failure envelope with a nonzero exit status.

Python callers can use `await SieveClient().maigret(username,
allow_osint=True)`. The base package has no Maigret dependency.

OpenOSINT remains an interoperability recipe: its current agent/MCP tools
return formatted strings rather than a stable versioned structured contract.
Run it independently when installed and configured.
