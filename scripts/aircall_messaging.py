"""Safe Aircall messaging admin CLI.

Usage:
  uv run python scripts/aircall_messaging.py status --line-id 1244782
  uv run python scripts/aircall_messaging.py restore --line-id 1244782
  uv run python scripts/aircall_messaging.py configure --line-id 1244782 --callback-url https://prod.example.com/webhooks/aircall/callback

Restoring (DELETE configuration) returns the number to normal Aircall-app
SMS so it receives messages even when this app is stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from texponse.config import get_settings
from texponse.integrations.aircall import AircallClient


async def cmd_status(line_id: int) -> int:
    client = AircallClient(get_settings())
    try:
        cfg = await client.get_number_messaging_config(line_id)
        print(f"Messaging config for {line_id}: {cfg}")
        print("-> Number is in API/proxy mode. Aircall-app SMS is disabled.")
        return 0
    except Exception as exc:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
        if status == 404:
            print(f"No messaging config for {line_id} -> normal Aircall-app SMS active. OK.")
            return 0
        print(f"status check failed: {exc}")
        return 1
    finally:
        await client.close()


async def cmd_restore(line_id: int) -> int:
    client = AircallClient(get_settings())
    try:
        try:
            await client.delete_number_messaging_config(line_id)
            print(f"Restored {line_id} to normal Aircall-app SMS.")
        except Exception as exc:
            resp = getattr(exc, "response", None)
            if getattr(resp, "status_code", None) == 404:
                print(f"{line_id} already in normal mode (no config). Nothing to do.")
            else:
                raise
        return 0
    except Exception as exc:
        print(f"restore failed: {exc}")
        return 1
    finally:
        await client.close()


async def cmd_configure(line_id: int, callback_url: str, allow_dev: bool) -> int:
    settings = get_settings()
    if settings.app_env.lower() != "production" and not allow_dev:
        print("REFUSED: configuring messaging in non-production env. Pass --allow-dev to override.")
        return 2
    if "ngrok" in callback_url and not allow_dev:
        print("REFUSED: ngrok callback URL looks temporary. Pass --allow-dev to override.")
        return 2
    client = AircallClient(settings)
    try:
        out = await client.configure_number_for_messaging(line_id, callback_url)
        print(f"Configured {line_id} -> {callback_url}: {out}")
        print("WARNING: this number can no longer send SMS from the Aircall app.")
        return 0
    except Exception as exc:
        print(f"configure failed: {exc}")
        return 1
    finally:
        await client.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Safe Aircall messaging admin")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="Show messaging config (read-only)")
    s.add_argument("--line-id", type=int, required=True)
    r = sub.add_parser("restore", help="Delete messaging config, back to normal SMS")
    r.add_argument("--line-id", type=int, required=True)
    c = sub.add_parser("configure", help="Point number at a callback URL")
    c.add_argument("--line-id", type=int, required=True)
    c.add_argument("--callback-url", required=True)
    c.add_argument("--allow-dev", action="store_true")
    args = p.parse_args()
    if args.cmd == "status":
        raise SystemExit(asyncio.run(cmd_status(args.line_id)))
    if args.cmd == "restore":
        raise SystemExit(asyncio.run(cmd_restore(args.line_id)))
    raise SystemExit(asyncio.run(cmd_configure(args.line_id, args.callback_url, args.allow_dev)))


if __name__ == "__main__":
    main()
