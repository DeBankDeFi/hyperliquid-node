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


CONTAINER_NAME = os.environ.get("HL_NODE_CONTAINER", "hyperliquid-node-node-1")
DOCKER_BIN = os.environ.get("HL_DOCKER_BIN", "docker")
REFERRER_ADDRESS = "0xAd9bE64fD7a35d99a138b87CB212BAefbCDCf045"
SNAPSHOT_PATH = os.environ.get("HL_REFERRER_SNAPSHOT", "/tmp/referrer")
CSV_OUTPUT_PATH = Path(os.environ.get("HL_REFERRER_CSV", "/tmp/referrer.csv"))


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
    except Exception as exc:  # pragma: no cover
        print(f"referrer_snapshot failed: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {len(referrals)} referral rows to {CSV_OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

