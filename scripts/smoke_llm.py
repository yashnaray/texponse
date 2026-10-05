from __future__ import annotations

import asyncio

from texponse.config import get_settings
from texponse.integrations.openrouter import build_llm_client


async def main() -> None:
    s = get_settings()
    print("provider:", s.llm_provider, "| model:", s.active_default_model)
    print("effort:", s.gemini_reasoning_effort)
    client = build_llm_client(s)
    result = await client.chat(
        messages=[
            {
                "role": "system",
                "content": (
                    "Reply as a support agent texting a customer. Plain SMS only. "
                    "Never narrate thinking."
                ),
            },
            {"role": "user", "content": "Mark: how many jobs have I had?"},
        ],
        max_tokens=100,
    )
    print("model_used:", result["model"])
    print("tokens:", result["prompt_tokens"], result["completion_tokens"])
    print("reply:", repr(result["content"][:200]))


asyncio.run(main())