"""
init_db.py — Standalone script to initialize the SQLite database and
optionally create a test API key.
"""

import asyncio
import os
import secrets

from database import init_db, create_api_key


async def main():
    await init_db()
    print("Database initialized.")

    # Create a test starter API key if requested
    if os.getenv("CREATE_TEST_KEY"):
        api_key = "pcs_" + secrets.token_hex(24)
        await create_api_key(api_key, tier="starter")
        print(f"Test API key (starter): {api_key}")

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
