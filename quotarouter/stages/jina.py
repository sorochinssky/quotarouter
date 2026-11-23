"""Jina Reader — keyless page extraction (`https://r.jina.ai/<url>`).

Free, no key: 20 requests per minute per IP. It costs neither credits nor money, which is
why the default extract chain starts here: most ordinary pages come back as clean Markdown
and Firecrawl's shared credits stay untouched for the pages Jina cannot render.

A change here is not free: a page that comes back as a bot-check wall or a JS stub is
reported as BAD_PAGE so the router tries the next stage.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .. import errors, http_client, types
from .base import Capability, Stage

logger = logging.getLogger(__name__)

BASE = "https://r.jina.ai/"
KEYLESS_RPM = 20

_STUB_MARKERS = (
    "enable javascript",
    "javascript is required",
    "checking your browser",
    "verify you are human",
    "are you a robot",
    "access denied",
    "captcha",
    "consent to the use of cookies and to",
)

_META_RE = re.compile(r"^(Title|URL Source|Published Time|Markdown Content|Warning):\s*(.*)$")


def _split_reader_output(text: str) -> tuple[str, str, str]:
    """Jina prefixes the body with its own header block; split it into (title, source_url, body).

    The header is the run of `Key: value` lines at the top; the body is everything in the
    `Markdown Content:` section, falling back to the first line that is not a header.
    """
    title = ""
    source = ""
    lines = text.splitlines()
    body_start = 0
    in_headers = True
    for index, raw in enumerate(lines):
        line = raw.strip()
        if in_headers:
            match = _META_RE.match(line)
            if match:
                key, value = match.group(1), match.group(2).strip()
                if key == "Title":
                    title = value
                elif key == "URL Source":
                    source = value
                elif key == "Markdown Content":
                    body_start = index + 1
                    break
                body_start = index + 1
                continue
            if not line:  # a blank line ends the header block — unless more headers follow
                next_is_header = any(_META_RE.match(l.strip()) for l in lines[index + 1:index + 6] if l.strip())
                if next_is_header:
                    body_start = index + 1
                    continue
                in_headers = False
                body_start = index + 1
                continue
            # First non-header text: the body starts here.
            in_headers = False
            body_start = index
            break
    return title, source, "\n".join(lines[body_start:]).strip()


def looks_like_stub(content: str, min_chars: int) -> bool:
    """True when a page text is too short or is a bot/CAPTCHA/JS wall rather than content."""
    text = (content or "").strip()
    if len(text) < min_chars:
        return True
    lowered = text.lower()[:2000]
    return any(marker in lowered for marker in _STUB_MARKERS)


class JinaStage(Stage):
    name = "jina"
    display_name = "Jina Reader"
    key_env = ""  # works without a key
    unit = "requests"
    monthly_allowance = None  # rate-limited, not metered
    capability = Capability(search=False, extract=True, extract_units=1.0)

    def has_credential(self) -> bool:
        """Jina Reader needs no key; nothing else gates it."""
        return True

    def not_ready_reason(self) -> str:
        return ""

    def search(self, query: str, limit: int) -> types.Outcome:  # pragma: no cover - not supported
        raise NotImplementedError("Jina Reader is extract-only")

    def extract(self, urls: list[str]) -> list[dict[str, Any]]:
        return self._map_urls(urls, self._one)

    def _one(self, url: str) -> dict[str, Any]:
        """One page: the reader either returns text or explains why this page did not come out."""
        from ..config import settings

        min_chars = settings().min_page_chars
        try:
            resp = http_client.get(f"{BASE}{url}", headers={"Accept": "text/plain"}, timeout=45.0)
        except errors.StageError as exc:  # classified transport failure (timeout, DNS, TLS)
            # A vendor-level failure, not this page's fault: keep the kind so the router can
            # pause the stage with the reason that actually applies.
            return types.page_error(url, f"jina: {exc.kind}: {exc}", kind=exc.kind)
        except Exception as exc:  # anything else must still be one page's problem
            logger.warning("jina: unexpected transport error for %s: %s", url, exc)
            return types.page_error(url, f"jina: transport: {exc}")
        headers = http_client.header_map(resp)
        if resp.status_code >= 400:
            # The anonymous tier blocks individual domains for a while. That is one page's
            # problem: raising here would park Jina for every other site as well.
            if errors.is_domain_block(resp.text or ""):
                return types.page_error(
                    url, f"jina: blocked for this domain: {errors.short(resp.text or '')}")
            try:
                self._raise_for_status(resp.status_code, resp.text or "", headers)
            except errors.StageError as exc:
                return types.page_error(url, f"jina: {exc.kind}: {exc}", kind=exc.kind)
            raise AssertionError("_raise_for_status returned for a >=400 response")  # pragma: no cover
        title, source, body = _split_reader_output(resp.text or "")
        if looks_like_stub(body, min_chars):
            return types.page_error(url, f"jina: page looks blocked or empty ({len(body)} chars)")
        return types.document(source or url, title, body, source_url=url)
