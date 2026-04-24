"""
Notion MCP — full read/write surface, not just tasks.

Tools:
  list_tasks            — list open tasks from the configured Tasks database
  create_task           — create a new task in the tasks DB
  complete_task         — mark a task done
  search_notion         — search pages by title
  read_page             — get full text of a Notion page

  list_databases        — list every database the integration can see
  query_database        — query an arbitrary database by id (with optional filter)
  add_database_row      — write a row to an arbitrary database
  create_database       — create a new database under a parent page, with schema
  append_to_page        — append blocks (text / heading / todo) to an existing page

Requires:
  NOTION_API_KEY        — integration token (share your DBs with the integration)
  NOTION_TASKS_DB_ID    — UUID of the Notion database used for tasks (tasks-specific tools)
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


# -------------------- General database + page tools --------------------

# Map of short property-type names the agent can use → Notion's property config.
# Keeps the create_database API much simpler for the agent.
_PROP_SCHEMAS: dict[str, dict] = {
    "title":       {"title": {}},
    "text":        {"rich_text": {}},
    "number":      {"number": {"format": "number"}},
    "currency":    {"number": {"format": "dollar"}},
    "date":        {"date": {}},
    "checkbox":    {"checkbox": {}},
    "url":         {"url": {}},
    "email":       {"email": {}},
    "phone":       {"phone_number": {}},
    "people":      {"people": {}},
    "files":       {"files": {}},
    "created_time":    {"created_time": {}},
    "last_edited_time":{"last_edited_time": {}},
}


def _select_schema(options: list[str]) -> dict:
    return {"select": {"options": [{"name": o} for o in options]}}


def _multi_select_schema(options: list[str]) -> dict:
    return {"multi_select": {"options": [{"name": o} for o in options]}}


def _status_schema(options: list[str]) -> dict:
    # Notion's status API is read-only at create-time; we emit select instead
    # and rename to Status. Most workflows work fine with select semantics.
    return {"select": {"options": [{"name": o} for o in options]}}


async def list_databases(args: dict) -> Any:
    """List every database the integration has access to."""
    limit = int(args.get("limit", 50))
    try:
        data = await _request(
            "POST", "/search",
            json={"filter": {"value": "database", "property": "object"},
                  "page_size": limit},
        )
    except RuntimeError as e:
        return {"error": str(e)}
    out = []
    for r in data.get("results", []):
        title = "".join(t.get("plain_text", "") for t in r.get("title") or [])
        props = list((r.get("properties") or {}).keys())
        out.append({
            "id": r.get("id"),
            "title": title,
            "url": r.get("url"),
            "property_names": props,
        })
    return {"databases": out, "count": len(out)}


async def query_database(args: dict) -> Any:
    """
    Query any database by id. args:
      database_id: str (required)
      filter: dict (optional, raw Notion filter object)
      sorts: list (optional, raw Notion sort)
      page_size: int (default 50, max 100)
    """
    db_id = args.get("database_id")
    if not db_id:
        return {"error": "database_id is required"}
    body: dict = {"page_size": min(100, int(args.get("page_size", 50)))}
    if args.get("filter"):
        body["filter"] = args["filter"]
    if args.get("sorts"):
        body["sorts"] = args["sorts"]
    try:
        data = await _request("POST", f"/databases/{db_id}/query", json=body)
    except RuntimeError as e:
        return {"error": str(e)}
    rows = []
    for p in data.get("results", []):
        row = {"id": p["id"], "url": p.get("url"), "properties": {}}
        for name, v in (p.get("properties") or {}).items():
            row["properties"][name] = _flatten_prop(v)
        rows.append(row)
    return {"rows": rows, "count": len(rows), "has_more": data.get("has_more", False)}


def _flatten_prop(p: dict) -> Any:
    """Reduce a Notion property value to something the agent can read easily."""
    t = p.get("type")
    v = p.get(t)
    if v is None:
        return None
    if t in ("title", "rich_text"):
        return "".join(r.get("plain_text", "") for r in v)
    if t in ("number", "checkbox", "url", "email", "phone_number"):
        return v
    if t == "select":
        return (v or {}).get("name")
    if t == "multi_select":
        return [o.get("name") for o in v]
    if t == "status":
        return (v or {}).get("name")
    if t == "date":
        return {"start": (v or {}).get("start"), "end": (v or {}).get("end")}
    if t == "people":
        return [x.get("name") or x.get("id") for x in v]
    if t in ("created_time", "last_edited_time"):
        return v
    return v


async def add_database_row(args: dict) -> Any:
    """
    Create a row in an arbitrary database. args:
      database_id: str (required)
      properties: dict (required) — short form, e.g.
          {"Name": "hello", "Status": "To do", "Due": "2026-05-01",
           "Tags": ["work", "client"], "Done": true, "Notes": "..."}
        We coerce to Notion's nested property shape automatically based on
        the existing database schema.
    """
    db_id = args.get("database_id")
    if not db_id:
        return {"error": "database_id is required"}
    props_in = args.get("properties") or {}
    if not isinstance(props_in, dict) or not props_in:
        return {"error": "properties dict is required"}

    # Fetch schema so we know the real Notion type for each property.
    try:
        schema = await _request("GET", f"/databases/{db_id}")
    except RuntimeError as e:
        return {"error": str(e)}
    schema_props = schema.get("properties", {})

    notion_props: dict = {}
    for key, value in props_in.items():
        if key not in schema_props:
            return {"error": f"unknown property '{key}' on database"}
        t = schema_props[key].get("type")
        coerced = _coerce_property_value(t, value)
        if coerced is None:
            continue
        notion_props[key] = coerced

    try:
        page = await _request(
            "POST", "/pages",
            json={"parent": {"database_id": db_id}, "properties": notion_props},
        )
    except RuntimeError as e:
        return {"error": str(e)}
    return {"id": page.get("id"), "url": page.get("url")}


def _coerce_property_value(ntype: str, value: Any) -> dict | None:
    """Lift a simple Python value to a Notion property object."""
    if value is None:
        return None
    if ntype == "title":
        return {"title": [{"text": {"content": str(value)}}]}
    if ntype == "rich_text":
        return {"rich_text": [{"text": {"content": str(value)}}]}
    if ntype == "number":
        return {"number": float(value)}
    if ntype == "checkbox":
        return {"checkbox": bool(value)}
    if ntype == "url":
        return {"url": str(value)}
    if ntype == "email":
        return {"email": str(value)}
    if ntype == "phone_number":
        return {"phone_number": str(value)}
    if ntype == "select":
        return {"select": {"name": str(value)}}
    if ntype == "multi_select":
        names = value if isinstance(value, list) else [value]
        return {"multi_select": [{"name": str(n)} for n in names]}
    if ntype == "status":
        return {"status": {"name": str(value)}}
    if ntype == "date":
        # accept "YYYY-MM-DD", ISO datetime, or {"start": .., "end": ..}
        if isinstance(value, dict):
            return {"date": value}
        return {"date": {"start": str(value)}}
    if ntype == "people":
        ids = value if isinstance(value, list) else [value]
        return {"people": [{"id": str(x)} for x in ids]}
    # Unknown/unsupported type — skip silently.
    return None


async def create_database(args: dict) -> Any:
    """
    Create a new Notion database as a child of a parent page. args:
      parent_page_id: str (required) — the page under which to put the DB
      title: str (required)
      properties: dict (required), shape:
          {"Name": "title",
           "Status": {"kind": "select", "options": ["Todo","Doing","Done"]},
           "Due": "date",
           "Count": "number",
           "URL": "url",
           "Tags": {"kind": "multi_select", "options": ["work","personal"]}}
        The first title-typed property must be named; all DBs need exactly one
        title. Short strings use _PROP_SCHEMAS, dict form {kind, options} is
        required for select/multi_select.
    """
    parent_page_id = args.get("parent_page_id")
    title = args.get("title")
    props_in = args.get("properties") or {}
    if not parent_page_id or not title or not props_in:
        return {"error": "parent_page_id, title, and properties are required"}

    notion_schema: dict = {}
    has_title = False
    for name, spec in props_in.items():
        if isinstance(spec, str):
            if spec == "title":
                notion_schema[name] = _PROP_SCHEMAS["title"]
                has_title = True
            elif spec in _PROP_SCHEMAS:
                notion_schema[name] = _PROP_SCHEMAS[spec]
            else:
                return {"error": f"unsupported property type '{spec}' for {name}"}
        elif isinstance(spec, dict):
            kind = spec.get("kind")
            options = spec.get("options") or []
            if kind == "select":
                notion_schema[name] = _select_schema(options)
            elif kind == "multi_select":
                notion_schema[name] = _multi_select_schema(options)
            elif kind == "status":
                notion_schema[name] = _status_schema(options)
            else:
                return {"error": f"unsupported property kind '{kind}' for {name}"}
        else:
            return {"error": f"bad spec for {name}"}

    if not has_title:
        # Auto-insert "Name" as title if the caller forgot — most DBs need one.
        notion_schema = {"Name": _PROP_SCHEMAS["title"], **notion_schema}

    body = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": title}}],
        "properties": notion_schema,
    }
    try:
        data = await _request("POST", "/databases", json=body)
    except RuntimeError as e:
        return {"error": str(e)}
    return {
        "id": data.get("id"),
        "url": data.get("url"),
        "title": title,
        "property_names": list(notion_schema.keys()),
    }


async def append_to_page(args: dict) -> Any:
    """
    Append blocks to an existing Notion page. args:
      page_id: str (required)
      blocks: list of dicts (required), each like:
          {"kind": "paragraph"|"heading_1"|"heading_2"|"heading_3"
                    |"bulleted_list_item"|"numbered_list_item"|"to_do"|"quote",
           "text": "...",
           "checked": false   (only for to_do)}
    """
    page_id = args.get("page_id")
    blocks_in = args.get("blocks") or []
    if not page_id or not isinstance(blocks_in, list) or not blocks_in:
        return {"error": "page_id and non-empty blocks list required"}

    children = []
    for b in blocks_in:
        if not isinstance(b, dict):
            continue
        kind = b.get("kind") or "paragraph"
        text = str(b.get("text") or "")
        rich = [{"type": "text", "text": {"content": text}}]
        block: dict[str, Any] = {"object": "block", "type": kind}
        payload = {"rich_text": rich}
        if kind == "to_do":
            payload["checked"] = bool(b.get("checked", False))
        block[kind] = payload
        children.append(block)

    if not children:
        return {"error": "no valid blocks"}
    try:
        data = await _request(
            "PATCH", f"/blocks/{page_id}/children", json={"children": children},
        )
    except RuntimeError as e:
        return {"error": str(e)}
    return {"ok": True, "appended": len(children), "results": len(data.get("results", []))}


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
    Tool(
        name="list_databases",
        description=(
            "List every Notion database the integration can access. Returns id, title, "
            "url, and existing property names per database."
        ),
        input_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 50}},
        },
        impl=list_databases,
        tier=Tier.READ,
    ),
    Tool(
        name="query_database",
        description=(
            "Query any Notion database by id. Optional `filter` + `sorts` use Notion's raw "
            "filter syntax. Returns a flattened rows list with each property already "
            "coerced to a readable scalar/list."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "database_id": {"type": "string"},
                "filter": {"type": "object"},
                "sorts": {"type": "array"},
                "page_size": {"type": "integer", "default": 50},
            },
            "required": ["database_id"],
        },
        impl=query_database,
        tier=Tier.READ,
    ),
    Tool(
        name="add_database_row",
        description=(
            "Add a row to an arbitrary Notion database. `properties` is a plain dict keyed "
            "by the exact property name in the database; we coerce each value to the right "
            "Notion property shape based on the database schema. Example: "
            '{"database_id": "…", "properties": {"Name": "Intro call", "Status": "Todo", '
            '"Due": "2026-05-03", "Tags": ["client", "bvl"]}}'
        ),
        input_schema={
            "type": "object",
            "properties": {
                "database_id": {"type": "string"},
                "properties": {"type": "object"},
            },
            "required": ["database_id", "properties"],
        },
        impl=add_database_row,
        tier=Tier.SEND,
    ),
    Tool(
        name="create_database",
        description=(
            "Create a new Notion database as a child of an existing page. Short property "
            "schema: {name: type}. Simple types are strings: "
            "'title','text','number','currency','date','checkbox','url','email','phone','files'. "
            "For options use a dict: {'kind': 'select'|'multi_select'|'status', 'options': [...]}. "
            "A title-typed property is required; if none given, 'Name' is added automatically."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "parent_page_id": {"type": "string"},
                "title": {"type": "string"},
                "properties": {"type": "object"},
            },
            "required": ["parent_page_id", "title", "properties"],
        },
        impl=create_database,
        tier=Tier.SEND,
    ),
    Tool(
        name="append_to_page",
        description=(
            "Append blocks to an existing Notion page. `blocks` is a list of "
            "{kind, text, checked?} where kind is one of paragraph / heading_1 / "
            "heading_2 / heading_3 / bulleted_list_item / numbered_list_item / to_do / quote."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "page_id": {"type": "string"},
                "blocks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string"},
                            "text": {"type": "string"},
                            "checked": {"type": "boolean"},
                        },
                        "required": ["text"],
                    },
                },
            },
            "required": ["page_id", "blocks"],
        },
        impl=append_to_page,
        tier=Tier.SEND,
    ),
]


def register(registry: Registry, task_runner: TaskRunner) -> None:
    registry.add_group("notion")
    for t in TOOLS:
        registry.add(t)
