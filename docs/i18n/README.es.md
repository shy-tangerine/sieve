<!-- i18n-summary
     source: ../../README.md
     source-revision: sha256:07e0a45dc472a3958e682f30659a28876263eab8de29a32c5d4d25a67f85bd82
     scope: summary (intentionally shortened; not a literal translation of every claim)
     synced-claims: install (uv sync quickstart), license (MIT), navigation links, capability tiers (HTTP-first, explicit browser escalation)
     language: Español
     -->

# Sieve

<p align="center">
  <a href="../../README.md">English</a> ·
  <a href="README.de.md">Deutsch</a> · Español ·
  <a href="README.zh-CN.md">简体中文</a> ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.pt-BR.md">Português</a>
</p>

Sieve es una herramienta local para investigar en la web: busca, recupera
páginas difíciles, rastrea sitios, extrae datos y conserva las fuentes.

## ¿Por qué Sieve?

Empieza con HTTP y solo escala a un navegador cuando la página necesita
JavaScript o una sesión autorizada. La CLI, el SDK de Python, el servidor MCP y
las skills de agentes comparten salidas JSON y límites explícitos.

## Instalación

```bash
uv sync --extra dev
uv run sieve search "Python free-threading status" --max-results 5
```

Consulta el [README en inglés](../../README.md) y la
[documentación](../) para ejemplos completos.

## Licencia

MIT.
