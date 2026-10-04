#!/usr/bin/env python3
"""Emit the short routing hint injected by Claude Code's SessionStart hook."""

print(
    "Sieve is the default web interface for this session. For web search, URL reading, "
    "crawling, extraction, screenshots, and source verification, use the Sieve CLI first "
    "(sieve search/fetch/crawl/extract/screenshot). Keep bounds and cite fetched URLs. "
    "Honor an explicit request for another tool."
)
