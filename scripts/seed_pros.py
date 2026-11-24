#!/usr/bin/env python3
"""Seed Firestore pros/ from professionals.json (opted_in=False)."""

import asyncio
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from app.services.pros import seed_pros_from_json  # noqa: E402


async def main() -> int:
    n = await seed_pros_from_json(force="--force" in sys.argv)
    print(f"Seeded {n} pros")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
