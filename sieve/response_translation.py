"""Translate native fetch responses into Sieve's response envelope.

The translator owns content-type routing and extraction.  Transport modules
provide the response model and options, keeping this seam independent from the
HTTP/MCP server.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from sieve.links import extract_links
from sieve.metadata import extract_image_urls, extract_metadata
from sieve.network_capture import analyze_fragment, build_network_field, detect_ajax_shell
from sieve.pdf_extractor import extract_pdf
from sieve.reddit import parse_old_reddit_listing
from sieve.envelope import detect_page_type
from sieve.public_output import safe_error


def _check_response_size(page: Any, maximum_bytes: int) -> None:
    body = getattr(page, "body", None)
    if body and isinstance(body, bytes) and len(body) > maximum_bytes:
        raise ValueError(
            f"Response body too large ({len(body):,} bytes, max {maximum_bytes:,} bytes)"
        )


def _raw_text(body: bytes | None, encoding: str, html: bool) -> list[str]:
    text = body.decode(encoding, errors="replace") if body else ""
    if html:
        return [text]
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return [text] if text else [text]


def _attach_browser_metadata(page: Any, result: Any) -> Any:
    """Attach safe action reports and captured XHR fragments to a response.

    This deliberately runs for every response route, including JSON, PDF, and
    image responses.  The server's previous translator attached the network
    envelope after extraction, so an early content-type return must not drop
    it.
    """
    result.action_results = getattr(page, "action_results", [])
    captured = getattr(page, "captured", None)
    if not captured:
        return result
    fragments = []
    for fragment in captured:
        analyzed = analyze_fragment(fragment.get("_body", ""), fragment.get("content_type", ""))
        fragments.append({
            "url": fragment.get("url", ""),
            "status": fragment.get("status", 0),
            "content_type": fragment.get("content_type", ""),
            "size_bytes": fragment.get("size_bytes", 0),
            "text": analyzed["text"],
            "from_script": analyzed["from_script"],
            "_raw": fragment.get("_body", ""),
        })
    network = build_network_field(fragments)
    if network:
        result.network = network
    return result


def _pdf_result(page: Any, model: Callable[..., Any], content_type: str,
                extraction_type: str, fetcher_used: str, duration_ms: float, *,
                pages: str | None, password: str | None,
                include_media: bool) -> Any:
    url = page.url
    total_size = len(page.body)
    try:
        extracted = extract_pdf(page.body, extraction_type=extraction_type, pages=pages,
                                password=password, include_media=include_media)
    except ImportError as exc:
        detail = safe_error(exc)["error"]
        return model(status=200, content=[f"[PDF extraction requires sieve-cli PDF extras. {detail}]"],
                     url=url, fetcher_used=fetcher_used, duration_ms=duration_ms,
                     content_type=content_type, total_size_bytes=total_size,
                     error=f"pdf_deps_missing: {detail}")
    except Exception as exc:
        detail = safe_error(exc, fallback_category="extract")["error"]
        return model(status=200, content=[f"[PDF extraction failed: {detail}]"],
                     url=url, fetcher_used=fetcher_used, duration_ms=duration_ms,
                     content_type=content_type, total_size_bytes=total_size,
                     error=f"pdf_extract_failed: {detail}")

    # A scanned PDF is first passed through the text extractor so its metadata,
    # outline, and quality signals are retained.  If OCR extras are available,
    # replace only the content with the OCR result, matching the pre-extraction
    # server behavior.  Missing OCR extras intentionally leaves the honest
    # ``scanned_pdf`` result intact.
    base_metadata = extracted.metadata
    base_toc = extracted.table_of_contents
    base_media = extracted.media
    if extracted.scanned and not extracted.encrypted:
        try:
            from sieve.ocr import ocr_pdf, ocr_available
            if ocr_available():
                ocr_result = ocr_pdf(page.body, pages=pages, password=password)
                if ocr_result.content and not ocr_result.error:
                    extracted.content = ocr_result.content
                    extracted.error = ""
                    extracted.scanned = False
                    extracted.ocr_fallback_used = True
                    extracted.quality_score = max(extracted.quality_score, 0.9)
                    extracted.content_ok = True
                elif ocr_result.error and ocr_result.encrypted:
                    extracted = ocr_result
                elif ocr_result.error:
                    detail = "OCR failed."
                    extracted.content = [
                        f"[Scanned PDF - OCR attempted but failed: {detail}]"
                    ]
                    extracted.error = f"ocr_failed: {detail}"
        except ImportError:
            pass
        except Exception:
            # OCR is an optional enhancement.  Keep the text extraction result
            # usable if the optional engine fails unexpectedly.
            pass
    return model(status=200, content=extracted.content, url=url,
                 fetcher_used=fetcher_used, duration_ms=duration_ms,
                 content_type=content_type, total_size_bytes=total_size,
                 extracted_type="markdown", error=extracted.error,
                 content_ok=extracted.content_ok,
                 metadata=base_metadata or extracted.metadata,
                 table_of_contents=base_toc or extracted.table_of_contents,
                 quality_score=extracted.quality_score,
                 media=base_media or extracted.media)


def translate_response(page: Any, extraction_type: str, css_selector: str | None,
                       main_content_only: bool, use_trafilatura: bool,
                       fetcher_used: str, duration_ms: float, model: Callable[..., Any],
                       *, maximum_bytes: int, pages: str | None = None,
                       password: str | None = None, include_media: bool = False,
                       include_links: bool = False) -> tuple[Any, bool]:
    """Return ``(response_model, ajax_shell_detected)`` for one native page."""
    _check_response_size(page, maximum_bytes)
    from sieve.resource_budget import BudgetExceeded, current_budget
    from sieve.dom_budget import DomWorkBudget, allow_html_pass
    account = current_budget()
    body_size = len(getattr(page, "body", None) or b"")
    if account is not None and getattr(page, "_input_account", None) is not account and not account.charge("input_bytes", body_size):
        raise BudgetExceeded("Request input budget exceeded.")
    work_budget = DomWorkBudget()
    headers = getattr(page, "headers", {}) or {}
    content_type = headers.get("content-type", "") if isinstance(headers, Mapping) else ""
    body = getattr(page, "body", None)
    total_size = len(body) if isinstance(body, bytes) else 0
    encoding = getattr(page, "encoding", None) or "utf-8"

    def finish(result: Any, ajax_shell: bool = False) -> tuple[Any, bool]:
        return _attach_browser_metadata(page, result), ajax_shell

    from sieve.content_type import is_json_media
    if is_json_media(content_type) and body:
        try:
            return finish(model(status=page.status,
                                content=[body.decode(encoding, errors="replace")],
                                url=page.url, fetcher_used=fetcher_used,
                                duration_ms=duration_ms, content_type=content_type,
                                total_size_bytes=total_size))
        except Exception:
            pass

    from sieve.content_type import is_pdf_media
    is_pdf = is_pdf_media(content_type) or (
        body and body[:5].startswith(b"%PDF")
    )
    if is_pdf and body:
        return finish(_pdf_result(page, model, content_type, extraction_type,
                                  fetcher_used, duration_ms, pages=pages,
                                  password=password, include_media=include_media))

    url = getattr(page, "url", "") or ""
    if url.lower().split("?", 1)[0].endswith(".pdf") and body and not body[:5].startswith(b"%PDF"):
        head = body[:4096].decode(encoding, errors="ignore").lower()
        if any(word in head for word in ("sign in", "log in", "login", "password",
                                         "subscribe", "paywall", "access denied",
                                         "authenticate")):
            error = ("auth_required: URL ends in .pdf but returned a login/paywall page, "
                     "not the PDF. The content is behind authentication.")
        else:
            error = ("not_a_pdf: URL ends in .pdf but the response is HTML, not a PDF "
                     "(possibly a redirect/error page). Try the direct PDF link.")
        return finish(model(status=getattr(page, "status", 200), content=[f"[{error}]"],
                            url=url, fetcher_used=fetcher_used,
                            duration_ms=duration_ms, content_type=content_type,
                            total_size_bytes=total_size, extracted_type="markdown",
                            error=error, content_ok=False))

    from sieve.content_type import is_image_media
    if is_image_media(content_type) and body:
        try:
            from sieve.ocr import ocr_image_bytes, ocr_available
            available = ocr_available()
            if available:
                text = ocr_image_bytes(body)
                if text:
                    return finish(model(status=page.status, content=[text], url=url,
                                         fetcher_used=fetcher_used,
                                         duration_ms=duration_ms, content_type=content_type,
                                         total_size_bytes=total_size,
                                         extracted_type="text"))
            error = "image_ocr_empty" if available else "image_ocr_unavailable"
        except Exception as exc:
            error = f"image_ocr_failed: {safe_error(exc, fallback_category='ocr')['error']}"
        return finish(model(status=page.status, content=[f"[Image page - {error}]"],
                            url=url, fetcher_used=fetcher_used,
                            duration_ms=duration_ms, content_type=content_type,
                            total_size_bytes=total_size, extracted_type="text",
                            error=error))

    html = body.decode(encoding, errors="replace") if isinstance(body, (bytes, bytearray)) else ""
    if not allow_html_pass(html, work_budget):
        return finish(model(status=page.status, content=[], url=url, fetcher_used=fetcher_used,
                            duration_ms=duration_ms, content_type=content_type,
                            total_size_bytes=total_size, is_truncated=True, content_ok=False,
                            error="Request DOM work budget exceeded.",
                            resource_budget=account.report() if account is not None else {}))
    from sieve.extractor import extract_content as primary_extract

    def extract_content(*args, **kwargs):
        if not allow_html_pass(html, work_budget):
            return []
        return primary_extract(*args, **kwargs)
    content: list[str]
    is_old_reddit_listing = (
        "old.reddit.com" in url and "/comments/" not in url
        and extraction_type in ("markdown", "text")
    )
    if is_old_reddit_listing and body and not css_selector:
        try:
            parsed = parse_old_reddit_listing(body.decode(encoding, errors="replace"))
            content = [parsed] if parsed else extract_content(
                page, extraction_type=extraction_type, css_selector=css_selector,
                main_content_only=main_content_only,
            )
        except Exception:
            content = extract_content(
                page, extraction_type=extraction_type, css_selector=css_selector,
                main_content_only=main_content_only,
            )
    elif use_trafilatura and extraction_type in ("markdown", "text", "article", "structured"):
        from sieve.trafilatura_extractor import extract_with_trafilatura
        content = extract_with_trafilatura(page, extraction_type=extraction_type, css_selector=css_selector)
        if not css_selector and (not content or content in ([""], ["\n"])):
            content = extract_content(page, extraction_type=extraction_type, css_selector=css_selector, main_content_only=main_content_only)
    else:
        content = extract_content(page, extraction_type=extraction_type, css_selector=css_selector, main_content_only=main_content_only)
    if work_budget.exhausted:
        return finish(model(status=page.status, content=[], url=url, fetcher_used=fetcher_used,
                            duration_ms=duration_ms, content_type=content_type,
                            total_size_bytes=total_size, is_truncated=True, content_ok=False,
                            error="Request DOM work budget exceeded.",
                            resource_budget=account.report() if account is not None else {}))
    selector_empty = bool(css_selector) and not any(part.strip() for part in content)
    if selector_empty:
        return finish(model(status=page.status, content=[], url=url,
                            fetcher_used=fetcher_used, duration_ms=duration_ms,
                            content_type=content_type, total_size_bytes=total_size,
                            extracted_type=extraction_type, error="selector_empty",
                            next_action="retry_without_css_selector", content_ok=False))
    if not content or content in ([""], ["\n"]):
        content = _raw_text(body, encoding, extraction_type == "html")
    if page.status == 503 and fetcher_used == "stealthy":
        content = ["[503 via stealthy fetcher. The target server may block headless browser fingerprints. Try smart_fetch or http/dynamic fetcher instead.]" ]

    metadata: dict[str, Any] = {}
    media: list[str] = []
    links: dict[str, Any] = {}
    if html:
        try:
            metadata = extract_metadata(html, url, work_budget=work_budget)
            if include_media and allow_html_pass(html, work_budget):
                media = extract_image_urls(html, url)
            if include_links:
                links = extract_links(html, url, metadata, work_budget=work_budget)
        except Exception:
            # Metadata and link enrichment is best-effort and must not turn a
            # successful page extraction into a failed response.
            metadata = metadata or {}
            media = media or []
            links = links or {}
    result = model(status=page.status, content=content, url=url, fetcher_used=fetcher_used,
                   duration_ms=duration_ms, content_type=content_type,
                   total_size_bytes=total_size, metadata=metadata, media=media, links=links,
                   page_type=detect_page_type(html, url, content_type, sum(map(len, content))))
    result = _attach_browser_metadata(page, result)
    # AJAX-shell detection is an HTML-only signal.  Keep the same guard as the
    # server translator so binary, JSON, and non-HTML content cannot trigger a
    # browser escalation based on a text-length heuristic.
    try:
        ajax_shell = False
        from sieve.content_type import classify_media
        if classify_media(content_type) == "html" or (not content_type and result.status == 200):
            ajax_shell = detect_ajax_shell(getattr(page, "content", ""), " ".join(content))
    except Exception:
        ajax_shell = False
    return result, ajax_shell
