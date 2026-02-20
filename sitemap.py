"""
sitemap.py – Sitemap parsing & recursive fetching
===================================================
Supports:
  • <urlset>  → extracts <loc> URLs
  • <sitemapindex> → recursively fetches child sitemaps (max depth 3)
  • .xml.gz   → gzip decompression
  • XML namespaces are stripped for reliable tag matching
  • De-duplicates URLs while preserving original order
"""

from __future__ import annotations

import gzip
import io
from typing import Callable
from urllib.parse import urljoin

import httpx
from lxml import etree

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MAX_DEPTH = 3
_FETCH_TIMEOUT = 30  # seconds for fetching child sitemaps


def _strip_ns(tag: str) -> str:
    """Remove XML namespace prefix from a tag name."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _decompress_if_gz(data: bytes, source_hint: str = "") -> bytes:
    """If *data* looks like a gzip stream, decompress it."""
    # gzip magic number: 1f 8b
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    return data


def _parse_xml(data: bytes) -> etree._Element:
    """Parse raw XML bytes into an lxml Element, tolerating common quirks."""
    # Remove any BOM
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    parser = etree.XMLParser(recover=True, remove_comments=True)
    return etree.fromstring(data, parser=parser)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_sitemap(
    source: str | bytes,
    *,
    user_agent: str = "",
    _visited: set[str] | None = None,
    _depth: int = 0,
    progress_callback: Callable[[str], None] | None = None,
) -> list[str]:
    """
    Parse a sitemap and return a de-duplicated, order-preserved list of URLs.

    Parameters
    ----------
    source : str | bytes
        • If *bytes*, treated as raw XML (or gzip) content.
        • If *str*, treated as a URL – the sitemap is fetched first.
    user_agent : str
        User-Agent string for HTTP requests. If empty, uses Chrome macOS default.
    _visited : set
        Internal – tracks already-visited sitemap URLs to prevent loops.
    _depth : int
        Internal – current recursion depth.
    progress_callback : callable, optional
        Called with status messages (e.g. "Fetching https://…").

    Returns
    -------
    list[str]
        Ordered, unique <loc> URLs found across all (child) sitemaps.
    """
    from headers import make_headers, UA_PRESETS

    if not user_agent:
        user_agent = UA_PRESETS["Chrome macOS (default)"]
    fetch_headers = make_headers(user_agent)

    if _visited is None:
        _visited = set()

    # ------------------------------------------------------------------
    # 1. Obtain raw XML bytes
    # ------------------------------------------------------------------
    raw: bytes
    if isinstance(source, bytes):
        raw = source
    else:
        url = source.strip()
        if url in _visited:
            return []
        _visited.add(url)
        if progress_callback:
            progress_callback(f"Fetching {url}")
        try:
            resp = httpx.get(
                url,
                timeout=_FETCH_TIMEOUT,
                follow_redirects=True,
                headers=fetch_headers,
            )
            resp.raise_for_status()
            raw = resp.content
        except Exception as exc:
            if progress_callback:
                progress_callback(f"Failed to fetch {url}: {exc}")
            return []

    raw = _decompress_if_gz(raw, source_hint=str(source)[:120])

    # ------------------------------------------------------------------
    # 2. Parse XML
    # ------------------------------------------------------------------
    try:
        root = _parse_xml(raw)
    except Exception as exc:
        if progress_callback:
            progress_callback(f"XML parse error: {exc}")
        return []

    if root is None:
        if progress_callback:
            progress_callback("XML parse returned empty document – check the sitemap source.")
        return []

    root_tag = _strip_ns(root.tag)

    # ------------------------------------------------------------------
    # 3a. <sitemapindex> → recurse into children
    # ------------------------------------------------------------------
    if root_tag == "sitemapindex":
        urls: list[str] = []
        seen_order: dict[str, None] = {}
        for child in root:
            if _strip_ns(child.tag) == "sitemap":
                for loc_el in child:
                    if _strip_ns(loc_el.tag) == "loc" and loc_el.text:
                        child_url = loc_el.text.strip()
                        if _depth + 1 <= _MAX_DEPTH and child_url not in _visited:
                            child_urls = parse_sitemap(
                                child_url,
                                user_agent=user_agent,
                                _visited=_visited,
                                _depth=_depth + 1,
                                progress_callback=progress_callback,
                            )
                            for u in child_urls:
                                if u not in seen_order:
                                    seen_order[u] = None
                                    urls.append(u)
        return urls

    # ------------------------------------------------------------------
    # 3b. <urlset> → collect <loc> entries
    # ------------------------------------------------------------------
    if root_tag == "urlset":
        urls = []
        seen_order: dict[str, None] = {}
        for url_el in root:
            if _strip_ns(url_el.tag) == "url":
                for child in url_el:
                    if _strip_ns(child.tag) == "loc" and child.text:
                        loc = child.text.strip()
                        if loc not in seen_order:
                            seen_order[loc] = None
                            urls.append(loc)
        return urls

    # Unknown root element – return empty
    if progress_callback:
        progress_callback(f"⚠️ Unknown root element: <{root_tag}>")
    return []
