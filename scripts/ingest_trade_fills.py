#!/usr/bin/env python3
"""Ingest one hour of node fills into the hl_trade_fills table.

Reads the hourly node_fills file (one JSON record per line), keeps only the
records whose ``builder`` matches the target address, flattens them and upserts
them into ``hl_trade_fills``.

Intended to run once per hour from cron. With no arguments it processes the
*previous* full hour (the most recently completed file). Pass ``--date`` /
``--hour`` to backfill a specific hour.

The hourly file lives at::

    {HL_FILLS_DIR}/{YYYYMMDD}/{H}

where ``H`` is the hour WITHOUT zero padding (e.g. ``7``, not ``07``) — this
matches the on-disk layout produced by the node.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import pymysql
except ImportError:  # pragma: no cover
    pymysql = None


FILLS_DIR = os.environ.get("HL_FILLS_DIR", "/data/hyper/data/node_fills/hourly")
BUILDER_ADDRESS = os.environ.get(
    "HL_BUILDER_ADDRESS", "0xad9be64fd7a35d99a138b87cb212baefbcdcf045"
).lower()

DB_HOST = os.environ.get(
    "HL_DB_HOST", "rabby-aurora-mysql.cluster-c63u91bhml7c.ap-northeast-1.rds.amazonaws.com"
)
DB_PORT = os.environ.get("HL_DB_PORT", "3306")
DB_USER = os.environ.get("HL_DB_USER", "node_team")
DB_PASSWORD = os.environ.get("HL_DB_PASSWORD")
DB_NAME = os.environ.get("HL_DB_NAME", "defi")
DB_TABLE = os.environ.get("HL_DB_TABLE", "hl_trade_fills")

BATCH_SIZE = int(os.environ.get("HL_BATCH_SIZE", "1000"))

# Columns written, in order. `user_addr` comes from record[0]; the rest are keys
# of the JSON object at record[1] (kept verbatim as column names).
COLUMNS = [
    "user_addr",
    "coin",
    "px",
    "sz",
    "side",
    "time",
    "startPosition",
    "dir",
    "closedPnl",
    "hash",
    "oid",
    "crossed",
    "fee",
    "builderFee",
    "tid",
    "feeToken",
    "builder",
    "twapId",
]
# Keys pulled from the JSON object (everything except the synthetic user_addr).
OBJECT_KEYS = COLUMNS[1:]


def resolve_target(args: argparse.Namespace) -> tuple[str, str]:
    """Return (YYYYMMDD, hour_string) for the hour to process."""

    if args.date and args.hour is not None:
        return args.date, str(int(args.hour))

    # Default: the previous full hour, in UTC (node fill timestamps are UTC).
    target = datetime.now(timezone.utc) - timedelta(hours=1)
    return target.strftime("%Y%m%d"), str(target.hour)


def build_row(record: list) -> tuple | None:
    """Flatten a raw record into a row tuple, or None if it should be skipped."""

    if not (isinstance(record, list) and len(record) == 2):
        return None
    addr, obj = record
    if not isinstance(obj, dict):
        return None
    if str(obj.get("builder", "")).lower() != BUILDER_ADDRESS:
        return None

    values = [addr]
    for key in OBJECT_KEYS:
        val = obj.get(key)
        if key == "crossed" and val is not None:
            val = 1 if val else 0
        values.append(val)
    return tuple(values)


def parse_file(path: Path) -> list[tuple]:
    """Read the hourly file and return flattened rows for matching records."""

    rows: list[tuple] = []
    bad_lines = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1
                if bad_lines <= 5:
                    print(f"  skipping unparseable line {line_no}", file=sys.stderr)
                continue
            row = build_row(record)
            if row is not None:
                rows.append(row)

    if bad_lines:
        print(f"  {bad_lines} unparseable line(s) skipped", file=sys.stderr)
    return rows


def upsert_rows(rows: list[tuple]) -> None:
    """Upsert rows into the target table in batches (idempotent on tid+user)."""

    if pymysql is None:
        raise RuntimeError("pymysql is required. Install it: pip install pymysql")

    col_list = ", ".join(f"`{c}`" for c in COLUMNS)
    placeholders = ", ".join(["%s"] * len(COLUMNS))
    # Update every non-key column on conflict so re-runs converge to latest data.
    updates = ", ".join(
        f"`{c}` = VALUES(`{c}`)" for c in COLUMNS if c not in ("tid", "user_addr")
    )
    sql = (
        f"INSERT INTO `{DB_TABLE}` ({col_list}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {updates}"
    )

    connection = pymysql.connect(
        host=DB_HOST,
        port=int(DB_PORT),
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4",
        autocommit=False,
    )
    try:
        with connection.cursor() as cursor:
            for start in range(0, len(rows), BATCH_SIZE):
                cursor.executemany(sql, rows[start : start + BATCH_SIZE])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="Date to process as YYYYMMDD (default: previous hour)")
    parser.add_argument("--hour", help="Hour to process, 0-23 (default: previous hour)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report counts without writing to the database",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    date_str, hour_str = resolve_target(args)
    path = Path(FILLS_DIR) / date_str / hour_str

    print(f"Processing fills for {date_str} hour {hour_str}: {path}")

    if not path.is_file():
        # Not yet written / already rotated away — not a hard failure for cron.
        print(f"File not found, nothing to do: {path}", file=sys.stderr)
        return 0

    rows = parse_file(path)
    print(f"Matched {len(rows)} record(s) for builder {BUILDER_ADDRESS}")

    if args.dry_run:
        print("Dry run: not writing to the database.")
        return 0

    if not rows:
        return 0

    upsert_rows(rows)
    print(f"Upserted {len(rows)} row(s) into {DB_NAME}.{DB_TABLE}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:  # pragma: no cover
        print(f"ingest_trade_fills failed: {exc}", file=sys.stderr)
        sys.exit(1)
