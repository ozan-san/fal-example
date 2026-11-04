"""Sends a small, fixed number of requests at a steady rate to the demo API."""

import argparse
import asyncio
import random
from collections import Counter

import httpx

PROMPTS = ["a red bicycle", "a lighthouse at dusk", "a bowl of ramen", "a paper boat on a lake"]
MAX_TOTAL = 200


async def send(client: httpx.AsyncClient, url: str, tally: Counter) -> None:
    try:
        response = await client.post(url, json={"prompt": random.choice(PROMPTS)})
        tally[response.status_code] += 1
    except httpx.HTTPError as exc:
        tally[type(exc).__name__] += 1


async def run(url: str, total: int, rps: float) -> Counter:
    tally: Counter = Counter()
    async with httpx.AsyncClient(timeout=180) as client:
        tasks = []
        for i in range(total):
            tasks.append(asyncio.create_task(send(client, url, tally)))
            if i < total - 1:
                await asyncio.sleep(1 / rps)
        await asyncio.gather(*tasks)
    return tally


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8010/generate")
    parser.add_argument("--total", type=int, default=30, help=f"requests to send, max {MAX_TOTAL}")
    parser.add_argument("--rps", type=float, default=0.2, help="requests per second")
    args = parser.parse_args()
    if not 1 <= args.total <= MAX_TOTAL:
        parser.error(f"--total must be between 1 and {MAX_TOTAL}")
    if args.rps <= 0:
        parser.error("--rps must be positive")
    print(f"Sending {args.total} requests at {args.rps} rps to {args.url}", flush=True)
    print("Results by status:", dict(asyncio.run(run(args.url, args.total, args.rps))), flush=True)


if __name__ == "__main__":
    main()
