"""Safe internet client shared by every web tool and the built-in web connector.

Guards that stay on in every internet mode (they protect the owner, not limit the agents):
  * public addresses only: hostnames are resolved and loopback, private (RFC1918/ULA),
    link-local, cloud-metadata, multicast and reserved addresses are refused - on every redirect
    hop - unless the owner allowlisted that host (e.g. a LAN service);
  * no agent-supplied credentials: Authorization / Cookie / Proxy-* headers are stripped;
  * bounded size, time and request rate; http(s) only.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import httpx

USER_AGENT = "NEXUS-agent/0.1 (local-first research assistant; owner-operated)"
MAX_BYTES = 8_000_000
MAX_TEXT = 400_000
FORBIDDEN_HEADERS = {"authorization", "cookie", "proxy-authorization", "x-api-key", "host"}


class WebPolicyError(Exception):
    """The request is refused by policy (scheme, address, size, rate, mode)."""


class WebError(Exception):
    """The request was attempted and failed (network, HTTP status, parse)."""


Resolver = Callable[[str], list[str]]


def system_resolver(host: str) -> list[str]:
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)})


def is_public_ip(ip: str) -> bool:
    a = ipaddress.ip_address(ip.split("%")[0])
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved
                or a.is_unspecified or (isinstance(a, ipaddress.IPv4Address) and a in ipaddress.ip_network("100.64.0.0/10")))


@dataclass
class WebPolicy:
    mode: str = "read_write"          # off | read | read_write
    blocked_domains: list[str] = field(default_factory=list)
    allowed_private_hosts: list[str] = field(default_factory=list)
    allowlist_when_off: list[str] = field(default_factory=list)  # legacy fetch_allowlist
    max_requests_per_minute: int = 120
    contact: str = ""  # owner-provided contact for the User-Agent (some sites, e.g. Wikimedia, require it)

    def user_agent(self) -> str:
        return f"NEXUS-agent/0.1 ({self.contact}; owner-operated agent)" if self.contact else USER_AGENT

    @classmethod
    def from_project(cls, settings: dict[str, Any]) -> "WebPolicy":
        net = settings.get("internet") or {}
        return cls(mode=net.get("mode", "read_write"), blocked_domains=list(net.get("blocked_domains", [])),
                   allowed_private_hosts=list(net.get("allowed_private_hosts", [])),
                   allowlist_when_off=list(settings.get("fetch_allowlist", [])),
                   max_requests_per_minute=int(net.get("max_requests_per_minute", 120)),
                   contact=str(net.get("contact", ""))[:200])


@dataclass
class WebResponse:
    url: str
    status: int
    headers: dict[str, str]
    content: bytes
    elapsed_s: float

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    def text(self) -> str:
        m = re.search(r"charset=([\w-]+)", self.headers.get("content-type", ""), re.I)
        enc = m.group(1) if m else "utf-8"
        try:
            return self.content.decode(enc, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        import json

        try:
            return json.loads(self.content)
        except ValueError as exc:
            raise WebError(f"response is not JSON ({self.content_type})") from exc


class _RateLimiter:
    def __init__(self) -> None:
        self.calls: dict[str, list[float]] = {}

    def check(self, key: str, per_minute: int) -> None:
        now = time.monotonic()
        q = [t for t in self.calls.get(key, []) if now - t < 60]
        if len(q) >= per_minute:
            raise WebPolicyError(f"internet rate limit reached ({per_minute} requests/minute for this project)")
        q.append(now)
        self.calls[key] = q


class SafeWebClient:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, resolver: Resolver | None = None):
        self.transport = transport
        self.resolver = resolver or system_resolver
        self.rate = _RateLimiter()

    async def _check_host(self, url: str, policy: WebPolicy, *, write: bool) -> str | None:
        """Validate the URL; return the vetted IP to connect to (None for owner-allowlisted hosts)."""
        s = urlsplit(url)
        if s.scheme not in ("http", "https"):
            raise WebPolicyError(f"only http(s) URLs are allowed, not '{s.scheme}'")
        host = (s.hostname or "").lower().rstrip(".")
        if not host:
            raise WebPolicyError("URL has no host")
        if s.username or s.password:
            raise WebPolicyError("credentials in URLs are not allowed")
        if any(host == d or host.endswith("." + d) for d in policy.blocked_domains):
            raise WebPolicyError(f"domain '{host}' is blocked by the owner")
        if policy.mode == "off":
            if write or not any(host == d or host.endswith("." + d) for d in policy.allowlist_when_off):
                raise WebPolicyError(f"internet access is off for this project ('{host}' not in the allowlist)")
        elif policy.mode == "read" and write:
            raise WebPolicyError("this project allows reading the internet but not writing to it")
        if host in policy.allowed_private_hosts:
            return None
        try:
            literal = ipaddress.ip_address(host.strip("[]"))
            ips = [str(literal)]
        except ValueError:
            try:
                ips = await asyncio.to_thread(self.resolver, host)
            except OSError as exc:
                raise WebError(f"could not resolve '{host}': {exc}") from None
        if not ips:
            raise WebError(f"'{host}' did not resolve")
        bad = [ip for ip in ips if not is_public_ip(ip)]
        if bad:
            raise WebPolicyError(f"'{host}' resolves to a non-public address ({bad[0]}); local-network and metadata "
                                 f"addresses are blocked unless the owner allowlists the host")
        return ips[0]

    async def request(self, method: str, url: str, *, policy: WebPolicy, project_key: str, headers: dict[str, str] | None = None,
                      params: dict[str, Any] | None = None, json_body: Any = None, data: dict[str, Any] | None = None,
                      content: bytes | None = None, max_bytes: int = MAX_BYTES, timeout_s: float = 25.0,
                      max_redirects: int = 5, trusted_headers: dict[str, str] | None = None,
                      intent: str | None = None) -> WebResponse:
        """trusted_headers: owner-configured credentials (e.g. a search API key) - never agent input.
        intent='read' marks a POST that only queries (e.g. a search API) as a read."""
        method = method.upper()
        write = method not in ("GET", "HEAD") and intent != "read"
        self.rate.check(project_key, policy.max_requests_per_minute)
        hdrs = {"user-agent": policy.user_agent(), "accept": "*/*"}
        for k, v in (headers or {}).items():
            if k.lower() not in FORBIDDEN_HEADERS and isinstance(v, str) and len(v) < 2000:
                hdrs[k] = v
        hdrs.update(trusted_headers or {})
        t0 = time.monotonic()
        current = url
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=min(10.0, timeout_s)),
                                     transport=self.transport, follow_redirects=False) as client:
            for hop in range(max_redirects + 1):
                ip = await self._check_host(current, policy, write=write)
                # Connect to the vetted address (defeats DNS rebinding); keep Host and TLS SNI/certificate
                # verification bound to the real hostname.
                target, ext, hop_headers = current, {}, dict(hdrs)
                if ip is not None:
                    sp = urlsplit(current)
                    netloc = (f"[{ip}]" if ":" in ip else ip) + (f":{sp.port}" if sp.port else "")
                    target = sp._replace(netloc=netloc).geturl()
                    hop_headers["host"] = sp.netloc.rsplit("@", 1)[-1]
                    if sp.scheme == "https":
                        ext = {"sni_hostname": sp.hostname}
                try:
                    req = client.build_request(method, target, headers=hop_headers, params=params if hop == 0 else None,
                                               json=json_body if hop == 0 else None, data=data if hop == 0 else None,
                                               content=content if hop == 0 else None, extensions=ext)
                    r = await client.send(req, stream=True)
                    try:
                        if r.status_code in (301, 302, 303, 307, 308) and "location" in r.headers:
                            current = urljoin(current, r.headers["location"])
                            if r.status_code == 303 or (r.status_code in (301, 302) and method == "POST"):
                                method, write = "GET", False
                            if urlsplit(current).hostname != urlsplit(url).hostname:
                                for k in list(trusted_headers or {}):
                                    hop_headers.pop(k, None)
                                    hdrs.pop(k, None)  # never forward owner credentials to another host
                            continue
                        declared = int(r.headers.get("content-length") or 0)
                        if declared > max_bytes:
                            raise WebPolicyError(f"response too large ({declared} bytes > {max_bytes})")
                        buf = bytearray()
                        async for chunk in r.aiter_bytes():
                            buf += chunk
                            if len(buf) > max_bytes:
                                raise WebPolicyError(f"response exceeded {max_bytes} bytes")
                        return WebResponse(current, r.status_code, {k.lower(): v for k, v in r.headers.items()},
                                           bytes(buf), time.monotonic() - t0)
                    finally:
                        await r.aclose()
                except httpx.TimeoutException as exc:
                    raise WebError(f"timed out ({type(exc).__name__})") from None
                except httpx.HTTPError as exc:
                    raise WebError(f"network error: {type(exc).__name__}: {exc}"[:300]) from None
        raise WebError(f"too many redirects (> {max_redirects})")


# ---- HTML to readable text ----------------------------------------------------------------
class _Readable(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "head"}
    BLOCK = {"p", "div", "section", "article", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote",
             "header", "footer", "table", "ul", "ol", "dd", "dt"}

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.out: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False
        self.links: list[dict[str, str]] = []
        self._href: str | None = None
        self._anchor: list[str] = []
        self.meta_description = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "meta" and (a.get("name") or "").lower() == "description":
            self.meta_description = a.get("content") or ""
        if tag in self.SKIP:
            self.skip += 1
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag in ("h1", "h2", "h3"):
            self.out.append("#" * int(tag[1]) + " ")
        if tag == "li":
            self.out.append("- ")
        if tag == "a" and a.get("href"):
            self._href = urljoin(self.base, a["href"])
            self._anchor = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag == "a" and self._href:
            if self._href.startswith(("http://", "https://")):
                self.links.append({"url": self._href, "text": " ".join("".join(self._anchor).split())[:200]})
            self._href = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self.skip:
            return
        self.out.append(data)
        if self._href is not None:
            self._anchor.append(data)


def html_to_text(html: str, base_url: str) -> dict[str, Any]:
    p = _Readable(base_url)
    try:
        p.feed(html)
        p.close()
    except Exception:  # malformed markup: keep what was parsed
        pass
    text = "".join(p.out)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return {"title": " ".join(p.title.split()), "text": text[:MAX_TEXT], "truncated": len(text) > MAX_TEXT,
            "links": p.links, "description": p.meta_description}
