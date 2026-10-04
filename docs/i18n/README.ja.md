<!-- i18n-summary
     source: ../../README.md
     source-revision: sha256:07e0a45dc472a3958e682f30659a28876263eab8de29a32c5d4d25a67f85bd82
     scope: summary (intentionally shortened; not a literal translation of every claim)
     synced-claims: install (uv sync quickstart), license (MIT), navigation links, capability tiers (HTTP-first, explicit browser escalation)
     language: 日本語
     -->

# Sieve

<p align="center">
  <a href="../../README.md">English</a> ·
  <a href="README.de.md">Deutsch</a> ·
  <a href="README.es.md">Español</a> ·
  <a href="README.zh-CN.md">简体中文</a> · 日本語 ·
  <a href="README.pt-BR.md">Português</a>
</p>

Sieve は、ウェブ検索、取得、クロール、データ抽出、引用元の保持を一つの
ローカルツールで行うためのウェブ調査エンジンです。

## Sieve を使う理由

まず HTTP で取得し、JavaScript や認証済みブラウザーが必要な場合だけ、
明示的な制限のもとでブラウザーへ切り替えます。CLI、Python SDK、MCP
サーバー、Agent Skill は同じ JSON 出力を共有します。

## インストール

```bash
uv sync --extra dev
uv run sieve search "Python free-threading status" --max-results 5
```

詳しい例は[英語の README](../../README.md)と[ドキュメント](../)を参照して
ください。

## ライセンス

MIT。
