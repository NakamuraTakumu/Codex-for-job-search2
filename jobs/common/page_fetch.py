"""Host-only HTTP page acquisition with rendering for empty public HTML. Never import this into a sandbox tool.

JobPageFetcher owns the invocation budget and fresh URL set; DNS checks, host
spacing and robots cache are shared by all jobs in this process.
"""
from __future__ import annotations

import asyncio
import email.utils
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.robotparser
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from schemas.common_models import source_url_identity

FETCH_ROOT = Path(__file__).resolve().parents[2] / "data" / "page_fetch"
USER_AGENT = "company-survey-fetch/0.1 (personal job-search research; low-volume)"
MAX_BYTES = 5 * 1024 * 1024
_LOCK = threading.Lock()
_RENDER_LOCK = threading.Lock()
RENDER_TIMEOUT = 30
_HOST_LOCKS: dict[str, threading.RLock] = {}
_ROBOTS_LOCKS: dict[str, threading.Lock] = {}
_LAST_REQUEST: dict[str, float] = {}
_COOLDOWN: dict[str, float] = {}
_ROBOTS: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def _host_lock(host: str):
    with _LOCK:
        return _HOST_LOCKS.setdefault(host, threading.RLock())


def _global_address(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return _global_address(str(ip.ipv4_mapped))
    return (ip.is_global and not any((ip.is_private, ip.is_loopback,
            ip.is_link_local, ip.is_multicast, ip.is_reserved, ip.is_unspecified)))


def checked_target(url: str) -> tuple[str, str, int, str]:
    """Return canonical URL, DNS host, port, pinned IP; reject any unsafe answer."""
    if not isinstance(url, str) or re.search(r"[\x00-\x20\x7f\\]", url):
        raise ValueError("SSRF guard: URL contains whitespace/control characters or backslash")
    try:
        p = urlsplit(url)
        explicit_port = p.port
    except ValueError as exc:
        raise ValueError(f"SSRF guard: invalid URL/port: {exc}") from exc
    if p.scheme not in {"http", "https"} or not p.hostname:
        raise ValueError("SSRF guard: only absolute http/https URLs are allowed")
    if p.username is not None or p.password is not None:
        raise ValueError("SSRF guard: URL userinfo is forbidden")
    port = explicit_port if explicit_port is not None else (443 if p.scheme == "https" else 80)
    if port not in {80, 443}:
        raise ValueError("SSRF guard: only ports 80/443 are allowed")
    host = p.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    if "%" in host or host == "localhost" or host.endswith(".localhost"):
        raise ValueError("SSRF guard: localhost/scoped addresses are forbidden")
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ips = list(dict.fromkeys(answer[4][0] for answer in answers))
    if not ips or any(not _global_address(ip) for ip in ips):
        raise ValueError("SSRF guard: every resolved address must be globally routable")
    authority = f"[{host}]" if ":" in host else host
    if p.port is not None:
        authority += f":{port}"
    canonical = urlunsplit((p.scheme, authority, p.path or "/", p.query, ""))
    return canonical, host, port, ips[0]


def _wait_for_host(host: str):
    # Called under this host's RLock, including the request itself. Other hosts
    # are independent; threads cannot reserve the same request start time.
    wait = max(0, _LAST_REQUEST.get(host, -3) + 3 - time.monotonic(),
               _COOLDOWN.get(host, 0) - time.monotonic())
    if wait:
        time.sleep(wait)
    _LAST_REQUEST[host] = time.monotonic()


def _retry_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = email.utils.parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 0
    return min(30, max(0, seconds))


def _curl_get(url: str) -> tuple[int, dict[str, str], bytes]:
    canonical, host, port, ip = checked_target(url)
    with _host_lock(host), tempfile.TemporaryDirectory(prefix="survey-fetch-") as td:
        _wait_for_host(host)
        body_path, headers_path = Path(td) / "body", Path(td) / "headers"
        resolved = f"[{ip}]" if ":" in ip else ip
        # --disable must be first: never load ~/.curlrc (cookies/auth/proxy/-L).
        command = ["/usr/bin/curl", "--disable", "--silent", "--show-error",
                   "--request", "GET", "--proto", "=http,https",
                   "--noproxy", "*", "--proxy", "", "--max-time", "20",
                   "--max-filesize", str(MAX_BYTES), "--user-agent", USER_AGENT,
                   # curl 8.5 cannot parse IPv6 literals in the HOST part.
                   # The URL literal already pins the destination; wildcard
                   # resolve also supplies the checked address without DNS.
                   "--resolve", f"{'*' if ':' in host else host}:{port}:{resolved}", "--dump-header",
                   str(headers_path), "--output", str(body_path), "--url", canonical]
        proc = subprocess.run(command, capture_output=True, timeout=22)
        if proc.returncode:
            reason = {28: "curl timeout (20 s)", 63: "response exceeds 5 MiB"}.get(
                proc.returncode, f"curl failed (exit {proc.returncode})")
            raise ValueError(reason)
        if not headers_path.exists() or not body_path.exists():
            raise ValueError("curl did not produce response headers/body")
        if body_path.stat().st_size > MAX_BYTES:
            raise ValueError("response exceeds 5 MiB")
        # Last header block ignores informational responses (e.g. HTTP 103).
        blocks = re.split(r"\r?\n\r?\n", headers_path.read_text(errors="replace").strip())
        lines = blocks[-1].splitlines()
        status = int(lines[0].split()[1])
        headers = {k.lower().strip(): v.strip() for line in lines[1:]
                   if ":" in line for k, v in [line.split(":", 1)]}
        if status in {429, 503} and headers.get("retry-after"):
            seconds = _retry_seconds(headers["retry-after"])
            _COOLDOWN[host] = time.monotonic() + seconds
            # Honor once, but do not retry/bypass a throttled/challenged page.
            if seconds:
                time.sleep(seconds)
        return status, headers, body_path.read_bytes()


def _request_chain(url: str, state: dict) -> bytes:
    for hop in range(6):
        # Set even on failure so a rejected redirect is visible to callers.
        state["final_url"] = url
        if _login_url(url):
            raise ValueError("login pages are not fetched")
        status, headers, body = _curl_get(url)
        state.update(status=status, content_type=headers.get("content-type", ""))
        if status in {301, 302, 303, 307, 308}:
            if hop == 5:
                raise ValueError("more than 5 redirects")
            location = headers.get("location")
            if not location:
                raise ValueError("redirect has no Location header")
            url = urljoin(url, location)
            continue
        return body
    raise AssertionError("unreachable")


def _robots_allowed(url: str) -> bool | None:
    _, host, _, _ = checked_target(url)
    with _LOCK:
        cache_lock = _ROBOTS_LOCKS.setdefault(host, threading.Lock())
    # Never hold a request lock across redirect hops (cross-host lock inversion).
    with cache_lock:
        if host not in _ROBOTS:
            p = urlsplit(url)
            robots_url = urlunsplit((p.scheme, p.netloc, "/robots.txt", "", ""))
            state = {}
            parser = None
            try:
                body = _request_chain(robots_url, state)
                if 200 <= state["status"] < 300:
                    parser = urllib.robotparser.RobotFileParser(robots_url)
                    parser.parse(body.decode("utf-8", errors="replace").splitlines())
                elif state["status"] in {404, 410}:
                    parser = urllib.robotparser.RobotFileParser(robots_url)
                    parser.parse([])
            except (ValueError, OSError, subprocess.SubprocessError):
                pass
            _ROBOTS[host] = parser
        parser = _ROBOTS[host]
        return parser.can_fetch(USER_AGENT, url) if parser is not None else None


class _VisibleText(HTMLParser):
    BLOCKS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section", "article", "header", "footer"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        hidden = (any(h for _, h in self.stack) or tag in {"script", "style", "noscript", "template", "head"}
                  or "hidden" in a or a.get("aria-hidden") == "true"
                  or bool(re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", a.get("style", ""), re.I)))
        if tag not in self.VOID:
            self.stack.append((tag, hidden))
        if not hidden and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        if not any(h for _, h in self.stack) and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not any(h for _, h in self.stack):
            self.parts.append(data)


def _extract(content_type: str, body: bytes) -> str:
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime == "application/pdf":
        executable = shutil.which("pdftotext")
        if not executable:
            raise ValueError("PDF extraction requires pdftotext")
        with tempfile.TemporaryDirectory(prefix="survey-pdf-") as td:
            path = Path(td) / "page.pdf"
            path.write_bytes(body)
            proc = subprocess.run([executable, "-layout", str(path), "-"],
                                  capture_output=True, timeout=20)
            if proc.returncode:
                raise ValueError("pdftotext failed")
            return proc.stdout.decode("utf-8", errors="replace").strip()
    if mime not in {"text/html", "application/xhtml+xml", "text/plain"}:
        raise ValueError(f"unsupported content type: {mime or '(missing)'}")
    match = re.search(r"charset\s*=\s*[\"']?([\w-]+)", content_type, re.I)
    if not match:
        match = re.search(r"charset\s*=\s*[\"']?([\w-]+)", body[:10000].decode("ascii", errors="ignore"), re.I)
    encoding = match.group(1) if match else "utf-8"
    try:
        raw = body.decode(encoding, errors="replace")
    except LookupError:
        raw = body.decode("utf-8", errors="replace")
    if mime == "text/plain":
        return raw.strip()
    parser = _VisibleText()
    parser.feed(raw)
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line)


CHALLENGE_TEXT_MAX_CHARS = 2000


def _is_challenge(text: str, body: bytes) -> bool:
    # Long pages can mention CAPTCHA in a comment form, so the text phrases count only on short pages.
    return bool(re.search(br"<title[^>]*>\s*just a moment", body, re.I)
                or (len(text.strip()) <= CHALLENGE_TEXT_MAX_CHARS
                    and re.search(r"just a moment|captcha|checking your browser|verify (?:you are|that you are) human|ロボットではない", text, re.I)))


class _LoginPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = False

    def handle_starttag(self, tag, attrs):
        if tag == "input" and str(dict(attrs).get("type", "")).lower() == "password":
            self.found = True

    handle_startendtag = handle_starttag


def _is_login(body: bytes) -> bool:
    parser = _LoginPage()
    parser.feed(body.decode("utf-8", errors="replace"))
    return parser.found


def _login_url(url: str) -> bool:
    return bool(re.search(r"(?:^|/)(?:login|log-in|signin|sign-in)(?:[/.]|$)",
                          urlsplit(url).path, re.I))


async def _render_html(url: str) -> tuple[bytes, str, str]:
    from playwright.async_api import async_playwright

    async with asyncio.timeout(RENDER_TIMEOUT), async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        try:
            async with asyncio.timeout(RENDER_TIMEOUT):
                context = await browser.new_context(service_workers="block", accept_downloads=False)
                errors = []

                async def guarded_route(route):
                    request = route.request
                    main_navigation = request.is_navigation_request() and request.frame.parent_frame is None
                    response = None
                    try:
                        if request.resource_type in {"image", "media", "font"}:
                            await route.abort()
                            return
                        checked_target(request.url)
                        if _login_url(request.url):
                            raise ValueError("login pages are not fetched")
                        if request.method != "GET":
                            raise ValueError("only GET requests are fetched")
                        response = await route.fetch(max_redirects=0, timeout=RENDER_TIMEOUT * 1000)
                        length = response.headers.get("content-length")
                        if length and int(length) > MAX_BYTES:
                            raise ValueError("response exceeds 5 MiB")
                        body = await response.body()
                        if len(body) > MAX_BYTES:
                            raise ValueError("response exceeds 5 MiB")
                        if not 200 <= response.status < 400:
                            raise ValueError(f"HTTP {response.status}; no bypass attempted")
                        if request.resource_type == "document" and not 300 <= response.status < 400:
                            mime = response.headers.get("content-type", "")
                            text = _extract(mime, body)
                            if _is_challenge(text, body):
                                raise ValueError("bot challenge detected; no bypass attempted")
                        await route.fulfill(response=response, body=body)
                    except Exception as exc:
                        if main_navigation:
                            errors.append(ValueError(str(exc)))
                        await route.abort()
                    finally:
                        if response is not None:
                            await response.dispose()

                await context.route("**/*", guarded_route)
                page = await context.new_page()
                try:
                    await page.goto(url, wait_until="networkidle", timeout=RENDER_TIMEOUT * 1000)
                except Exception:
                    if errors:
                        raise errors[0]
                    raise
                if errors:
                    raise errors[0]
                rendered = await page.evaluate("""limit => {
                    const html = document.documentElement.outerHTML;
                    const text = (document.body?.innerText || '').trim();
                    if (new TextEncoder().encode(html).length > limit ||
                        new TextEncoder().encode(text).length > limit) return null;
                    return {html, text};
                }""", MAX_BYTES)
                if rendered is None:
                    raise ValueError("rendered response exceeds 5 MiB")
                return rendered["html"].encode("utf-8"), rendered["text"], page.url
        finally:
            await browser.close()


def _render_page(url: str) -> tuple[bytes, str, str]:
    """A fresh browser, serialized per process; safe even in an asyncio caller."""
    checked_target(url)
    if _login_url(url):
        raise ValueError("login pages are not fetched")
    with _RENDER_LOCK, ThreadPoolExecutor(max_workers=1) as pool:
        try:
            return pool.submit(lambda: asyncio.run(_render_html(url))).result()
        except Exception as exc:
            raise ValueError(f"browser rendering failed: {exc}") from exc


def _append_jsonl(path: Path, entry: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        fcntl.flock(output.fileno(), fcntl.LOCK_EX)
        try:
            output.write(json.dumps(entry, ensure_ascii=False) + "\n")
            output.flush()
        finally:
            fcntl.flock(output.fileno(), fcntl.LOCK_UN)


def _record_failure(result: dict, attempted_at: str, context: dict | None = None):
    entry = {"requested_url": result["requested_url"], "final_url": result.get("final_url"),
             "attempted_at": attempted_at, "status": result.get("status"),
             "content_type": result.get("content_type"), "reason": result["error"], "robots_allowed": result.get("robots_allowed"),
             "category": "url_rejection" if "SSRF guard:" in result["error"] else "fetch_failure",
             **(context or {})}
    _append_jsonl(FETCH_ROOT / "failures.jsonl", entry)


def read_log(path: Path) -> list[dict]:
    try:
        with path.open(encoding="utf-8") as log:
            fcntl.flock(log.fileno(), fcntl.LOCK_SH)
            try:
                return [json.loads(line) for line in log if line.strip()]
            finally:
                fcntl.flock(log.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:
        return []


def _snapshot_extension(content_type: str) -> str:
    mime = content_type.split(";", 1)[0].strip().lower()
    return {"text/html": "html", "application/xhtml+xml": "html",
            "application/pdf": "pdf", "text/plain": "txt"}[mime]


def _manual_snapshot(url: str) -> dict | None:
    entries = [entry for entry in read_log(FETCH_ROOT / "log.jsonl")
               if entry.get("origin") == "manual" and url in (entry.get("requested_url"), entry.get("final_url"))]
    for entry in sorted(entries, key=lambda e: datetime.fromisoformat(e["fetched_at"]), reverse=True):
        sid = entry.get("snapshot_id", "")
        if not re.fullmatch(r"[0-9a-f]{64}", sid):
            continue
        snapshots = FETCH_ROOT / "snapshots"
        # Old entries hash the extracted text and have only <id>.txt. Do not
        # rewrite them or pretend their lost original body can be recovered.
        if entry.get("raw_path"):
            raw_path = snapshots / f"{sid}.{_snapshot_extension(entry['content_type'])}"
            text_path = snapshots / f"{sid}.extracted.txt"
        else:
            raw_path = text_path = snapshots / f"{sid}.txt"
        try:
            body = raw_path.read_bytes()
            text = text_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        if hashlib.sha256(body).hexdigest() == entry.get("sha256") and text.strip():
            return {**entry, "raw_path": str(raw_path.resolve()),
                    "text_path": str(text_path.resolve()), "bytes": len(body),
                    "text_chars": len(text), "robots_allowed": entry.get("robots_allowed")}
    return None


def import_snapshot(url: str, path: Path, fetched_at: str | None = None) -> dict:
    """Import user-saved data without network/DNS; caller vouches for its URL."""
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username is not None or p.password is not None:
        raise ValueError("manual import requires an absolute HTTP/HTTPS URL without userinfo")
    stamp = datetime.fromisoformat(fetched_at) if fetched_at else datetime.now(timezone.utc)
    if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
        raise ValueError("fetched_at must be timezone-aware UTC")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("saved page exceeds 5 MiB")
    body = path.read_bytes()
    if len(body) > MAX_BYTES:
        raise ValueError("saved page exceeds 5 MiB")
    suffix = path.suffix.lower()
    if suffix == ".pdf" or body.startswith(b"%PDF"):
        content_type = "application/pdf"
    elif suffix in {".html", ".htm", ".xhtml"} or re.search(br"<!doctype\s+html|<html(?:\s|>)", body[:1024], re.I):
        content_type = "text/html"
    elif suffix in {".txt", ".text"}:
        content_type = "text/plain"
    else:
        raise ValueError("saved page must be HTML, PDF or plain text (.txt)")
    if content_type == "text/html" and _is_login(body):
        raise ValueError("saved page is a login page (password input)")
    text = _extract(content_type, body)
    if not text.strip():
        raise ValueError("saved page has no visible text")
    if _is_challenge(text, body):
        raise ValueError("saved page is a bot challenge, not a source snapshot")
    result = dict(ok=True, status=200, requested_url=url, final_url=url,
                  fetched_at=stamp.isoformat(timespec="seconds"), content_type=content_type,
                  raw_path=None, text_path=None, bytes=0, text_chars=0, snapshot_id=None,
                  robots_allowed=None, error=None, origin="manual")
    _persist(result, body, text)
    return result


def unresolved_failures(root: Path | None = None) -> list[dict]:
    root = root or FETCH_ROOT
    successes = read_log(root / "log.jsonl")
    latest: dict[str, dict] = {}
    for failure in read_log(root / "failures.jsonl"):
        url = failure["requested_url"]
        if url not in latest or datetime.fromisoformat(failure["attempted_at"]) >= datetime.fromisoformat(latest[url]["attempted_at"]):
            latest[url] = failure
    return sorted((failure for url, failure in latest.items()
                   if not any(url in (entry.get("requested_url"), entry.get("final_url"))
                              and datetime.fromisoformat(entry.get("logged_at", entry["fetched_at"])) > datetime.fromisoformat(failure["attempted_at"])
                              for entry in successes)), key=lambda e: e["attempted_at"], reverse=True)


def _persist(result: dict, body: bytes, text: str):
    digest = hashlib.sha256(body).hexdigest()
    FETCH_ROOT.mkdir(parents=True, exist_ok=True)
    snapshots = FETCH_ROOT / "snapshots"
    snapshots.mkdir(exist_ok=True)
    raw_path = snapshots / f"{digest}.{_snapshot_extension(result['content_type'])}"
    text_path = snapshots / f"{digest}.extracted.txt"
    result.update(snapshot_id=digest, raw_path=str(raw_path.resolve()),
                  text_path=str(text_path.resolve()), bytes=len(body), text_chars=len(text))
    # One lock covers immutable snapshot creation and append, across processes.
    with (FETCH_ROOT / "log.jsonl").open("a", encoding="utf-8") as log:
        fcntl.flock(log.fileno(), fcntl.LOCK_EX)
        try:
            if not raw_path.exists():
                raw_path.write_bytes(body)
            if not text_path.exists():
                text_path.write_text(text, encoding="utf-8")
            entry = {key: result[key] for key in ("requested_url", "final_url", "fetched_at", "status", "content_type", "snapshot_id", "origin", "raw_path", "text_path", "bytes", "text_chars", "robots_allowed")}
            if result.get("browser_rendered"):
                entry["browser_rendered"] = True
            entry["sha256"] = digest
            # Import time can differ from manual acquisition time. Resolution
            # uses when a successful entry was logged, not an old saved date.
            entry["logged_at"] = datetime.now(timezone.utc).isoformat()
            log.write(json.dumps(entry, ensure_ascii=False) + "\n")
            log.flush()
        finally:
            fcntl.flock(log.fileno(), fcntl.LOCK_UN)


def fetch_page(url: str) -> str:
    """Fetch one public page on this host and return JSON; never throw to a model."""
    return _fetch_page(url)


def _fetch_page(url: str, context: dict | None = None) -> str:
    attempted_at = datetime.now(timezone.utc).isoformat()
    result = dict(ok=False, status=None, requested_url=url, final_url=url,
                  fetched_at=None, content_type=None, raw_path=None, text_path=None,
                  bytes=0, text_chars=0,
                  snapshot_id=None, robots_allowed=None, error=None, origin="host")
    try:
        checked_target(url)  # reject before even fetching robots.txt
        if _login_url(url):
            raise ValueError("login pages are not fetched")
        result["robots_allowed"] = _robots_allowed(url)
        body = _request_chain(url, result)
        if len(body) > MAX_BYTES:
            raise ValueError("response exceeds 5 MiB")
        result["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if result["final_url"] != url:
            final_allowed = _robots_allowed(result["final_url"])
            if final_allowed is False or result["robots_allowed"] is False:
                result["robots_allowed"] = False
            elif final_allowed is None:
                result["robots_allowed"] = None
        status = result["status"]
        if status in {403, 429, 503}:
            raise ValueError(f"HTTP {status}: blocked/throttled/unavailable; no bypass attempted")
        if not 200 <= status < 300:
            raise ValueError(f"HTTP {status}")
        text = _extract(result["content_type"], body)
        if _is_challenge(text, body):
            raise ValueError("bot challenge detected; no bypass attempted")
        mime = result["content_type"].split(";", 1)[0].strip().lower()
        if not text.strip() and mime in {"text/html", "application/xhtml+xml"}:
            body, text, final_url = _render_page(result["final_url"])
            if final_url != result["final_url"]:
                final_allowed = _robots_allowed(final_url)
                if final_allowed is False or result["robots_allowed"] is False:
                    result["robots_allowed"] = False
                elif final_allowed is None:
                    result["robots_allowed"] = None
            result.update(browser_rendered=True, final_url=final_url,
                          fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          content_type="text/html; charset=utf-8")
            if len(body) > MAX_BYTES or len(text.encode("utf-8")) > MAX_BYTES:
                raise ValueError("rendered response exceeds 5 MiB")
            if _is_challenge(text, body):
                raise ValueError("bot challenge detected; no bypass attempted")
        if not text.strip():
            raise ValueError("empty visible text")
        _persist(result, body, text)
        result["ok"] = True
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        result.update(error=f"{type(exc).__name__}: {exc}", raw_path=None, text_path=None,
                      bytes=0, text_chars=0, snapshot_id=None)
        try:
            _record_failure(result, attempted_at, context)
            # URL/SSRF rejection is not a live fetch failure: never use a manual
            # snapshot to turn an unsafe request into an apparent fetch success.
            if "SSRF guard:" not in result["error"]:
                manual = _manual_snapshot(url)
                if manual:
                    live_error = result["error"]
                    result.pop("browser_rendered", None)
                    result.update(ok=True, status=manual["status"], final_url=manual["final_url"],
                                  fetched_at=manual["fetched_at"], content_type=manual["content_type"],
                                  raw_path=manual["raw_path"], text_path=manual["text_path"],
                                  bytes=manual["bytes"], text_chars=manual["text_chars"],
                                  robots_allowed=manual["robots_allowed"],
                                  snapshot_id=manual["snapshot_id"], error=None, origin="manual",
                                  live_error=live_error)
        except (ValueError, OSError) as log_exc:
            result["error"] += f"; failure log/manual snapshot unavailable: {log_exc}"
    if result["fetched_at"] is None:
        result["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return json.dumps(result, ensure_ascii=False)


class JobPageFetcher:
    """One instance per job, shared by attempts and validation repair turns."""
    def __init__(self, cap: int = 40, *, company_id: str | None = None,
                 recruitment_year: int | None = None, job_name: str | None = None):
        self.cap = cap
        self.calls = 0
        self.context = {k: v for k, v in dict(company_id=company_id,
                       recruitment_year=recruitment_year, job_name=job_name).items() if v is not None}
        self.successful_urls: set[str] = set()
        self._successes: dict[str, dict] = {}
        self._lock = threading.RLock()

    def fetch_page(self, url: str) -> str:
        with self._lock:
            if self.calls >= self.cap:
                result = dict(ok=False, status=None, requested_url=url,
                    final_url=None, fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    content_type=None, raw_path=None, text_path=None, bytes=0,
                    text_chars=0, snapshot_id=None, origin="host",
                    robots_allowed=None, error=f"per-job fetch cap ({self.cap}) exceeded")
                try:
                    _record_failure(result, datetime.now(timezone.utc).isoformat(), self.context)
                except OSError as exc:
                    result["error"] += f"; failure logging failed: {exc}"
                return json.dumps(result)
            self.calls += 1
            # Successful evidence is immutable within a job. A later failing
            # call cannot replace a fresh snapshot with an older manual/cache copy.
            try:
                key = source_url_identity(url)
            except (ValueError, TypeError):
                key = repr(url)
            if key in self._successes:
                try:
                    checked_target(url)  # cache identity must not bypass URL/SSRF guards
                except (ValueError, OSError):
                    return _fetch_page(url, self.context)
                return json.dumps(self._successes[key], ensure_ascii=False)
            raw = _fetch_page(url, self.context)
            result = json.loads(raw)
            if result["ok"]:
                for alias in (result["requested_url"], result["final_url"]):
                    self.successful_urls.add(alias)
                    self._successes[source_url_identity(alias)] = result
            return raw

    def fetched_urls(self) -> set[str]:
        with self._lock:
            return self.successful_urls.copy()
