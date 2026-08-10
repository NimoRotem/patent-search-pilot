"""Recover patent specification text from Google Patents without depending on SerpApi.

Google's document page is the cheapest useful source and normally answers directly.  When it
rate-limits or serves an incomplete shell, the same URL is fetched through ScrapingBee.  The
caller receives only parsed text and source provenance; database persistence stays in
``enrich.py`` so every recovery source follows one schema path.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser

import requests

import pubnorm


MAX_HTML_BYTES = 25_000_000
DIRECT_TIMEOUT = 30
SCRAPINGBEE_TIMEOUT = 60
_RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


class GoogleTextParser(HTMLParser):
    """Tolerant extractor for Google Patents' itemprop sections."""

    BLOCKS = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "tr"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
            "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.claims = []
        self.description = []
        self.abstract = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        inherited = self.stack[-1][1] if self.stack else None
        item = (attrs.get("itemprop") or "").lower()
        classes = set((attrs.get("class") or "").lower().split())
        section = item if item in ("claims", "description", "abstract") else inherited
        if not section and "claims" in classes:
            section = "claims"
        elif not section and "abstract" in classes:
            section = "abstract"
        if section and tag in self.BLOCKS:
            getattr(self, section).append("\n")
        if tag not in self.VOID:
            self.stack.append((tag, section))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if not self.stack:
            return
        section = self.stack[-1][1]
        if section and tag in self.BLOCKS:
            getattr(self, section).append("\n")
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.stack and self.stack[-1][1]:
            getattr(self, self.stack[-1][1]).append(data)

    @staticmethod
    def clean(parts):
        text = html.unescape("".join(parts))
        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
        out = []
        for line in lines:
            if line and (not out or out[-1] != line):
                out.append(line)
        return out

    def result(self):
        return {"claims": self.clean(self.claims),
                "description": self.clean(self.description),
                "abstract": self.clean(self.abstract)}


def parse_google_html(body: str) -> dict:
    parser = GoogleTextParser()
    parser.feed(body or "")
    return parser.result()


def _usable(response) -> dict:
    if response is None or response.status_code != 200:
        return {}
    content = response.content or b""
    if not content or len(content) > MAX_HTML_BYTES:
        return {}
    return parse_google_html(response.text)


def _urls(pub: str) -> list[str]:
    urls = []
    primary = pubnorm.google_url(pub)
    if primary:
        urls.append(primary)
    for candidate in pubnorm.mongo_candidates(pub):
        compact = re.sub(r"[^A-Za-z0-9]", "", candidate).upper()
        parsed = pubnorm.parse(compact)
        if not parsed or not parsed[2]:
            continue
        url = f"https://patents.google.com/patent/{compact}/en"
        if url not in urls:
            urls.append(url)
        if len(urls) >= 3:
            break
    return urls


def _merge(target: dict, candidate: dict):
    for key in ("claims", "description", "abstract"):
        if not target.get(key) and candidate.get(key):
            target[key] = candidate[key]


def fetch_google_full_text(pub: str, scrapingbee_key: str = "") -> dict:
    """Return parsed claims/description/abstract, using ScrapingBee only when needed."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Rotem Patents research/3.0)",
        "Accept-Language": "en-US,en;q=0.8",
    }
    result = {"claims": [], "description": [], "abstract": [], "source": ""}
    urls = _urls(pub)
    for url in urls:
        response = None
        try:
            response = requests.get(url, headers=headers, timeout=DIRECT_TIMEOUT)
        except requests.RequestException:
            pass
        parsed = _usable(response)
        if parsed:
            _merge(result, parsed)
            result["source"] = "google_patents:direct"
        if result["claims"] and result["description"]:
            return result
        if response is None or response.status_code in _RETRYABLE:
            break

    if not scrapingbee_key or not urls:
        return result
    # One proxy attempt against the canonical spelling. ScrapingBee is the rate-limit fallback,
    # not a multiplier over every speculative publication-number spelling.
    try:
        response = requests.get(
            "https://app.scrapingbee.com/api/v1/",
            params={"api_key": scrapingbee_key, "url": urls[0], "render_js": "false"},
            headers={"Accept": "text/html"}, timeout=SCRAPINGBEE_TIMEOUT)
    except requests.RequestException:
        return result
    parsed = _usable(response)
    if parsed:
        had_direct = bool(result["source"])
        _merge(result, parsed)
        result["source"] = ("google_patents:direct+scrapingbee" if had_direct
                            else "scrapingbee:google_patents")
    return result
