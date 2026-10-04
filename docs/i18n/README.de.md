<!-- i18n-summary
     source: ../../README.md
     source-revision: sha256:07e0a45dc472a3958e682f30659a28876263eab8de29a32c5d4d25a67f85bd82
     scope: summary (intentionally shortened; not a literal translation of every claim)
     synced-claims: install (uv sync quickstart), license (MIT), navigation links, capability tiers (HTTP-first, explicit browser escalation), explicit schema generation data flow
     language: Deutsch
     -->

# Sieve

<p align="center">
  <a href="../../README.md">English</a> · Deutsch ·
  <a href="README.es.md">Español</a> ·
  <a href="README.zh-CN.md">简体中文</a> ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.pt-BR.md">Português</a>
</p>

Sieve ist ein lokales Werkzeug für Web-Recherche: Es sucht, lädt schwierige
Seiten, crawlt Websites, extrahiert Daten und bewahrt Quellenangaben.

## Warum Sieve?

HTTP wird zuerst verwendet; wenn eine Seite JavaScript oder einen Browser
benötigt, wird der nächste Schritt ausdrücklich und begrenzt ausgeführt.
CLI, Python-SDK, MCP-Server und Agent-Skill verwenden dieselben JSON-Ausgaben.

## Installation

```bash
uv sync --extra dev
uv run sieve search "Python free-threading status" --max-results 5
```

Siehe die [englische README](../../README.md) und die
[Dokumentation](../) für vollständige Beispiele und Details.

Die ausdrücklich aufgerufene Schema-Generierung sendet höchstens die ersten
12.000 Zeichen des HTML-Beispiels und die ausgewählten Feldnamen an den
konfigurierten OpenAI-kompatiblen LLM-Endpunkt. Remote-Endpunkte benötigen
HTTPS; HTTP ist auf Loopback für lokale Modelle beschränkt.

## Lizenz

MIT.
