"""Data-table extraction: data-vs-layout scoring and span expansion.

Reconstructed clean-room against Sieve's own tests (tests/test_crawl4ai_ports.py)
and the ``sieve extract URL --tables`` call sites; no upstream-derived code.

What counts as a "data" table is decided by a additive score over structural
signals (thead/tbody/th/caption/summary, uniform column counts) with a
penalty for presentation-role tables. Tables scoring below the threshold are
treated as layout scaffolding and skipped.

Cell values expand ``colspan`` horizontally and ``rowspan`` vertically so the
rows are rectangular-ish, matching what a human reader sees. Text inside
nested tables belongs to the nested table only — it never leaks into the
outer table's cells.

Output is bounded: rows per table, columns per row, characters per cell, and
the aggregate cell count are all capped (issue #47 budgets).
"""

from __future__ import annotations

from typing import Any

from lxml import etree

__all__ = ["extract_tables", "extract_table_data"]

# Extraction budgets (#47): a bounded HTML document can still explode into a
# huge rows/columns/cells structure, so each table and the aggregate output
# are capped.
MAX_ROWS_PER_TABLE = 5000
MAX_COLUMNS = 64
MAX_CELL_CHARS = 2000
MAX_AGGREGATE_CELLS = 50_000

# A table is a data table when its structural score reaches this value.
DEFAULT_DATA_THRESHOLD = 7


def _direct_rows(table: etree._Element) -> list[etree._Element]:
    """Rows belonging to this table, excluding rows of nested tables."""
    return [
        row for row in table.xpath(".//tr")
        if row.xpath("ancestor::table[1]")[0] is table
    ]


def _score_table(table: etree._Element) -> int:
    """Additive data-vs-layout score; >= threshold means data table.

    Signals (weights chosen to separate product/spec tables from layout
    grids): explicit thead +2, tbody +1, header cells on the first row +2
    (plus 1 when they sit in a thead), caption +2, summary attribute +1,
    uniform column count across rows +2; role="presentation"/"none" -3.
    """
    rows = _direct_rows(table)
    if not rows:
        return -100  # no rows: never a data table

    score = 0
    if table.xpath("./thead"):
        score += 2
    if table.xpath("./tbody"):
        score += 1

    header_cells = len(rows[0].xpath("./th"))
    if header_cells > 0:
        score += 2
        if table.xpath(".//thead") or table.xpath(".//tr[1]/th"):
            score += 1

    role = (table.get("role") or "").lower()
    if role in ("presentation", "none"):
        score -= 3

    col_counts = [len(r.xpath(".//td|.//th")) for r in rows]
    if col_counts:
        mean = sum(col_counts) / len(col_counts)
        variance = sum((c - mean) ** 2 for c in col_counts) / len(col_counts)
        if variance < 1:
            score += 2

    if table.xpath("./caption"):
        score += 2
    if table.get("summary"):
        score += 1

    return score


def _is_data_table(table: etree._Element, threshold: int = DEFAULT_DATA_THRESHOLD) -> bool:
    return _score_table(table) >= threshold


def _cell_text(cell: etree._Element) -> str:
    """Cell text, ignoring text contributed by nested tables."""
    parts: list[str] = []

    def visit(node: etree._Element) -> None:
        if node.text:
            parts.append(node.text)
        for child in node:
            if child.tag != "table":
                visit(child)
            if child.tail:
                parts.append(child.tail)

    visit(cell)
    return "".join(parts).strip()[:MAX_CELL_CHARS]


class MalformedSpanError(ValueError):
    """A rowspan/colspan attribute is not a valid positive integer.

    A malformed span poisons the whole table: the grid geometry can no longer
    be trusted, so callers skip the table rather than emit misaligned rows.
    """


def _span(cell: etree._Element, attr: str) -> int:
    """Read a rowspan/colspan attribute; must be a positive integer.

    Raises:
        MalformedSpanError: when the attribute is present but not a valid
            positive integer (the whole table is then skipped by the caller).
    """
    raw = (cell.get(attr) or "1").strip()
    digits = raw.lstrip("0")
    if not raw.isascii() or not raw.isdigit() or not digits:
        raise MalformedSpanError(
            f"cell has malformed {attr}={raw!r}; table geometry untrusted"
        )
    limit = MAX_COLUMNS if attr == "colspan" else MAX_ROWS_PER_TABLE
    # Avoid converting an attacker-controlled, arbitrarily long integer.
    return limit if len(digits) > len(str(limit)) else min(int(digits), limit)


def _table_headers(table: etree._Element) -> tuple[list[str], bool]:
    """First header row (colspan-expanded) and whether it came from a thead."""
    thead_rows = table.xpath("./thead/tr")
    if thead_rows:
        return _expand_spans(thead_rows[0].xpath(".//th|.//td")), True
    rows = _direct_rows(table)
    if not rows:
        return [], False
    return _expand_spans(rows[0].xpath(".//th|.//td")), False


def _expand_spans(cells: list[etree._Element]) -> list[str]:
    """Expand colspan only (used for the header row)."""
    values: list[str] = []
    for cell in cells:
        if len(values) >= MAX_COLUMNS:
            break
        text = _cell_text(cell)
        values.extend([text] * min(_span(cell, "colspan"), MAX_COLUMNS - len(values)))
    return values


def _validate_spans(table: etree._Element) -> None:
    """Reject the table when any cell carries a malformed span attribute."""
    for cell in table.xpath(".//td|.//th"):
        for attr in ("colspan", "rowspan"):
            _span(cell, attr)


def _build_rows(
    table: etree._Element,
    header_count: int,
    has_thead: bool,
    max_cells: int,
) -> list[list[str]]:
    """Body rows with colspan/rowspan expanded into a rectangular grid.

    Rowspan bookkeeping: ``carried[col] = [remaining, value]``. Cells read
    their carried value first, then their own. Rows are dropped when entirely
    empty. Output is capped at MAX_ROWS_PER_TABLE.
    """
    rows: list[list[str]] = []
    carried: dict[int, list] = {}
    body_rows = [
        tr for tr in _direct_rows(table) if not tr.xpath("ancestor::thead")
    ]
    # A headerless table whose first row is all <th> uses that row as headers.
    if not has_thead and body_rows and header_count and bool(body_rows[0].xpath("./th")):
        body_rows = body_rows[1:]

    width = max(header_count, 8) if header_count else 8
    grid_width = header_count
    for tr in body_rows:
        if len(rows) >= MAX_ROWS_PER_TABLE:
            break
        row: list[str] = []
        col = 0
        cells = list(tr.xpath("./td|./th"))
        idx = 0
        while col < MAX_COLUMNS and (idx < len(cells) or any(
            carried.get(c, [0])[0] > 0 for c in range(col, width + 5)
        )):
            # Emit carried rowspan values before reading new cells.
            while col < MAX_COLUMNS and carried.get(col, [0])[0] > 0:
                row.append(carried[col][1])
                carried[col][0] -= 1
                if carried[col][0] == 0:
                    del carried[col]
                col += 1
            if idx >= len(cells) or col >= MAX_COLUMNS:
                break
            cell = cells[idx]
            idx += 1
            text = _cell_text(cell)
            colspan = _span(cell, "colspan")
            rowspan = _span(cell, "rowspan")
            for _ in range(min(colspan, MAX_COLUMNS - col)):
                row.append(text)
                if rowspan > 1:
                    carried[col] = [rowspan - 1, text]
                col += 1
        while col < MAX_COLUMNS and carried.get(col, [0])[0] > 0:
            row.append(carried[col][1])
            carried[col][0] -= 1
            if carried[col][0] == 0:
                del carried[col]
            col += 1
        if any(str(v).strip() for v in row):
            grid_width = max(grid_width, len(row))
            if (len(rows) + 1) * grid_width > max_cells:
                break
            rows.append(row)
    return rows


def _align(
    rows: list[list[str]], headers: list[str]
) -> tuple[list[str], list[list[str]], int]:
    """Pad/truncate rows to a uniform column count; cap columns."""
    max_cols = len(headers) if headers else max((len(r) for r in rows), default=0)
    max_cols = min(max_cols, MAX_COLUMNS)
    aligned = [r[:max_cols] + [""] * (max_cols - len(r)) for r in rows]
    if not headers and max_cols > 0:
        headers = [f"Column {i + 1}" for i in range(max_cols)]
    else:
        headers = headers[:max_cols] + [""] * (max_cols - len(headers))
    return headers, aligned, max_cols


def extract_table_data(
    table: etree._Element, *, max_cells: int = MAX_AGGREGATE_CELLS
) -> dict[str, Any]:
    """Extract one data table into the canonical dict shape."""
    captions = table.xpath("./caption/text()")
    caption = captions[0].strip() if captions else ""
    summary = (table.get("summary") or "").strip()

    # Malformed spans make the grid geometry untrustworthy: skip the table.
    _validate_spans(table)

    headers, has_thead = _table_headers(table)
    rows = _build_rows(table, len(headers), has_thead, max_cells)
    headers, aligned, col_count = _align(rows, headers)
    return {
        "headers": headers,
        "rows": aligned,
        "caption": caption,
        "summary": summary,
        "metadata": {"row_count": len(aligned), "column_count": col_count},
    }


def extract_tables(
    html: str, threshold: int = DEFAULT_DATA_THRESHOLD, *, work_budget=None
) -> list[dict[str, Any]]:
    """Extract every data table from ``html``.

    Args:
        html: Raw HTML string.
        threshold: Minimum structural score for a table to count as data
            (lower accepts more layout-ish tables).

    Returns:
        A list of ``{headers, rows, caption, summary, metadata}`` dicts, in
        document order, outer tables before their nested tables. Tables that
        fail extraction are skipped, and the aggregate cell budget
        (``MAX_AGGREGATE_CELLS``) stops collection when exhausted.
    """
    from lxml import html as lh
    from sieve.dom_budget import allow_html_pass, dom_budget, measure_tree
    budget = dom_budget(work_budget)
    if not allow_html_pass(html, budget):
        return []

    try:
        tree = lh.fromstring(html)
    except Exception:
        return []

    tables = tree.xpath(".//table")
    if tree.tag == "table":
        tables = [tree] + tables

    out: list[dict[str, Any]] = []
    total_cells = 0
    for table in tables:
        measure_tree(table, budget)
        if budget.exhausted:
            break
        if not _is_data_table(table, threshold):
            continue
        try:
            data = extract_table_data(table, max_cells=MAX_AGGREGATE_CELLS - total_cells)
        except MalformedSpanError:
            continue
        except Exception:
            # Rationale: malformed individual tables are skipped within collection bounds.
            continue
        total_cells += data["metadata"]["row_count"] * data["metadata"]["column_count"]
        out.append(data)
        if total_cells >= MAX_AGGREGATE_CELLS:
            break
    return out
