"""
Stress test suite for the Dossier API.

Three modes:
  1. seed   — Create N dossiers with the full D2 flow (9 steps each)
  2. write  — Create M dossiers via API and time each operation
  3. read   — Fetch random dossiers at a target rate, measure latency

Usage:
  # First, start the API:
  cd gov_dossier_app && uvicorn main:app --host 0.0.0.0 --port 8000

  # Seed 100,000 dossiers directly into the DB (fast, bypasses API):
  python stress_test.py seed --count 100000 --config gov_dossier_app/config.yaml

  # Write benchmark: 100 dossiers via API, time each step:
  python stress_test.py write --count 100 --base-url http://localhost:8000

  # Read benchmark: 500 GET requests at 25/sec:
  python stress_test.py read --total 500 --rate 25 --base-url http://localhost:8000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import statistics
import sys
import time
from datetime import datetime, timezone, timedelta
from uuid import uuid4, UUID

import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("stress")

BASE_URL = "http://localhost:8000"

GEMEENTEN = ["Brugge", "Gent", "Antwerpen", "Leuven", "Mechelen", "Hasselt", "Kortrijk", "Oostende"]
ONDERWERPEN = [
    "Restauratie gevelbekleding", "Plaatsing zonnepanelen", "Renovatie dakstructuur",
    "Verbouwing binnenruimte", "Herstel voegwerk", "Plaatsing ramen", "Aanleg tuin",
    "Restauratie trap", "Vernieuwing elektriciteit", "Isolatie buitenmuur",
]
USERS = ["firma.acme", "jan.aanvrager"]
BEHANDELAARS = ["benjamma", "sophie.tekent"]


def make_d2_flow(dossier_idx: int) -> list[dict]:
    """Generate the 9-step D2 flow for a dossier. Returns list of (method, path, headers, body)."""
    did = f"d2{dossier_idx:06d}-0000-0000-0000-000000000001"
    eid_aanvraag = f"e2{dossier_idx:06d}-0000-0000-0000-000000000001"
    eid_beslissing = f"e2{dossier_idx:06d}-0000-0000-0000-000000000002"
    eid_handtekening = f"e2{dossier_idx:06d}-0000-0000-0000-000000000003"

    # Use fixed credentials matching POC users
    # firma.acme has kbo 0123456789, jan.aanvrager has rrn 85010100123
    user = random.choice(USERS)
    behandelaar = random.choice(BEHANDELAARS)
    gemeente = random.choice(GEMEENTEN)
    onderwerp = random.choice(ONDERWERPEN)
    obj_id = 20000 + dossier_idx
    obj_uri = f"https://id.erfgoed.net/erfgoedobjecten/{obj_id}"

    if user == "firma.acme":
        aanvrager = {"kbo": "0123456789"}
    else:
        aanvrager = {"rrn": "85010100123"}

    v = [str(uuid4()) for _ in range(12)]  # version IDs

    steps = []

    # Pre-generate synthetic file ids for this dossier. The stress test does
    # not actually upload files to the File Service — the read benchmark only
    # measures Pydantic hydration + the FileId-walker + HMAC signing, none of
    # which touch the File Service. The values just need to be opaque strings
    # so the walker treats them as file ids.
    aanvraag_bijlage_fids = [str(uuid4()), str(uuid4())]  # 2 bijlagen per aanvraag
    brief_fids = [str(uuid4()) for _ in range(3)]

    bijlagen_content = [
        {
            "file_id": aanvraag_bijlage_fids[0],
            "filename": "detailplan.pdf",
            "content_type": "application/pdf",
            "size": 102400,
        },
        {
            "file_id": aanvraag_bijlage_fids[1],
            "filename": "fotos.zip",
            "content_type": "application/zip",
            "size": 512000,
        },
    ]

    # Step 1: dienAanvraagIn
    steps.append({
        "path": f"/dossiers/{did}/activities/a0{dossier_idx:06d}-0001-0000-0000-000000000001/dienAanvraagIn",
        "user": user,
        "body": {
            "workflow": "toelatingen",
            "used": [{"entity": obj_uri}],
            "generated": [{
                "entity": f"oe:aanvraag/{eid_aanvraag}@{v[0]}",
                "content": {
                    "onderwerp": onderwerp,
                    "handeling": "renovatie",
                    "aanvrager": aanvrager,
                    "gemeente": gemeente,
                    "object": obj_uri,
                    "bijlagen": bijlagen_content,
                }
            }]
        }
    })

    # Step 2: doeVoorstelBeslissing (onvolledig)
    steps.append({
        "path": f"/dossiers/{did}/activities/a0{dossier_idx:06d}-0002-0000-0000-000000000001/doeVoorstelBeslissing",
        "user": behandelaar,
        "body": {
            "used": [{"entity": f"oe:aanvraag/{eid_aanvraag}@{v[0]}"}],
            "generated": [{
                "entity": f"oe:beslissing/{eid_beslissing}@{v[1]}",
                "content": {
                    "beslissing": "onvolledig",
                    "datum": datetime.now(timezone.utc).isoformat(),
                    "object": obj_uri,
                    "brief": brief_fids[0],
                }
            }]
        }
    })

    # Step 3: tekenBeslissing (sophie signs → onvolledig)
    steps.append({
        "path": f"/dossiers/{did}/activities/a0{dossier_idx:06d}-0003-0000-0000-000000000001/tekenBeslissing",
        "user": "sophie.tekent",
        "body": {
            "used": [{"entity": f"oe:beslissing/{eid_beslissing}@{v[1]}"}],
            "generated": [{
                "entity": f"oe:handtekening/{eid_handtekening}@{v[2]}",
                "content": {"getekend": True}
            }]
        }
    })

    # Step 4: vervolledigAanvraag
    steps.append({
        "path": f"/dossiers/{did}/activities/a0{dossier_idx:06d}-0004-0000-0000-000000000001/vervolledigAanvraag",
        "user": user,
        "body": {
            "used": [{"entity": obj_uri}],
            "generated": [{
                "entity": f"oe:aanvraag/{eid_aanvraag}@{v[3]}",
                "derivedFrom": f"oe:aanvraag/{eid_aanvraag}@{v[0]}",
                "content": {
                    "onderwerp": f"{onderwerp} - aangevuld",
                    "handeling": "renovatie",
                    "aanvrager": aanvrager,
                    "gemeente": gemeente,
                    "object": obj_uri,
                    "bijlagen": bijlagen_content,
                }
            }]
        }
    })

    # Step 5: bewerkAanvraag
    steps.append({
        "path": f"/dossiers/{did}/activities/a0{dossier_idx:06d}-0005-0000-0000-000000000001/bewerkAanvraag",
        "user": behandelaar,
        "body": {
            "used": [{"entity": obj_uri}],
            "generated": [{
                "entity": f"oe:aanvraag/{eid_aanvraag}@{v[4]}",
                "derivedFrom": f"oe:aanvraag/{eid_aanvraag}@{v[3]}",
                "content": {
                    "onderwerp": f"{onderwerp} - bewerkt",
                    "handeling": "renovatie",
                    "aanvrager": aanvrager,
                    "gemeente": gemeente,
                    "object": obj_uri,
                    "bijlagen": bijlagen_content,
                }
            }]
        }
    })

    # Step 6: doeVoorstelBeslissing (goedgekeurd)
    steps.append({
        "path": f"/dossiers/{did}/activities/a0{dossier_idx:06d}-0006-0000-0000-000000000001/doeVoorstelBeslissing",
        "user": behandelaar,
        "body": {
            "used": [{"entity": f"oe:aanvraag/{eid_aanvraag}@{v[4]}"}],
            "generated": [{
                "entity": f"oe:beslissing/{eid_beslissing}@{v[5]}",
                "derivedFrom": f"oe:beslissing/{eid_beslissing}@{v[1]}",
                "content": {
                    "beslissing": "goedgekeurd",
                    "datum": datetime.now(timezone.utc).isoformat(),
                    "object": obj_uri,
                    "brief": brief_fids[1],
                }
            }]
        }
    })

    # Step 7: tekenBeslissing (sophie declines)
    steps.append({
        "path": f"/dossiers/{did}/activities/a0{dossier_idx:06d}-0007-0000-0000-000000000001/tekenBeslissing",
        "user": "sophie.tekent",
        "body": {
            "used": [{"entity": f"oe:beslissing/{eid_beslissing}@{v[5]}"}],
            "generated": [{
                "entity": f"oe:handtekening/{eid_handtekening}@{v[6]}",
                "derivedFrom": f"oe:handtekening/{eid_handtekening}@{v[2]}",
                "content": {"getekend": False}
            }]
        }
    })

    # Step 8: doeVoorstelBeslissing (goedgekeurd again)
    steps.append({
        "path": f"/dossiers/{did}/activities/a0{dossier_idx:06d}-0008-0000-0000-000000000001/doeVoorstelBeslissing",
        "user": behandelaar,
        "body": {
            "used": [{"entity": f"oe:aanvraag/{eid_aanvraag}@{v[4]}"}],
            "generated": [{
                "entity": f"oe:beslissing/{eid_beslissing}@{v[7]}",
                "derivedFrom": f"oe:beslissing/{eid_beslissing}@{v[5]}",
                "content": {
                    "beslissing": "goedgekeurd",
                    "datum": datetime.now(timezone.utc).isoformat(),
                    "object": obj_uri,
                    "brief": brief_fids[2],
                }
            }]
        }
    })

    # Step 9: tekenBeslissing (sophie signs → goedgekeurd)
    steps.append({
        "path": f"/dossiers/{did}/activities/a0{dossier_idx:06d}-0009-0000-0000-000000000001/tekenBeslissing",
        "user": "sophie.tekent",
        "body": {
            "used": [{"entity": f"oe:beslissing/{eid_beslissing}@{v[7]}"}],
            "generated": [{
                "entity": f"oe:handtekening/{eid_handtekening}@{v[8]}",
                "derivedFrom": f"oe:handtekening/{eid_handtekening}@{v[6]}",
                "content": {"getekend": True}
            }]
        }
    })

    return did, steps


# ─── WRITE BENCHMARK ──────────────────────────────────────────────

async def write_benchmark(count: int, base_url: str):
    """Create N dossiers via the API, timing each step."""
    logger.info(f"Write benchmark: {count} dossiers × 9 steps = {count * 9} requests")

    step_times: dict[int, list[float]] = {i: [] for i in range(9)}
    dossier_times: list[float] = []
    errors = 0

    async with aiohttp.ClientSession() as session:
        for idx in range(count):
            offset = 100000 + idx  # avoid collision with seeded data
            did, steps = make_d2_flow(offset)
            dossier_start = time.monotonic()

            for step_idx, step in enumerate(steps):
                url = f"{base_url}{step['path']}"
                headers = {"Content-Type": "application/json", "X-POC-User": step["user"]}

                t0 = time.monotonic()
                try:
                    async with session.put(url, json=step["body"], headers=headers) as resp:
                        if resp.status >= 400:
                            body = await resp.text()
                            logger.warning(f"Dossier {idx} step {step_idx+1}: HTTP {resp.status} — {body[:100]}")
                            errors += 1
                        else:
                            await resp.json()
                except Exception as e:
                    logger.error(f"Dossier {idx} step {step_idx+1}: {e}")
                    errors += 1
                elapsed = time.monotonic() - t0
                step_times[step_idx].append(elapsed)

            dossier_elapsed = time.monotonic() - dossier_start
            dossier_times.append(dossier_elapsed)

            if (idx + 1) % 10 == 0:
                avg = statistics.mean(dossier_times[-10:])
                logger.info(f"  {idx + 1}/{count} dossiers — last 10 avg: {avg:.3f}s/dossier")

    # Report
    print("\n" + "=" * 70)
    print(f"WRITE BENCHMARK RESULTS — {count} dossiers")
    print("=" * 70)
    print(f"Total dossiers: {count}")
    print(f"Total requests: {count * 9}")
    print(f"Errors: {errors}")
    print(f"Total time: {sum(dossier_times):.1f}s")
    print(f"Avg per dossier: {statistics.mean(dossier_times):.3f}s")
    print(f"Median per dossier: {statistics.median(dossier_times):.3f}s")
    if len(dossier_times) > 1:
        print(f"P95 per dossier: {sorted(dossier_times)[int(len(dossier_times)*0.95)]:.3f}s")
    print()

    step_names = [
        "dienAanvraagIn", "doeVoorstelBeslissing", "tekenBeslissing(sign)",
        "vervolledigAanvraag", "bewerkAanvraag", "doeVoorstelBeslissing(2)",
        "tekenBeslissing(decline)", "doeVoorstelBeslissing(3)", "tekenBeslissing(final)"
    ]
    print(f"{'Step':<30} {'Avg':>8} {'Med':>8} {'P95':>8} {'Max':>8}")
    print("-" * 70)
    for i, name in enumerate(step_names):
        times = step_times[i]
        if not times:
            continue
        avg = statistics.mean(times)
        med = statistics.median(times)
        p95 = sorted(times)[int(len(times) * 0.95)]
        mx = max(times)
        print(f"{name:<30} {avg*1000:>7.1f}ms {med*1000:>7.1f}ms {p95*1000:>7.1f}ms {mx*1000:>7.1f}ms")


# ─── READ BENCHMARK ───────────────────────────────────────────────

async def read_benchmark(total: int, rate: int, base_url: str):
    """Fetch random dossiers at a target rate."""
    logger.info(f"Read benchmark: {total} requests at {rate}/sec target")

    # First, get list of dossier IDs
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{base_url}/dossiers",
            headers={"X-POC-User": "claeyswo"}
        ) as resp:
            data = await resp.json()
            dossier_ids = [d["id"] for d in data.get("dossiers", [])]

    if not dossier_ids:
        logger.error("No dossiers found! Run seed or write first.")
        return

    logger.info(f"Found {len(dossier_ids)} dossiers to sample from")

    latencies: list[float] = []
    errors = 0
    interval = 1.0 / rate

    async with aiohttp.ClientSession() as session:
        sem = asyncio.Semaphore(50)  # max concurrent

        async def fetch_one(idx: int):
            nonlocal errors
            did = random.choice(dossier_ids)
            url = f"{base_url}/dossiers/{did}"
            headers = {"X-POC-User": "claeyswo"}

            async with sem:
                t0 = time.monotonic()
                try:
                    async with session.get(url, headers=headers) as resp:
                        await resp.read()
                        if resp.status >= 400:
                            errors += 1
                except Exception:
                    errors += 1
                elapsed = time.monotonic() - t0
                latencies.append(elapsed)

        tasks = []
        start = time.monotonic()
        for i in range(total):
            tasks.append(asyncio.create_task(fetch_one(i)))
            # Pace to target rate
            expected_time = (i + 1) * interval
            actual_time = time.monotonic() - start
            if actual_time < expected_time:
                await asyncio.sleep(expected_time - actual_time)

            if (i + 1) % 100 == 0:
                logger.info(f"  {i + 1}/{total} requests sent")

        await asyncio.gather(*tasks)
        wall_time = time.monotonic() - start

    # Report
    print("\n" + "=" * 70)
    print(f"READ BENCHMARK RESULTS — {total} requests")
    print("=" * 70)
    print(f"Target rate: {rate}/sec")
    print(f"Actual rate: {total / wall_time:.1f}/sec")
    print(f"Wall time: {wall_time:.1f}s")
    print(f"Errors: {errors}")
    print(f"Dossiers sampled from: {len(dossier_ids)}")
    print()
    if latencies:
        latencies_sorted = sorted(latencies)
        print(f"Latency avg:  {statistics.mean(latencies)*1000:.1f}ms")
        print(f"Latency med:  {statistics.median(latencies)*1000:.1f}ms")
        print(f"Latency P95:  {latencies_sorted[int(len(latencies)*0.95)]*1000:.1f}ms")
        print(f"Latency P99:  {latencies_sorted[int(len(latencies)*0.99)]*1000:.1f}ms")
        print(f"Latency max:  {max(latencies)*1000:.1f}ms")


# ─── DB SEEDER (bypasses API for speed) ───────────────────────────

async def seed_dossiers(count: int, config_path: str):
    """Seed N dossiers directly into the DB for read benchmarks."""
    logger.info(f"Seeding {count} dossiers directly into DB...")

    # Import engine internals
    sys.path.insert(0, ".")
    from gov_dossier_engine.app import load_config_and_registry, SYSTEM_USER
    from gov_dossier_engine.db import init_db, create_tables, get_session_factory
    from gov_dossier_engine.db.models import Repository
    from gov_dossier_engine.engine import execute_activity

    config, registry = load_config_and_registry(config_path)
    db_url = config.get("database", {}).get("url", "sqlite+aiosqlite:///./dossiers.db")
    await init_db(db_url)
    await create_tables()

    plugin = registry.get("toelatingen")
    if not plugin:
        logger.error("Toelatingen plugin not found!")
        return

    session_factory = get_session_factory()
    batch_size = 500
    start = time.monotonic()

    # Enable WAL mode for SQLite (much faster concurrent writes)
    from sqlalchemy import text
    async with session_factory() as session:
        try:
            await session.execute(text("PRAGMA journal_mode=WAL"))
            await session.execute(text("PRAGMA synchronous=NORMAL"))
            await session.execute(text("PRAGMA cache_size=-64000"))  # 64MB cache
            await session.commit()
            logger.info("SQLite WAL mode enabled")
        except Exception:
            pass  # Not SQLite, ignore

    # Pre-compute activity def and user lookups
    from gov_dossier_engine.auth import User
    act_def_map = {a["name"]: a for a in plugin.workflow.get("activities", [])}
    poc_users = plugin.workflow.get("poc_users", [])
    user_map = {}
    for u in poc_users:
        user_map[u["username"]] = User(
            id=str(u["id"]),
            type=u["type"],
            name=u["name"],
            roles=u.get("roles", []),
            properties=u.get("properties", {}),
        )

    for batch_start in range(0, count, batch_size):
        batch_end = min(batch_start + batch_size, count)

        async with session_factory() as session:
            async with session.begin():
                repo = Repository(session)

                for idx in range(batch_start, batch_end):
                    did, steps = make_d2_flow(idx)
                    dossier_id = UUID(did)

                    for step_idx, step in enumerate(steps):
                        path_parts = step["path"].split("/")
                        activity_id = UUID(path_parts[4])
                        activity_type = path_parts[5] if len(path_parts) > 5 else step["body"].get("type")

                        act_def = act_def_map.get(activity_type)
                        if not act_def:
                            continue

                        user = user_map.get(step["user"])
                        if not user:
                            continue

                        try:
                            is_last_step = (step_idx == len(steps) - 1)
                            await execute_activity(
                                plugin=plugin,
                                activity_def=act_def,
                                repo=repo,
                                dossier_id=dossier_id,
                                activity_id=activity_id,
                                user=user,
                                role=act_def.get("default_role", ""),
                                used_items=step["body"].get("used", []),
                                generated_items=step["body"].get("generated", []),
                                workflow_name=step["body"].get("workflow"),
                                skip_cache=not is_last_step,
                            )
                            await repo.session.flush()
                        except Exception as e:
                            logger.warning(f"Dossier {idx} step {step_idx}: {e}")
                            break

        elapsed = time.monotonic() - start
        rate = (batch_end) / elapsed if elapsed > 0 else 0
        logger.info(f"  {batch_end}/{count} dossiers seeded ({rate:.0f} dossiers/sec)")

    elapsed = time.monotonic() - start
    total_activities = count * 9
    logger.info(f"Seeding complete: {count} dossiers, {total_activities} activities in {elapsed:.1f}s")
    logger.info(f"Rate: {count/elapsed:.1f} dossiers/sec, {total_activities/elapsed:.0f} activities/sec")


# ─── MAIN ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Dossier API stress test")
    sub = parser.add_subparsers(dest="command")

    seed_p = sub.add_parser("seed", help="Seed dossiers directly into DB")
    seed_p.add_argument("--count", type=int, default=1000)
    seed_p.add_argument("--config", default="gov_dossier_app/config.yaml")

    write_p = sub.add_parser("write", help="Write benchmark via API")
    write_p.add_argument("--count", type=int, default=100)
    write_p.add_argument("--base-url", default=BASE_URL)

    read_p = sub.add_parser("read", help="Read benchmark via API")
    read_p.add_argument("--total", type=int, default=500)
    read_p.add_argument("--rate", type=int, default=25)
    read_p.add_argument("--base-url", default=BASE_URL)

    args = parser.parse_args()

    if args.command == "seed":
        asyncio.run(seed_dossiers(args.count, args.config))
    elif args.command == "write":
        asyncio.run(write_benchmark(args.count, args.base_url))
    elif args.command == "read":
        asyncio.run(read_benchmark(args.total, args.rate, args.base_url))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
