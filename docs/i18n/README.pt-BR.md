<!-- i18n-summary
     source: ../../README.md
     source-revision: sha256:07e0a45dc472a3958e682f30659a28876263eab8de29a32c5d4d25a67f85bd82
     scope: summary (intentionally shortened; not a literal translation of every claim)
     synced-claims: install (uv sync quickstart), license (MIT), navigation links, capability tiers (HTTP-first, explicit browser escalation)
     language: Português
     -->

# Sieve

<p align="center">
  <a href="../../README.md">English</a> ·
  <a href="README.de.md">Deutsch</a> ·
  <a href="README.es.md">Español</a> ·
  <a href="README.zh-CN.md">简体中文</a> ·
  <a href="README.ja.md">日本語</a> · Português
</p>

Sieve é uma ferramenta local para pesquisa na web: busca fontes, acessa páginas
difíceis, rastreia sites, extrai dados e mantém as citações junto aos resultados.

## Por que Sieve?

Ele começa com HTTP e só usa um navegador quando a página exige JavaScript ou
uma sessão autorizada. CLI, SDK Python, servidor MCP e Agent Skill compartilham
saídas JSON e limites explícitos.

## Instalação

```bash
uv sync --extra dev
uv run sieve search "Python free-threading status" --max-results 5
```

Veja o [README em inglês](../../README.md) e a [documentação](../) para exemplos
completos.

## Licença

MIT.
