# API integrations

Sieve API integrations are small JSON files that describe named workflows for
an API a user has chosen. They give an agent a stable way to add “API X for
workflow Y” without allowing the integration file to run code.

Store files in `~/.sieve/integrations/` (one `*.json` file per integration).
The file contains `name`, an HTTPS `base_url`, `allowed_domains`, optional
`auth`, and named `workflows`. A workflow has a method, relative path, bounded
parameter/body templates, and an optional response `select` and `mapping`.
Templates are whole values such as `"{query}"`; they are substituted from the
explicit `--args` JSON object at invocation time.

Credentials stay outside the file. Use `"secret_ref": "env:PROVIDER_TOKEN"`
and ask the user to configure that environment variable, or use a keyring
reference such as `keyring:provider/user`. Never request, print, save, or put
the secret in a prompt, schema, log, or result.

Validate before installing and invoke explicitly:

```bash
sieve integration validate ./weather.json
sieve integration list
sieve integration invoke weather lookup --args '{"city":"Lisbon"}'
```

Sieve enforces HTTPS, the domain allowlist, DNS/private-network checks,
redirect limits, a 120-second timeout ceiling, and rejection of responses whose buffered body exceeds 2 MiB. The current 2 MiB check is an acceptance limit, not a pre-buffer memory limit; streaming enforcement is tracked separately.
Only JSON data is accepted. There is no automatic API discovery, fallback,
arbitrary code, shell execution, or template expression evaluation.

An agent adding an integration should first read the provider documentation,
write the smallest workflow schema needed by the user, run `validate`, and
then ask the user to configure the named secret reference. Invocation only
happens after the user asks for that workflow.
