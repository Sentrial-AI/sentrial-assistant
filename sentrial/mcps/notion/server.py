"""
Notion MCP. Replaces Apple Reminders for cloud deployment.

Tools:
  list_tasks       — list open tasks from the configured Tasks database
  create_task      — create a new task
  complete_task    — mark a task done
  search_notion    — search pages by title
  read_page        — get full text of a Notion page

Requires:
  NOTION_API_KEY        — integration token (share your DBs with the integration)
  NOTION_TASKS_DB_ID    — UUID of the Notion database used for tasks
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import requests

from sentrial.core import secrets as kc
from sentrial.core.confirmation import Tier
from sentrial.core.task_runner import TaskRunner
from sentrial.mcps.base import Registry, Tool

log = logging.getLogger(__name__)

NOTION_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {kc.require('notion_api_key')}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _tasks_db() -> str:
    db_id = kc.get("notion_tasks_db_id")
    if not db_id:
        raise RuntimeError(
            "Missing NOTION_TASKS_DB_ID. Create a Notion database with a 'Name' (title), "
            "'Status' (status/select), and optional 'Due' (date) property, share it with "
            "your Notion integration, then set NOTION_TASKS_DB_ID to its UUID."
        )
    return db_id


async def _request(method: str, path: str, json: dict | None = None) -> dict:
    loop = asyncio.get_event_loop()

    def _do() -> dict:
        r = requests.request(
            method, f"{NOTION_BASE}{path}",
            headers=_headers(), json=json, timeout=20,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"notion {method} {path} → {r.status_code}: {r.text[:300]}")
        return r.json()

    return await loop.run_in_executor(None, _do)


# -------------------- Tools --------------------

async def list_tasks(args: dict) -> Any:
    status_filter = args.get("status", "open")  # 'open' | 'all' | any status name
    db_id = _tasks_db()
    body: dict = {"page_size": 50, "sorts": [{"timestamp": "created_time", "direction": "descending"}]}
    if status_filter == "open":
        body["filter"] = {
            "and": [
                {"property": "Status", "status": {"does_not_equal": "Done"}},
            ]
        }
    elif status_filter not in ("all", None):
        body["filter"] = {"property": "Status", "status": {"equals": status_filter}}
    try:
        data = await _request("POST", f"/databases/{db_id}/query", json=body)
    except RuntimeError as e:
        return {"error": str(e)}

    tasks = []
    for p in data.get("results", []):
        props = p.get("properties", {})
        title = ""
        for v in props.values():
            if v.get("type") == "title":
                title = "".join(t.get("plain_text", "") for t in v.get("title", []))
                break
        status = ""
        for v in props.values():
            if v.get("type") == "status":
                status = (v.get("status") or {}).get("name", "")
                break
        due = None
        for v in props.values():
            if v.get("type") == "date":
                due = (v.get("date") or {}).get("start")
                break
        tasks.append({"id": p["id"], "title": title, "status": status, "due": due, "url": p.get("url")})
    return {"tasks": tasks, "count": len(tasks)}


async def create_task(args: dict) -> Any:
    title = args.get("title")
    if not title:
        return {"error": "title is required"}
    due = args.get("due")  # ISO date or datetime string
    status = args.get("status", "To do")
    db_id = _tasks_db()

    properties: dict = {
        "Name": {"title": [{"text": {"content": title}}]},
        "Status": {"status": {"name": status}},
    }
    if due:
        properties["Due"] = {"date": {"start": due}}

    try:
        data = await _request(
            "POST", "/pages",
            json={"parent": {"database_id": db_id}, "properties": properties},
        )
    except RuntimeError as e:
        return {"error": str(e)}
    return {"id": data.get("id"), "url": data.get("url"), "title": title, "due": due, "status": status}


async def complete_task(args: dict) -> Any:
    task_id = args.get("task_id")
    if not task_id:
        return {"error": "task_id is required"}
    try:
        data = await _request(
            "PATCH", f"/pages/{task_id}",
            json={"properties": {"Status": {"status": {"name": "Done"}}}},
        )
    except RuntimeError as e:
        return {"error": str(e)}
    return {"ok": True, "id": data.get("id")}


async def search_notion(args: dict) -> Any:
    q = args.get("query", "").strip()
    limit = int(args.get("limit", 10))
    try:
        data = await _request("POST", "/search", json={"query": q, "page_size": limit})
    except RuntimeError as e:
        return {"error": str(e)}
    out = []
    for r in data.get("results", []):
        title = ""
        props = r.get("properties") or {}
        for v in props.values():
            if isinstance(v, dict) and v.get("type") == "title":
                title = "".join(t.get("plain_text", "") for t in v.get("title", []))
                break
        if not title and r.get("object") == "page":
            # child page style
            title = "".join(t.get("plain_text", "") for t in r.get("title", []))
        out.append({"id": r.get("id"), "object": r.get("object"), "title": title, "url": r.get("url")})
    return {"results": out, "count": len(out)}


async def read_page(args: dict) -> Any:
    page_id = args.get("page_id")
    if not page_id:
        return {"error": "page_id is required"}
    try:
        blocks = await _request("GET", f"/blocks/{page_id}/children?page_size=100")
    except RuntimeError as e:
        return {"error": str(e)}
    lines = []
    for b in blocks.get("results", []):
        t = b.get("type")
        rich = (b.get(t) or {}).get("rich_text", [])
        text = "".join(r.get("plain_text", "") for r in rich)
        if t == "heading_1":
            lines.append(f"# {text}")
        elif t == "heading_2":
            lines.append(f"## {text}")
        elif t == "heading_3":
            lines.append(f"### {text}")
        elif t == "bulleted_list_item":
            lines.append(f"- {text}")
        elif t == "numbered_list_item":
            lines.append(f"1. {text}")
        elif t == "to_do":
            done = (b.get("to_do") or {}).get("checked", False)
            lines.append(f"[{'x' if done else ' '}] {text}")
        elif text:
            lines.append(text)
    return {"id": page_id, "content": "\n".join(lines)}


TOOLS = [
    Tool(
        name="list_tasks",
        description=(
            "List tasks from Liam's Notion tasks database. By default returns open "
            "(non-Done) tasks; pass status='all' for everything or status='Done' etc."
        ),
        input_schema={
            "type": "object",
            "properties": {"status": {"type": "string", "description": "'open' (default), 'all', or a specific status name"}},
        },
        impl=list_tasks,
        tier=Tier.READ,
    ),
    Tool(
        name="create_task",
        description="Create a new task in Liam's Notion tasks database.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "due": {"type": "string", "description": "ISO date (YYYY-MM-DD) or datetime"},
                "status": {"type": "string", "description": "Default 'To do'"},
            },
            "required": ["title"],
        },
        impl=create_task,
        tier=Tier.SEND,
    ),
    Tool(
        name="complete_task",
        description="Mark a Notion task as Done. task_id from list_tasks.",
        input_schema={
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
        impl=complete_task,
        tier=Tier.SEND,
    ),
    Tool(
        name="search_notion",
        description="Search Notion workspace by text. Returns pages and databases shared with the integration.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
        impl=search_notion,
        tier=Tier.READ,
    ),
    Tool(
        name="read_page",
        description="Read the text content of a Notion page by id.",
        input_schema={
            "type": "object",
            "properties": {"page_id": {"type": "string"}},
            "required": ["page_id"],
        },
        impl=read_page,
        tier=Tier.READ,
    ),
]


def register(registry: Registry, task_runner: TaskRunner) -> None:
    for t in TOOLS:
        registry.add(t)
