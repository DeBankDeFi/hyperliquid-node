#!/usr/bin/env python3
"""Trigger a referrer snapshot inside the node container and export CSV."""

from __future__ import annotations

import csv
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

try:
    import pymysql
except ImportError:  # pragma: no cover
    pymysql = None


CONTAINER_NAME = os.environ.get("HL_NODE_CONTAINER", "hyperliquid-node-node-1")
DOCKER_BIN = os.environ.get("HL_DOCKER_BIN", "docker")
REFERRER_ADDRESS = "0xAd9bE64fD7a35d99a138b87CB212BAefbCDCf045"
SNAPSHOT_PATH = os.environ.get("HL_REFERRER_SNAPSHOT", "/tmp/referrer")
CSV_OUTPUT_PATH = Path(os.environ.get("HL_REFERRER_CSV", "/tmp/referrer.csv"))
DB_HOST = os.environ.get("HL_DB_HOST")
DB_PORT = os.environ.get("HL_DB_PORT")
DB_USER = os.environ.get("HL_DB_USER")
DB_PASSWORD = os.environ.get("HL_DB_PASSWORD")
DB_NAME = os.environ.get("HL_DB_NAME")
DB_TABLE = os.environ.get("HL_DB_TABLE", "referral_stats")


def build_snapshot_command() -> str:
    payload = json.dumps(
        {
            "type": "fileSnapshot",
            "request": {"type": "referrerStates"},
            "outPath": SNAPSHOT_PATH,
            "includeHeightInOutput": True,
        }
    )
    quoted_payload = shlex.quote(payload)
    return (
        "curl -s -X POST 'http://127.0.0.1:3001/info' "
        "--header 'Content-Type: application/json' "
        f"--data {quoted_payload}"
    )


def run_docker_exec(command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command inside the container."""

    docker_command = ["sudo", DOCKER_BIN, "exec", CONTAINER_NAME, "bash", "-c", command]

    return subprocess.run(
        docker_command,
        text=True,
        capture_output=True,
        check=check,
    )


def trigger_snapshot() -> None:
    """Invoke the snapshot curl command inside the container."""

    try:
        result = run_docker_exec(build_snapshot_command())
    except subprocess.CalledProcessError as exc:  # pragma: no cover
        print(
            f"Error running snapshot command: {exc.stderr.strip() if exc.stderr else exc}",
            file=sys.stderr,
        )
        raise

    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)


def fetch_snapshot_json() -> list:
    """Read the JSON snapshot from the container and return it as Python data."""

    try:
        result = run_docker_exec(f"cat {shlex.quote(SNAPSHOT_PATH)}")
    except subprocess.CalledProcessError as exc:  # pragma: no cover
        print(
            f"Error reading {SNAPSHOT_PATH}: {exc.stderr.strip() if exc.stderr else exc}",
            file=sys.stderr,
        )
        raise

    content = result.stdout.strip()
    if not content:
        raise RuntimeError(f"Snapshot file {SNAPSHOT_PATH} is empty")

    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse JSON from {SNAPSHOT_PATH}") from exc


def extract_referrals(snap: list) -> list[dict]:
    """Extract referral state entries for the target address."""

    if not isinstance(snap, list) or len(snap) < 2:
        raise ValueError("Unexpected snapshot structure: expected [height, [entries]]")

    entries = snap[1]
    if not isinstance(entries, list):
        raise ValueError("Unexpected snapshot structure: entries should be a list")

    target_lower = REFERRER_ADDRESS.lower()

    for record in entries:
        if not (isinstance(record, list) and len(record) == 2):
            continue
        address, payload = record
        if not isinstance(address, str) or address.lower() != target_lower:
            continue

        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        referral_states = data.get("referralStates")
        if not isinstance(referral_states, list):
            return []

        return [state for state in referral_states if isinstance(state, dict)]

    return []


def write_csv(referrals: list[dict]) -> None:
    """Write the flattened referral data to the CSV output path."""

    CSV_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    with CSV_OUTPUT_PATH.open("w", newline="") as csvfile:
        writer = csv.DictWriter(
            csvfile,
            fieldnames=[
                "user",
                "cumVlm",
                "cumRewardedFeesSinceReferred",
                "cumFeesRewardedToReferrer",
                "timeJoined",
            ],
        )
        writer.writeheader()
        for state in referrals:
            writer.writerow(
                {
                    "user": state.get("user", ""),
                    "cumVlm": state.get("cumVlm", ""),
                    "cumRewardedFeesSinceReferred": state.get(
                        "cumRewardedFeesSinceReferred", ""
                    ),
                    "cumFeesRewardedToReferrer": state.get(
                        "cumFeesRewardedToReferrer", ""
                    ),
                    "timeJoined": state.get("timeJoined", ""),
                }
            )


def parse_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def get_db_config() -> dict | None:
    if not DB_HOST:
        return None

    missing = [
        name
        for name, val in {
            "HL_DB_USER": DB_USER,
            "HL_DB_PASSWORD": DB_PASSWORD,
            "HL_DB_NAME": DB_NAME,
        }.items()
        if not val
    ]

    if missing:
        print(
            "Skipping database sync; missing environment variables: "
            + ", ".join(missing),
            file=sys.stderr,
        )
        return None

    try:
        port = int(DB_PORT) if DB_PORT else 3306
    except ValueError:
        print(
            f"Invalid HL_DB_PORT value '{DB_PORT}', defaulting to 3306",
            file=sys.stderr,
        )
        port = 3306

    return {
        "host": DB_HOST,
        "port": port,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "database": DB_NAME,
        "table": DB_TABLE,
    }


def sync_to_db(referrals: list[dict]) -> None:
    config = get_db_config()
    if not config or not referrals:
        return

    if pymysql is None:
        raise RuntimeError(
            "pymysql is required for database sync. Install it or unset HL_DB_HOST."
        )

    rows = []
    for state in referrals:
        user = state.get("user")
        if not user:
            continue
        rows.append(
            (
                user,
                parse_float(state.get("cumVlm")),
                parse_float(state.get("cumRewardedFeesSinceReferred")),
                parse_float(state.get("cumFeesRewardedToReferrer")),
                parse_float(state.get("timeJoined")),
            )
        )

    if not rows:
        return

    sql = (
        f"INSERT INTO `{config['table']}` "
        "(id, cum_vlm, cum_rewarded_fees_since_referred, cum_fees_rewarded_to_referrer, time_joined) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE "
        "cum_vlm = VALUES(cum_vlm), "
        "cum_rewarded_fees_since_referred = VALUES(cum_rewarded_fees_since_referred), "
        "cum_fees_rewarded_to_referrer = VALUES(cum_fees_rewarded_to_referrer), "
        "time_joined = VALUES(time_joined)"
    )

    connection = pymysql.connect(
        host=config["host"],
        port=config["port"],
        user=config["user"],
        password=config["password"],
        database=config["database"],
        charset="utf8mb4",
        autocommit=False,
    )

    try:
        with connection.cursor() as cursor:
            cursor.executemany(sql, rows)
        connection.commit()
        print(
            f"Upserted {len(rows)} rows into {config['database']}.{config['table']}",
            file=sys.stderr,
        )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    try:
        trigger_snapshot()
        snapshot = fetch_snapshot_json()
        referrals = extract_referrals(snapshot)
        if not referrals:
            print(
                "No referral states found for"
                f" {REFERRER_ADDRESS}. Writing empty CSV.",
                file=sys.stderr,
            )
        write_csv(referrals)
        sync_to_db(referrals)
    except Exception as exc:  # pragma: no cover
        print(f"referrer_snapshot failed: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {len(referrals)} referral rows to {CSV_OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

