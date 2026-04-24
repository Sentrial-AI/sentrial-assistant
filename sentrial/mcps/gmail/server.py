"""
Gmail MCP — read/write against the connected Google account.

Tools:
  list_emails       — recent messages (from/subject/snippet/date/labels)
  search_emails     — Gmail query syntax (from:x before:2026/04/25 is:unread …)
  read_email        — full body of one message by id
  draft_email       — create a Gmail draft (tier DRAFT — not sent)
  send_draft        — send a pre-existing draft by id (tier SEND — gated)
  send_email        — compose + send in one step (tier SEND — gated)
  archive_email     — remove INBOX label (tier SEND)
  mark_read         — remove UNREAD label (tier SEND)
  mark_unread       — add UNREAD label (tier SEND)

Auth comes from `sentrial.core.google_oauth` — the MCP itself doesn't know
about client secrets. Every call calls `ensure_access_token()` which
refreshes the token if it's near expiry. Failures surface with a clear
"reconnect" message so the agent can tell the user.
"""
from __future__ import annotations

import base64
import logging
from email.message import EmailMessage
from typing import Any

import httpx

from sentrial.core import google_oauth
from sentrial.core.confirmation import Tier
from sentrial.core.task_runner import TaskRunner
from sentrial.mcps.base import Registry, Tool

log = logging.getLogger(__name__)

API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


async def _request(method: str, path: str, json: dict | None = None,
                    params: dict | None = None) -> dict:
    tok = await google_oauth.ensure_access_token()
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.request(
            method, f"{API_BASE}{path}",
            headers={"Authorization": f"Bearer {tok}"},
            json=json, params=params,
        )
    if r.status_code >= 400:
        raise RuntimeError(f"gmail {method} {path} → {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


def _b64url(s: str | bytes) -> str:
    if isinstance(s, str):
        s = s.encode()
    return base64.urlsafe_b64encode(s).rstrip(b"=").decode()


def _decode_b64url(s: str) -> bytes:
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + ("=" * pad))


def _header(headers: list[dict], name: str) -> str:
    name_lower = name.lower()
    for h in headers or []:
        if (h.get("name") or "").lower() == name_lower:
            return h.get("value") or ""
    return ""


def _walk_for_text(part: dict) -> str:
    """Return the best-effort plain-text content of a Gmail payload."""
    mime = part.get("mimeType") or ""
    body = part.get("body") or {}
    data = body.get("data")
    if mime == "text/plain" and data:
        try:
            return _decode_b64url(data).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""
    # Prefer plain over html when both exist.
    for child in part.get("parts") or []:
        if (child.get("mimeType") or "") == "text/plain" and (child.get("body") or {}).get("data"):
            return _walk_for_text(child)
    for child in part.get("parts") or []:
        t = _walk_for_text(child)
        if t:
            return t
    # Fallback: strip HTML.
    if mime == "text/html" and data:
        try:
            html = _decode_b64url(data).decode("utf-8", errors="replace")
            import re
            return re.sub(r"<[^>]+>", "", html)
        except Exception:  # noqa: BLE001
            return ""
    return ""


# -------------------- tools --------------------

async def list_emails(args: dict) -> Any:
    """Recent messages. args: limit (default 15), label ('INBOX'|'UNREAD'|…)."""
    limit = int(args.get("limit", 15))
    label = args.get("label")  # e.g. 'INBOX', 'UNREAD', 'STARRED'
    params: dict = {"maxResults": min(50, limit)}
    if label:
        params["labelIds"] = label
    try:
        listing = await _request("GET", "/messages", params=params)
    except RuntimeError as e:
        return {"error": str(e)}
    ids = [m["id"] for m in listing.get("messages") or []]
    out = []
    # Fetch metadata only (much cheaper than full).
    for mid in ids[:limit]:
        try:
            msg = await _request(
                "GET", f"/messages/{mid}",
                params={"format": "metadata",
                        "metadataHeaders": "From,To,Subject,Date"},
            )
            headers = (msg.get("payload") or {}).get("headers") or []
            out.append({
                "id": mid,
                "thread_id": msg.get("threadId"),
                "from": _header(headers, "From"),
                "to": _header(headers, "To"),
                "subject": _header(headers, "Subject"),
                "date": _header(headers, "Date"),
                "snippet": msg.get("snippet"),
                "labels": msg.get("labelIds") or [],
                "unread": "UNREAD" in (msg.get("labelIds") or []),
            })
        except RuntimeError:
            continue
    return {"emails": out, "count": len(out)}


async def search_emails(args: dict) -> Any:
    """Gmail search by query string: `q`. Example: 'from:foo@bar.com newer_than:7d'."""
    q = args.get("query") or args.get("q")
    if not q:
        return {"error": "query required"}
    limit = int(args.get("limit", 15))
    try:
        listing = await _request("GET", "/messages",
                                  params={"q": q, "maxResults": min(50, limit)})
    except RuntimeError as e:
        return {"error": str(e)}
    ids = [m["id"] for m in listing.get("messages") or []]
    out = []
    for mid in ids[:limit]:
        try:
            msg = await _request(
                "GET", f"/messages/{mid}",
                params={"format": "metadata",
                        "metadataHeaders": "From,To,Subject,Date"},
            )
            headers = (msg.get("payload") or {}).get("headers") or []
            out.append({
                "id": mid,
                "from": _header(headers, "From"),
                "subject": _header(headers, "Subject"),
                "date": _header(headers, "Date"),
                "snippet": msg.get("snippet"),
            })
        except RuntimeError:
            continue
    return {"results": out, "count": len(out), "query": q}


async def read_email(args: dict) -> Any:
    """Full body of one message. args: message_id."""
    mid = args.get("message_id")
    if not mid:
        return {"error": "message_id required"}
    try:
        msg = await _request("GET", f"/messages/{mid}", params={"format": "full"})
    except RuntimeError as e:
        return {"error": str(e)}
    payload = msg.get("payload") or {}
    headers = payload.get("headers") or []
    body = _walk_for_text(payload)
    return {
        "id": mid,
        "thread_id": msg.get("threadId"),
        "from": _header(headers, "From"),
        "to": _header(headers, "To"),
        "cc": _header(headers, "Cc"),
        "subject": _header(headers, "Subject"),
        "date": _header(headers, "Date"),
        "snippet": msg.get("snippet"),
        "body": (body or "")[:8000],   # cap to keep agent context sane
        "labels": msg.get("labelIds") or [],
    }


def _compose_raw(to: str, subject: str, body: str, cc: str = "",
                  bcc: str = "", reply_to_message_id: str | None = None,
                  thread_id: str | None = None) -> dict:
    msg = EmailMessage()
    msg["To"] = to
    if cc: msg["Cc"] = cc
    if bcc: msg["Bcc"] = bcc
    msg["Subject"] = subject
    msg.set_content(body)
    raw = _b64url(msg.as_bytes())
    out: dict = {"raw": raw}
    if thread_id:
        out["threadId"] = thread_id
    return out


async def draft_email(args: dict) -> Any:
    """Create a Gmail draft (not sent). args: to, subject, body, cc?, bcc?, thread_id?"""
    to = args.get("to"); subject = args.get("subject"); body = args.get("body", "")
    if not to or not subject:
        return {"error": "to + subject required"}
    msg = _compose_raw(
        to=to, subject=subject, body=body,
        cc=args.get("cc", ""), bcc=args.get("bcc", ""),
        thread_id=args.get("thread_id"),
    )
    try:
        data = await _request("POST", "/drafts", json={"message": msg})
    except RuntimeError as e:
        return {"error": str(e)}
    return {
        "id": data.get("id"),
        "message_id": (data.get("message") or {}).get("id"),
        "to": to, "subject": subject,
    }


async def send_draft(args: dict) -> Any:
    """Send an existing draft. args: draft_id."""
    did = args.get("draft_id")
    if not did:
        return {"error": "draft_id required"}
    try:
        data = await _request("POST", "/drafts/send", json={"id": did})
    except RuntimeError as e:
        return {"error": str(e)}
    return {"ok": True, "message_id": (data or {}).get("id")}


async def send_email(args: dict) -> Any:
    """Compose + send in one step. Always tier SEND (gated)."""
    to = args.get("to"); subject = args.get("subject"); body = args.get("body", "")
    if not to or not subject:
        return {"error": "to + subject required"}
    raw = _compose_raw(
        to=to, subject=subject, body=body,
        cc=args.get("cc", ""), bcc=args.get("bcc", ""),
        thread_id=args.get("thread_id"),
    )
    try:
        data = await _request("POST", "/messages/send", json=raw)
    except RuntimeError as e:
        return {"error": str(e)}
    return {"ok": True, "message_id": data.get("id"), "thread_id": data.get("threadId")}


async def _modify_labels(message_id: str, add: list[str], remove: list[str]) -> dict:
    body: dict = {}
    if add: body["addLabelIds"] = add
    if remove: body["removeLabelIds"] = remove
    return await _request("POST", f"/messages/{message_id}/modify", json=body)


async def archive_email(args: dict) -> Any:
    mid = args.get("message_id")
    if not mid: return {"error": "message_id required"}
    try:
        await _modify_labels(mid, add=[], remove=["INBOX"])
    except RuntimeError as e:
        return {"error": str(e)}
    return {"ok": True, "id": mid}


async def mark_read(args: dict) -> Any:
    mid = args.get("message_id")
    if not mid: return {"error": "message_id required"}
    try:
        await _modify_labels(mid, add=[], remove=["UNREAD"])
    except RuntimeError as e:
        return {"error": str(e)}
    return {"ok": True, "id": mid}


async def mark_unread(args: dict) -> Any:
    mid = args.get("message_id")
    if not mid: return {"error": "message_id required"}
    try:
        await _modify_labels(mid, add=["UNREAD"], remove=[])
    except RuntimeError as e:
        return {"error": str(e)}
    return {"ok": True, "id": mid}


TOOLS = [
    Tool(
        name="list_emails",
        description=(
            "List recent Gmail messages. Returns from / subject / date / snippet / "
            "labels / unread-flag. Default limit 15. Optional `label` filters to "
            "INBOX, UNREAD, STARRED, etc."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 15},
                "label": {"type": "string"},
            },
        },
        impl=list_emails, tier=Tier.READ,
    ),
    Tool(
        name="search_emails",
        description=(
            "Search Gmail using the same query syntax as the Gmail search bar. "
            "Examples: 'from:alex@acme.com newer_than:7d', 'is:unread has:attachment', "
            "'subject:\"follow up\" before:2026/04/25'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 15},
            },
            "required": ["query"],
        },
        impl=search_emails, tier=Tier.READ,
    ),
    Tool(
        name="read_email",
        description="Read the full body of one Gmail message by id.",
        input_schema={
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
        impl=read_email, tier=Tier.READ,
    ),
    Tool(
        name="draft_email",
        description=(
            "Create a Gmail draft (does NOT send). Good for preview-before-send "
            "workflows. Returns the draft id — use send_draft to actually send."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "cc": {"type": "string"},
                "bcc": {"type": "string"},
                "thread_id": {"type": "string"},
            },
            "required": ["to", "subject"],
        },
        impl=draft_email, tier=Tier.DRAFT,
    ),
    Tool(
        name="send_draft",
        description="Send a pre-existing Gmail draft by id.",
        input_schema={
            "type": "object",
            "properties": {"draft_id": {"type": "string"}},
            "required": ["draft_id"],
        },
        impl=send_draft, tier=Tier.SEND,
    ),
    Tool(
        name="send_email",
        description="Compose + send a Gmail message in one step. Use sparingly — draft_email + manual review is usually safer.",
        input_schema={
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "cc": {"type": "string"},
                "bcc": {"type": "string"},
                "thread_id": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
        impl=send_email, tier=Tier.SEND,
    ),
    Tool(
        name="archive_email",
        description="Remove INBOX label from a message (archive).",
        input_schema={
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
        impl=archive_email, tier=Tier.SEND,
    ),
    Tool(
        name="mark_read",
        description="Remove UNREAD label.",
        input_schema={
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
        impl=mark_read, tier=Tier.SEND,
    ),
    Tool(
        name="mark_unread",
        description="Add UNREAD label.",
        input_schema={
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
        impl=mark_unread, tier=Tier.SEND,
    ),
]


def register(registry: Registry, task_runner: TaskRunner) -> None:
    # Gmail is always registered; tool impls check google_oauth.is_connected()
    # at call time and return a clear error if not yet authorized.
    from sentrial.core import google_oauth
    status = "active" if google_oauth.is_connected() else "pending_auth"
    registry.add_group("gmail", status=status)
    for t in TOOLS:
        registry.add(t)
