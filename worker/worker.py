import os
import time
import json
import socket
import random
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────
WORKER_ID = socket.gethostname()
INTERVAL_SECONDS = int(os.getenv("WORKER_INTERVAL_SECONDS", "60"))
SEED_RECORDS = int(os.getenv("WORKER_SEED_RECORDS", "10"))

# ── Helpers ──────────────────────────────────────────────
def now_ms() -> int:
    return int(time.time() * 1000)

def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def log_event(event: str, **fields) -> None:
    payload = {
        "event": event,
        "ts": now_ms(),
        "worker_id": WORKER_ID,
        **fields,
    }
    print(json.dumps(payload, ensure_ascii=False), flush=True)

# ── In-memory store ──────────────────────────────────────
def seed_records(n: int) -> list:
    today = today_str()
    records = []
    for i in range(n):
        # mix today and yesterday records
        date = today if i % 3 != 0 else "2000-01-01"
        records.append({
            "id": f"record-{i+1:03d}",
            "date": date,
            "created_at": now_ms(),
            "updated_at": now_ms(),
        })
    log_event("store_seeded", total_records=len(records), today=today)
    return records

# ── Worker job ───────────────────────────────────────────
def update_today_timestamps(store: list) -> None:
    today = today_str()
    updated = []
    skipped = []

    for record in store:
        if record["date"] == today:
            record["updated_at"] = now_ms()
            updated.append(record["id"])
        else:
            skipped.append(record["id"])

    log_event(
        "timestamp_update_done",
        today=today,
        updated_count=len(updated),
        skipped_count=len(skipped),
        updated_ids=updated,
    )

# ── Main loop ────────────────────────────────────────────
def main() -> None:
    log_event(
        "worker_started",
        interval_seconds=INTERVAL_SECONDS,
        seed_records=SEED_RECORDS,
    )

    store = seed_records(SEED_RECORDS)

    while True:
        try:
            log_event("job_started", total_records=len(store))
            update_today_timestamps(store)
            log_event("job_finished", sleep_seconds=INTERVAL_SECONDS)

        except Exception as e:
            log_event(
                "job_error",
                error_type=type(e).__name__,
                error_message=str(e),
            )

        time.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
