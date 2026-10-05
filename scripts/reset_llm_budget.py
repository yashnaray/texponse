from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

import redis.asyncio as aioredis

from texponse.config import get_settings


async def main() -> None:
    s = get_settings()
    key = f"texponse:llm_spend:{datetime.now(UTC).strftime('%Y-%m-%d')}"
    rc = aioredis.from_url(s.redis_url, socket_connect_timeout=5)
    try:
        spent = int(await rc.get(key) or 0)
        budget = s.effective_daily_budget
        print(f"today: {spent}/{budget} LLM attempts used (provider={s.llm_provider})")
        if "--reset" in sys.argv:
            await rc.delete(key)
            print(f"RESET: deleted {key}")
        else:
            print("reset with: python scripts/reset_llm_budget.py --reset")
    finally:
        await rc.aclose()


asyncio.run(main())