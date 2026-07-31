from __future__ import annotations

import hashlib
import heapq
import io
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from .config import ProjectConfig
from .discovery import canonicalize_url, registrable_domain
from .models import Candidate, SourceDocument, SourceType

_LAST_REQUEST_BY_DOMAIN: dict[str, float] = {}
_DISCOVERY_TERMS = (
    "pricing", "price", "plans", "plan", "product", "features", "feature",
    "docs", "documentation", "help", "security", "价格", "套餐", "产品", "功能",
    "文档", "帮助", "安全",
)


@dataclass(frozen=True)
class _CollectedPage:
    document: SourceDocument
    html: str | None = None


def reset_throttle_state() -> None:
    """Clear process-local domain timing, primarily for deterministic tests and new runs."""
    _LAST_REQUEST_BY_DOMAIN.clear()


def _normalise_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _html_title_and_text(content: str, fallback_title: str) -> tuple[str, str]:
    soup = BeautifulSoup(content, "html.parser")
    for element in soup(["script", "style", "noscript"]):
        element.decompose()
    title = _normalise_text(soup.title.get_text(" ", strip=True)) if soup.title else fallback_title
    return title or fallback_title, _normalise_text(soup.get_text(" ", strip=True))


def _pdf_text(content: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(content))
        return _normalise_text("\n".join(page.extract_text() or "" for page in reader.pages))
    except Exception:
        return _normalise_text(content.decode("utf-8", errors="ignore"))


def _source_document(candidate: Candidate, url: str, title: str, text: str, source_type: SourceType) -> SourceDocument:
    return SourceDocument(candidate_id=candidate.id, url=url, title=title, fetched_at=datetime.now(UTC), content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(), source_type=source_type, text=text)


def _fixture_path_for_url(url: str, candidate: Candidate, fixture_dir: Path) -> Path | None:
    parsed = urlsplit(url)
    if parsed.scheme != "fixture":
        return None
    stem = parsed.netloc or candidate.id
    suffix = parsed.path.strip("/").replace("/", "-")
    names = [f"{stem}-{suffix}" if suffix else stem]
    if not suffix:
        names.extend([candidate.id, Path(stem).stem])
    for name in dict.fromkeys(names):
        for extension in (".html", ".htm", ".txt", ".md", ".pdf"):
            path = fixture_dir / f"{name}{extension}"
            if path.is_file():
                return path
    return None


def _throttle(url: str, delay_seconds: float) -> None:
    domain = urlsplit(url).netloc.lower()
    if not domain or delay_seconds <= 0:
        return
    now = time.monotonic()
    previous = _LAST_REQUEST_BY_DOMAIN.get(domain)
    if previous is not None:
        remaining = delay_seconds - (now - previous)
        if remaining > 0:
            time.sleep(remaining)
    _LAST_REQUEST_BY_DOMAIN[domain] = time.monotonic()


def _request(url: str, config: ProjectConfig) -> httpx.Response:
    """Issue one initial request plus the configured number of retry attempts."""
    attempts = 1 + config.collection.retries
    error: Exception | None = None
    with httpx.Client(timeout=config.collection.timeout_seconds, follow_redirects=True) as client:
        for attempt in range(attempts):
            try:
                _throttle(url, config.collection.per_domain_delay_seconds)
                response = client.get(url)
                response.raise_for_status()
                return response
            except (httpx.HTTPError, OSError) as exc:
                error = exc
                if attempt + 1 < attempts:
                    time.sleep(2**attempt)
    assert error is not None
    raise error


def _browser_text(url: str) -> tuple[str, str] | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle")
            title, text = page.title(), page.locator("body").inner_text()
            browser.close()
            return title, _normalise_text(text)
    except Exception:
        return None


def _same_official_site(url: str, candidate: Candidate) -> bool:
    parsed, homepage = urlsplit(url), urlsplit(candidate.homepage)
    if parsed.scheme == "fixture" or homepage.scheme == "fixture":
        return parsed.scheme == homepage.scheme == "fixture" and parsed.netloc.casefold() == homepage.netloc.casefold()
    return bool(registrable_domain(url)) and registrable_domain(url) == registrable_domain(candidate.homepage)


def _discover_links(html: str, base_url: str, candidate: Candidate) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        label = _normalise_text(anchor.get_text(" ", strip=True)).casefold()
        absolute = canonicalize_url(urljoin(base_url, href))
        signal = f"{absolute.casefold()} {label}"
        if any(term in signal for term in _DISCOVERY_TERMS) and _same_official_site(absolute, candidate):
            urls.add(absolute)
    return sorted(urls)


def _collect_one(candidate: Candidate, url: str, config: ProjectConfig, fixture_dir: Path | None) -> _CollectedPage | None:
    if fixture_dir is not None:
        path = _fixture_path_for_url(url, candidate, fixture_dir)
        if path is not None:
            content = path.read_bytes()
            suffix = path.suffix.lower()
            if suffix in {".html", ".htm"}:
                html = content.decode("utf-8", errors="replace")
                title, text = _html_title_and_text(html, candidate.name)
                return _CollectedPage(_source_document(candidate, url, title, text, SourceType.FIXTURE), html)
            if suffix == ".pdf":
                return _CollectedPage(_source_document(candidate, url, path.stem or candidate.name, _pdf_text(content), SourceType.FIXTURE))
            return _CollectedPage(_source_document(candidate, url, path.stem or candidate.name, _normalise_text(content.decode("utf-8", errors="replace")), SourceType.FIXTURE))
    if url.startswith("fixture://"):
        return None
    response = _request(url, config)
    final_url = canonicalize_url(str(response.url))
    content_type = response.headers.get("content-type", "").lower()
    if len(response.content) > config.collection.max_pdf_megabytes * 1024 * 1024:
        raise ValueError(f"Response exceeds maximum collection size: {url}")
    if "pdf" in content_type or urlsplit(final_url).path.lower().endswith(".pdf"):
        return _CollectedPage(_source_document(candidate, final_url, candidate.name, _pdf_text(response.content), SourceType.PDF))
    title, text = _html_title_and_text(response.text, candidate.name)
    if config.collection.browser_fallback and len(text) < 40 and "<script" in response.text.lower():
        rendered = _browser_text(final_url)
        if rendered is not None:
            title, text = rendered
    return _CollectedPage(_source_document(candidate, final_url, title, text, SourceType.HTML), response.text)


def _entry_urls(candidate: Candidate) -> list[str]:
    urls: list[str] = []
    for item in [*candidate.official_entry_urls, candidate.homepage]:
        url = canonicalize_url(item)
        if url not in urls and _same_official_site(url, candidate):
            urls.append(url)
    return urls


def collect_candidate(candidate: Candidate, config: ProjectConfig, fixture_dir: Path | None = None) -> list[SourceDocument]:
    """Collect official entry pages, then relevant same-site public pages, deterministically."""
    max_pages = config.collection.max_pages_per_candidate
    documents: list[SourceDocument] = []
    seen: set[str] = set()
    pending: list[str] = []

    def collect(url: str) -> None:
        if len(documents) >= max_pages or url in seen:
            return
        seen.add(url)
        page = _collect_one(candidate, url, config, Path(fixture_dir) if fixture_dir is not None else None)
        if page is None:
            return
        documents.append(page.document)
        if page.html:
            for link in _discover_links(page.html, url, candidate):
                if link not in seen:
                    heapq.heappush(pending, link)

    for url in _entry_urls(candidate):
        collect(url)
        if len(documents) >= max_pages:
            return documents
    while pending and len(documents) < max_pages:
        collect(heapq.heappop(pending))
    return documents
