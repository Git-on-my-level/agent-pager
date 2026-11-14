"""Pager incident policy and private audit tools. Standard library only.

No transport, credentials, process argv, or autonomous recovery lives here.
Incident state is serialized by the caller's pager lock. Audit has its own lock
so refused calls and transport failures are recorded independently of sent state.
"""
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time

AUDIT_DAYS = 30
AUDIT_BYTES = 2 * 1024 * 1024


def audit(directory, event):
    """Bounded daily segments; at most four segments/day, owner-only storage."""
    try:
        root = Path(directory) / "audit"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(root / "lock", os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as lock:
            # Audit contention must never block an emergency notification.
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            day = time.strftime("%Y-%m-%d", time.gmtime())
            path = root / (day + ".jsonl")
            if path.exists() and path.stat().st_size >= AUDIT_BYTES:
                for i in (3, 2, 1):
                    older = root / (day + ".jsonl" + ("." + str(i - 1) if i > 1 else ""))
                    if older.exists():
                        older.replace(root / (day + ".jsonl." + str(i)))
            fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "a") as handle:
                handle.write(json.dumps(dict(at=time.time(), **event), sort_keys=True) + "\n")
            for old in root.glob("*.jsonl*"):
                if time.time() - old.stat().st_mtime > AUDIT_DAYS * 86400:
                    old.unlink()
        return True
    except (OSError, ValueError):
        return False


def history(directory, source=None, limit=100):
    rows = []
    root = Path(directory) / "audit"
    # Rotation names sort newest-first within a day; timestamps define order.
    for path in sorted(root.glob("*.jsonl*"), reverse=True):
        try:
            for line in path.read_text().splitlines():
                try:
                    row = json.loads(line)
                    if isinstance(row, dict) and isinstance(row.get("at"), (int, float)) and (
                            source is None or row.get("source") == source):
                        rows.append(row)
                except ValueError:
                    continue
        except OSError:
            continue
    return sorted(rows, key=lambda row: row["at"])[-limit:]


def callers(directory):
    result = {}
    for row in history(directory, limit=1000000):
        source = row.get("source") or "unattributed"
        entry = result.setdefault(source, dict(source=source, first_seen=row["at"], last_seen=row["at"],
                                               outcomes={}, parents=[], legacy_calls=0))
        entry["last_seen"] = row["at"]
        outcome = row.get("outcome", "unknown")
        entry["outcomes"][outcome] = entry["outcomes"].get(outcome, 0) + 1
        parent = row.get("parent_comm")
        if parent and parent not in entry["parents"]:
            entry["parents"].append(parent)
        entry["legacy_calls"] += not bool(row.get("incident"))
    return sorted(result.values(), key=lambda row: row["source"])


def scan(root):
    """Candidate invocation sites, not a claim of active/exhaustive runtime use.

    Only file/line/literal source is emitted, never code or arbitrary argv.
    No symlink traversal, repository metadata, credentials or runtime logs.
    """
    root = Path(root).resolve()
    found = []
    skip = {".git", "node_modules", ".venv", "venv", "__pycache__", "secrets", "audit", ".cache"}
    invocation = re.compile(r"(?:['\"](?:[^'\"]*/)?page['\"]|\bpage\s+--(?:sev|source|dedupe))")
    source = re.compile(r"--source['\"]?\s*[,=]?\s*['\"]?([\w.-]+)")
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = [name for name in names if name not in skip and not name.startswith(".")]
        for name in files:
            path = Path(directory) / name
            if path.is_symlink() or path.suffix not in {".py", ".sh", ".json", ".plist", ".yaml", ".yml"}:
                continue
            try:
                if path.stat().st_size > 1024 * 1024:
                    continue
                for number, line in enumerate(path.read_text().splitlines(), 1):
                    if invocation.search(line):
                        match = source.search(line)
                        found.append(dict(path=str(path.relative_to(root)), line=number,
                                          source=match[1] if match else None))
            except (OSError, UnicodeError):
                continue
    return found


def timestamp(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("observation time requires a timezone")
    return parsed.timestamp()


def incident_decision(previous, *, condition, observed_at, now, wake_after=600,
                      resolve_after=3600, max_age=300, handling="unhandled",
                      human_action=None, requested="wake"):
    """Return (state, severity-or-None, reason, stable-episode).

    Unknown/stale observations cannot clear an incident or justify a wake.
    Automated recovery suppresses wakes, not audit or the initial info notice.
    A brief clear never creates a new episode. Persistent unhandled risk with a
    concrete human action escalates after the bounded triage interval.
    """
    state = dict(previous or {})
    generation = state.get("generation", 0)
    if not 0 <= now - observed_at <= max_age:
        return state, None, "stale_observation", generation
    if condition == "unknown":
        return state, None, "unknown_observation", generation
    if condition == "resolved":
        if not state.get("active"):
            # Keep retrying a failed resolution delivery with the same identity;
            # transport dedupe suppresses it after acceptance.
            return state, "info" if generation else None, "resolved" if generation else "already_resolved", generation
        state.setdefault("clear_since", observed_at)
        if observed_at - state["clear_since"] < resolve_after:
            return state, None, "recovery_debounce", generation
        state.update(active=False)
        return state, "info", "resolved", generation
    if not state.get("active"):
        generation += 1
        state = dict(active=True, generation=generation, first_seen=observed_at)
    elif observed_at - state.get("last_firing", observed_at) > max_age or "clear_since" in state:
        # Require continuous current evidence, while keeping the incident key.
        state["first_seen"] = observed_at
    state.pop("clear_since", None)
    state["last_firing"] = max(observed_at, state.get("last_firing", observed_at))
    if requested != "wake":
        return state, "info", "informational", generation
    if handling == "automatic":
        return state, "info", "recovery_active", generation
    if not human_action:
        return state, "info", "no_human_action", generation
    if observed_at - state["first_seen"] < wake_after:
        return state, "info", "triage_grace", generation
    return state, "wake", "human_action_required", generation


def incident_key(source, incident):
    return hashlib.sha256((source + "|" + incident).encode()).hexdigest()[:24]
