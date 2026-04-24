"""
Sentrial daemon — entrypoint for both local Mac dev and Railway deployment.

On Railway: launched via `python -m sentrial.core.daemon run`. PORT env var sets the port.
On Mac local: same command, or run via launchd plist.

Loaded capabilities (v1 cloud):
  - Notion MCP       (tasks, pages)
  - Creative MCP     (proposal/audit/demo — approval-gated autonomous)

Not loaded in cloud (Mac-only, kept on disk):
  - Reminders MCP    (osascript → Apple Reminders)
  - Menubar input    (rumps, macOS runloop)
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

import typer
from rich.console import Console
from rich.table import Table

from sentrial.core import secrets

log = logging.getLogger("sentrial")
console = Console()
app = typer.Typer(help="Sentrial — personal assistant", no_args_is_help=True)


@app.command()
def run(
    host: str = typer.Option(None, help="Bind host (default 0.0.0.0)"),
    port: int = typer.Option(None, help="Bind port (default $PORT or 8765)"),
    log_level: str = typer.Option("INFO"),
):
    """Run the HTTP server + agent + task runner."""
    logging.basicConfig(level=log_level, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    try:
        secrets.require("anthropic_api_key")
    except secrets.KeychainError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    asyncio.run(_main(host=host, port=port))


async def _main(host: str | None, port: int | None) -> None:
    from sentrial.core.agent import Agent
    from sentrial.core.confirmation import Tier
    from sentrial.core.task_runner import TaskRunner
    from sentrial.inputs import webhook as webhook_input
    from sentrial.mcps.base import Registry, Tool
    from sentrial.outputs import notify

    async def _notify(msg: str) -> None:
        await notify.send(msg)

    task_runner = TaskRunner(notifier=_notify, executors={})

    # Load capability modules
    registry = Registry()
    from sentrial.mcps.creative import server as creative_server
    from sentrial.mcps.evolution import server as evolution_server
    from sentrial.mcps.notion import server as notion_server

    notion_enabled = bool(secrets.get("notion_api_key"))
    if notion_enabled:
        notion_server.register(registry, task_runner)
        log.info("loaded: notion MCP")
    else:
        log.warning("skipped: notion MCP (set NOTION_API_KEY + NOTION_TASKS_DB_ID)")

    creative_server.register(registry, task_runner)
    log.info("loaded: creative MCP")

    evolution_server.register(registry, task_runner)
    log.info("loaded: evolution MCP (self-improvement loop)")

    # notify_user — Sentrial's outbound voice
    async def _notify_user_tool(args: dict) -> dict:
        msg = str(args.get("message", ""))
        title = str(args.get("title") or "Sentrial")
        await notify.send(msg, title=title)
        return {"ok": True}

    registry.add(Tool(
        name="notify_user",
        description=(
            "Send a notification to Liam (web push → Pushover → iMessage, whichever is live). "
            "Use for scope previews of autonomous jobs and completion pings."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "title": {"type": "string", "description": "Optional short title"},
            },
            "required": ["message"],
        },
        impl=_notify_user_tool,
        tier=Tier.SEND,
    ))

    # v1 auto-gate — replace with UI confirmation (phase 2)
    async def _confirm(tool_name: str, args: dict, tier) -> bool:
        log.info(f"[gate tier={tier.name}] {tool_name} → auto-approve (v1)")
        return True

    async def _strong_confirm(tool_name: str, args: dict, tier) -> bool:
        log.warning(f"[strong-gate tier={tier.name}] {tool_name} → DENIED (v1)")
        return False

    agent = Agent(
        tools=registry.tools,
        tool_impls=registry.impls,
        task_runner=task_runner,
        confirm_cb=_confirm,
        strong_confirm_cb=_strong_confirm,
    )

    # Start HTTP server
    server_task = asyncio.create_task(
        webhook_input.serve(host=host, port=port, task_runner=task_runner, agent=agent, registry=registry)
    )

    # Signal handling
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    log.info("sentrial up")
    await stop.wait()
    log.info("shutting down")
    server_task.cancel()
    try:
        await server_task
    except asyncio.CancelledError:
        pass


@app.command()
def setup():
    """Local-only: write Keychain secrets. On Railway, use env vars."""
    if sys.platform != "darwin":
        console.print("[yellow]Non-Mac host — set Railway env vars instead. See DEPLOY.md[/yellow]")
        raise typer.Exit(0)
    console.print("[bold]Sentrial local setup[/bold]\n")
    _prompt("anthropic_api_key", "Anthropic API key", required=True)
    _prompt("notion_api_key", "Notion integration token (optional)")
    _prompt("notion_tasks_db_id", "Notion tasks DB UUID (optional)")
    _prompt("liam_phone", "Your phone number for iMessage (+14155551234) (optional)")
    _prompt("pushover_token", "Pushover token (optional)")
    _prompt("pushover_user", "Pushover user key (optional)")


def _prompt(key: str, label: str, required: bool = False) -> None:
    if secrets.get(key):
        console.print(f"[dim]✓ {key} already set[/dim]")
        return
    val = typer.prompt(label, default="", show_default=False, hide_input=True)
    if not val:
        if required:
            console.print(f"[red]{key} is required[/red]")
            raise typer.Exit(1)
        return
    secrets.set(key, val)
    console.print(f"[green]✓ {key}[/green]")


@app.command("audit-tail")
def audit_tail(n: int = typer.Option(50)):
    from sentrial.core import audit
    table = Table("time", "actor", "tier", "action", "status", "result")
    for r in audit.tail(n):
        table.add_row(
            r["timestamp"][:19], r["actor"], str(r["tier"]),
            r["action"][:40], r["status"], (r["result_summary"] or "")[:60],
        )
    console.print(table)


@app.command("gen-token")
def gen_token():
    """Generate a random token suitable for SENTRIAL_TOKEN."""
    import secrets as _s
    console.print(_s.token_urlsafe(32))


@app.command("gen-vapid")
def gen_vapid():
    """
    Generate a VAPID keypair for web push.
    Outputs VAPID_PUBLIC_KEY (base64-url) and VAPID_PRIVATE_KEY (PEM) — paste both
    into Railway env vars. The PEM is multi-line; Railway's UI supports multi-line values.
    """
    import base64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    priv = ec.generate_private_key(ec.SECP256R1())
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    pub_b64 = base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode()

    console.print("[bold]VAPID keys generated[/bold]")
    console.print("[dim]Paste these into Railway env vars:[/dim]\n")
    console.print(f"VAPID_PUBLIC_KEY={pub_b64}\n")
    console.print("VAPID_PRIVATE_KEY (multi-line):")
    console.print(priv_pem)
    console.print("VAPID_CONTACT=mailto:you@example.com")


def cli():
    app()


if __name__ == "__main__":
    cli()
