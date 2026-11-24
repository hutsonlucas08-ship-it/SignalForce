"""FOMO Copilot data boundary.

Only sanitized read-only snapshots are accepted. FOMO session credentials,
cookies, wallet signing material, and trade-execution data are never stored.
"""
import json
from datetime import datetime, timezone

ALLOWED_TYPES = {"spot_swaps", "clans_24h", "clans_7d", "clans_30d", "trader_leaderboard"}
MAX_SNAPSHOT_BYTES = 500_000
BLOCKED_KEYS = {
    "authorization", "cookie", "setcookie", "accesstoken", "refreshtoken",
    "sessiontoken", "sessionid", "apikey", "privatekey", "secret", "password",
    "seed", "seedphrase", "mnemonic", "csrftoken",
}


class FomoSnapshotError(ValueError):
    pass


def _check_no_secrets(value, path="payload"):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "").replace("_", "")
            if (normalized in BLOCKED_KEYS or normalized.endswith("secret")
                    or normalized.endswith("token") or normalized.endswith("privatekey")):
                raise FomoSnapshotError(f"Sensitive field is not accepted: {path}.{key}")
            _check_no_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value[:2000]):
            _check_no_secrets(child, f"{path}[{index}]")


def validate_snapshot(snapshot_type: str, payload: dict) -> str:
    if snapshot_type not in ALLOWED_TYPES:
        raise FomoSnapshotError("Unsupported FOMO snapshot type")
    if not isinstance(payload, dict):
        raise FomoSnapshotError("Snapshot payload must be an object")
    rows_key = "swaps" if snapshot_type == "spot_swaps" else "clans"
    rows = payload.get(rows_key)
    row_limit = 100 if snapshot_type == "spot_swaps" else 50
    if not isinstance(rows, list) or len(rows) > row_limit:
        raise FomoSnapshotError(f"{snapshot_type} must contain at most {row_limit} records")
    if any(not isinstance(row, dict) for row in rows):
        raise FomoSnapshotError("Snapshot records must be objects")
    _check_no_secrets(payload)
    try:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise FomoSnapshotError("Snapshot must contain valid JSON data") from exc
    if len(encoded.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise FomoSnapshotError("Snapshot is too large (maximum 500 KB)")
    return encoded


def parse_observed_at(value):
    if not value:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not isinstance(value, str) or len(value) > 40:
        raise FomoSnapshotError("Invalid observation timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FomoSnapshotError("Invalid observation timestamp") from exc
    if parsed.tzinfo is None:
        raise FomoSnapshotError("Observation timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_snapshots(conn, account_id):
    rows = conn.execute(
        "SELECT snapshot_type, payload_json, source, observed_at, created_at "
        "FROM customer_fomo_snapshots WHERE account_id=? ORDER BY snapshot_type",
        (account_id,),
    ).fetchall()
    snapshots = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item.pop("payload_json"))
        except (TypeError, ValueError):
            item["payload"] = {}
        snapshots.append(item)
    return snapshots
