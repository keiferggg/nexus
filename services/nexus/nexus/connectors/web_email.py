"""Built-in web connector (writes to any public URL) and SMTP email connector."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from .adapters import DESTRUCTIVE, EXTERNAL_WRITE, ActionSpec, AmbiguousOutcome, ConnectorError, _obj


class WebConnector:
    """Requests to ANY public URL on behalf of agents. Reads are separate tools; this connector
    carries writes (POST/PUT/PATCH, form submissions) and deletes, so every write goes through
    the action-request flow (approval or pre-approval, idempotency key, ambiguity handling).
    Uses SafeWebClient: public addresses only, no agent-supplied credentials."""

    kind = "web"

    def __init__(self, config: dict[str, Any], secret: str | None, web_client, policy, project_id: str):
        self.web = web_client
        self.policy = policy
        self.project_id = project_id

    async def discover(self) -> list[ActionSpec]:
        S, O = {"type": "string"}, {"type": "object"}
        return [
            ActionSpec("send_request", "Send a POST, PUT or PATCH request with a JSON or text body to any public URL.",
                       _obj({"method": S, "url": S, "json": O, "text": S, "headers": O}, ["method", "url"]), EXTERNAL_WRITE),
            ActionSpec("submit_form", "Submit a web form (application/x-www-form-urlencoded POST) to any public URL.",
                       _obj({"url": S, "fields": O}, ["url", "fields"]), EXTERNAL_WRITE),
            ActionSpec("delete_request", "Send an HTTP DELETE to any public URL.", _obj({"url": S, "headers": O}, ["url"]),
                       DESTRUCTIVE),
        ]

    async def execute(self, name: str, args: dict[str, Any], *, idempotency_key: str, timeout_s: float = 25) -> Any:
        from ..web.client import WebError, WebPolicyError

        raw = args.get("headers")
        hdrs = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        hdrs["idempotency-key"] = idempotency_key
        if name == "send_request":
            method = str(args.get("method", "POST")).upper()
            if method not in ("POST", "PUT", "PATCH"):
                raise ConnectorError("send_request supports POST, PUT or PATCH")
            kw: dict[str, Any] = ({"json_body": args["json"]} if "json" in args
                                  else {"content": str(args.get("text", "")).encode()})
        elif name == "submit_form":
            method, kw = "POST", {"data": {str(k): str(v) for k, v in (args.get("fields") or {}).items()}}
        elif name == "delete_request":
            method, kw = "DELETE", {}
        else:
            raise ConnectorError(f"unknown action {name}")
        try:
            r = await self.web.request(method, args["url"], policy=self.policy, project_key=self.project_id, headers=hdrs,
                                       timeout_s=timeout_s, max_redirects=3 if method == "POST" else 0, **kw)
        except WebPolicyError as exc:
            raise ConnectorError(f"refused by policy: {exc}") from None
        except WebError as exc:
            if "timed out" in str(exc):
                raise AmbiguousOutcome(f"{method} {args['url']} sent but no response") from None
            raise ConnectorError(str(exc)) from None
        if r.status >= 400:
            raise ConnectorError(f"HTTP {r.status}: {r.text()[:300]}")
        body: Any = r.text()[:20000]
        try:
            body = r.json()
        except Exception:
            pass
        return {"status": r.status, "url": r.url, "response": body}

    async def reconcile(self, name: str, args: dict[str, Any], *, idempotency_key: str) -> str:
        return "unknown"  # arbitrary sites offer no status lookup: the owner resolves manually

    async def validate(self) -> str:
        return f"internet mode: {self.policy.mode}"


class EmailConnector:
    """Send email through the owner's SMTP account.
    config = {"host", "port", "security": "starttls"|"ssl"|"none", "from", "username"?,
              "allowed_recipient_domains"?: [...]}; secret = SMTP password / app password."""

    kind = "email"

    def __init__(self, config: dict[str, Any], secret: str | None):
        for k in ("host", "from"):
            if not config.get(k):
                raise ConnectorError(f"email connector needs '{k}'")
        self.cfg = config
        self.secret = secret

    async def discover(self) -> list[ActionSpec]:
        S = {"type": "string"}
        A = {"type": "array", "items": S}
        return [ActionSpec("send_email", f"Send an email from {self.cfg['from']}.",
                           _obj({"to": A, "subject": S, "body": S, "cc": A}, ["to", "subject", "body"]), EXTERNAL_WRITE)]

    def _check(self, addrs: list[str]) -> list[str]:
        out = []
        allowed = [d.lower() for d in self.cfg.get("allowed_recipient_domains") or []]
        for a in addrs:
            a = str(a).strip()
            if not re.fullmatch(r"[^@\s<>,]+@[^@\s<>,]+\.[^@\s<>,]+", a):
                raise ConnectorError(f"invalid address {a!r}")
            if allowed and a.split("@")[1].lower() not in allowed:
                raise ConnectorError(f"recipient domain not allowed by owner: {a}")
            out.append(a)
        if len(out) > 50:
            raise ConnectorError("at most 50 recipients")
        return out

    def _send(self, msg, rcpts: list[str], timeout_s: float) -> None:
        import smtplib
        import ssl

        sec = self.cfg.get("security", "starttls")
        port = int(self.cfg.get("port") or (465 if sec == "ssl" else 587))
        if sec == "ssl":
            s = smtplib.SMTP_SSL(self.cfg["host"], port, timeout=timeout_s, context=ssl.create_default_context())
        else:
            s = smtplib.SMTP(self.cfg["host"], port, timeout=timeout_s)
        with s:
            if sec == "starttls":
                s.starttls(context=ssl.create_default_context())
            if self.cfg.get("username") and self.secret:
                s.login(self.cfg["username"], self.secret)
            s.send_message(msg, from_addr=self.cfg["from"], to_addrs=rcpts)

    async def execute(self, name: str, args: dict[str, Any], *, idempotency_key: str, timeout_s: float = 30) -> Any:
        import smtplib
        from email.message import EmailMessage
        from email.utils import make_msgid

        if name != "send_email":
            raise ConnectorError(f"unknown action {name}")
        to = self._check(list(args["to"]))
        cc = self._check(list(args.get("cc") or []))
        if not to:
            raise ConnectorError("at least one recipient required")
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.cfg["from"], ", ".join(to), str(args["subject"])[:300]
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Message-ID"] = make_msgid(idstring=idempotency_key[:16], domain="nexus.local")
        msg["X-NEXUS-Operation"] = idempotency_key
        msg.set_content(str(args["body"])[:100_000])
        try:
            await asyncio.to_thread(self._send, msg, to + cc, timeout_s)
        except (ConnectionRefusedError, smtplib.SMTPConnectError) as exc:
            raise ConnectorError(f"could not connect to SMTP server: {exc}") from None
        except smtplib.SMTPResponseException as exc:
            raise ConnectorError(f"SMTP refused: {exc.smtp_code} {exc.smtp_error!r}") from None
        except smtplib.SMTPRecipientsRefused as exc:
            raise ConnectorError(f"recipients refused: {list(exc.recipients)}") from None
        except (smtplib.SMTPServerDisconnected, TimeoutError, OSError) as exc:
            raise AmbiguousOutcome(f"SMTP session ended unexpectedly ({type(exc).__name__}); the email may have been sent") from None
        return {"sent_to": to + cc, "message_id": msg["Message-ID"]}

    async def reconcile(self, name: str, args: dict[str, Any], *, idempotency_key: str) -> str:
        return "unknown"  # check the Sent folder for the X-NEXUS-Operation header

    async def validate(self) -> str:
        return f"SMTP {self.cfg['host']} as {self.cfg['from']} (connection tested on first send)"
