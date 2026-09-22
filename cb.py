import asyncio

from texponse.config import get_settings
from texponse.integrations.aircall import AircallClient

CALLBACK = "https://demeanor-slobbery-rentable.ngrok-free.dev/webhooks/aircall/callback"


async def main():
    client = AircallClient(get_settings())
    print(await client.configure_number_for_messaging(1244782, CALLBACK))
    await client.close()


asyncio.run(main())
