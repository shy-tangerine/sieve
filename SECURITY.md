# Security policy

Sieve handles remote content, browser automation, proxies, optional API keys,
and an HTTP service. Treat fetched pages and request parameters as untrusted.

## Supported versions

Security fixes are provided for the latest released Sieve version and the current `revisions` development line. Older releases may be asked to upgrade before a fix is backported. If a vulnerability also affects an older release, the advisory will identify any additional fixed versions explicitly.

## Reporting

Until a project security mailbox is established, report vulnerabilities through
a private GitHub Security Advisory on the public repository. Do not include
secrets, cookies, or personal data in an issue.

## Response expectations

Private reports will be triaged as project capacity permits. Maintainers will acknowledge actionable reports, keep sensitive details private while a fix is prepared, and publish remediation guidance or an advisory when disclosure is appropriate. Please include affected versions, reproduction steps, and impact without including real credentials or personal data.

## Deployment baseline

- Keep the service bound to `127.0.0.1` unless remote access is required.
- Set `SIEVE_AUTH_TOKEN` before any non-loopback bind.
- Keep credentials in environment or local config excluded by `.gitignore`.
- Validate crawl targets, proxy destinations, file paths, and output paths.
- Use robots.txt, terms of service, and applicable law as deployment constraints.
