from __future__ import annotations

import hashlib
import io
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from .config import ProjectConfig
from .models import Candidate, SourceDocument, SourceType

_LAST_REQUEST_BY_DOMAIN: dict[str, float] = {}


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


def _fixture_path(candidate: Candidate, fixture_dir: Path) -> Path | None:
    direct = urlsplit(candidate.homepage)
    if direct.scheme == "fixture" and direct.path:
        candidate_path = fixture_dir / direct.path.lstrip("/")
        if candidate_path.is_file():
            return candidate_path
    stem = direct.netloc or candidate.id
    for name in (candidate.id, stem, Path(stem).stem):
        for suffix in (".html", ".htm", ".txt", ".md", ".pdf"):
            path = fixture_dir / f"{name}{suffix}"
            if path.is_file():
                return path
    return None


def _collect_fixture(candidate: Candidate, fixture_dir: Path) -> list[SourceDocument]:
    path = _fixture_path(candidate, fixture_dir)
    if path is None:
        return []
    content = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"}:
        title, text = _html_title_and_text(content.decode("utf-8", errors="replace"), candidate.name)
    elif suffix == ".pdf":
        title, text = path.stem or candidate.name, _pdf_text(content)
    else:
        title, text = path.stem or candidate.name, _normalise_text(content.decode("utf-8", errors="replace"))
    return [_source_document(candidate, f"fixture://{path.name}", title, text, SourceType.FIXTURE)]


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


def collect_candidate(candidate: Candidate, config: ProjectConfig, fixture_dir: Path | None = None) -> list[SourceDocument]:
    """Collect one candidate homepage as an HTML, PDF, or deterministic fixture."""
    if fixture_dir is not None:
        fixture_documents = _collect_fixture(candidate, Path(fixture_dir))
        if fixture_documents:
            return fixture_documents
    if candidate.homepage.startswith("fixture://"):
        return []

    response = _request(candidate.homepage, config)
    content_type = response.headers.get("content-type", "").lower()
    if len(response.content) > config.collection.max_pdf_megabytes * 1024 * 1024:
        raise ValueError(f"Response exceeds maximum collection size: {candidate.homepage}")
    if "pdf" in content_type or urlsplit(str(response.url)).path.lower().endswith(".pdf"):
        return [_source_document(candidate, str(response.url), candidate.name, _pdf_text(response.content), SourceType.PDF)]

    title, text = _html_title_and_text(response.text, candidate.name)
    if config.collection.browser_fallback and len(text) < 40 and "<script" in response.text.lower():
        rendered = _browser_text(str(response.url))
        if rendered is not None:
            title, text = rendered
    return [_source_document(candidate, str(response.url), title, text, SourceType.HTML)]
