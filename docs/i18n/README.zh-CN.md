<!-- i18n-summary
     source: ../../README.md
     source-revision: sha256:07e0a45dc472a3958e682f30659a28876263eab8de29a32c5d4d25a67f85bd82
     scope: summary (intentionally shortened; not a literal translation of every claim)
     synced-claims: install (uv sync quickstart), license (MIT), navigation links, capability tiers (HTTP-first, explicit browser escalation)
     language: 简体中文
     -->

# Sieve

<p align="center">
  <a href="../../README.md">English</a> ·
  <a href="README.de.md">Deutsch</a> ·
  <a href="README.es.md">Español</a> · 简体中文 ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.pt-BR.md">Português</a>
</p>

Sieve 是一个本地网页研究工具：搜索网页、获取困难页面、抓取网站、提取
结构化数据，并保留来源和引用信息。

## 为什么选择 Sieve？

它优先使用 HTTP，只有在页面需要 JavaScript 或授权浏览器会话时才升级到
浏览器。CLI、Python SDK、MCP 服务器和 Agent Skill 使用相同的 JSON 输出和
明确的执行限制。

## 安装

```bash
uv sync --extra dev
uv run sieve search "Python free-threading status" --max-results 5
```

完整示例请参阅[英文 README](../../README.md)和[文档](../)。

## 许可证

MIT。
