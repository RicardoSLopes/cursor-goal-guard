"""Standalone Cursor workspace hook. Requires Python 3.10+."""

import argparse
import json
import math
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path


GOAL_COMMAND = re.compile(r"^\s*/goal(?:\s|$)")
GOAL_MARKER = '<system_notification source="goal">'
EVENTS = {"beforeSubmitPrompt", "preToolUse", "afterAgentResponse", "stop"}
TOKEN_FIELDS = ("inputTokens", "outputTokens", "cacheWriteTokens", "cacheReadTokens")
AUTH_LIMITS = {
    "session_generations", "session_tokens", "plan_percent", "idle_seconds", "goal_runtime_seconds",
    "fast_mode", "max_mode", "context_tokens",
}


def cursor_database():
    if sys.platform == "win32":
        root = Path(os.environ["APPDATA"])
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def latest_resume(path, session):
    """Return the latest native user bubble iff it is a goal continuation."""
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.10)) as db:
            deadline = time.monotonic() + .15
            db.set_progress_handler(lambda: time.monotonic() >= deadline, 100)
            prefix = f"bubbleId:{session}"
            # UUID order does not reflect chronology. Select two rows by timestamp
            # within the indexed key range, under the same SQLite deadline.
            sql = """SELECT key,value FROM cursorDiskKV WHERE key>=? AND key<?
                AND CASE WHEN json_valid(value) THEN json_extract(value,'$.type')=1 ELSE 0 END
                AND (substr(json_extract(value,'$.createdAt'),-1)='Z'
                    OR substr(json_extract(value,'$.createdAt'),-6,1) IN ('+','-'))"""
            order = " ORDER BY julianday(json_extract(value,'$.createdAt')) DESC, key DESC LIMIT 1"
            bounds = prefix + ":", prefix + ";"
            latest = db.execute(sql + order, bounds).fetchone()
            if latest is None:
                return None
            key, value = latest
            bubble = json.loads(value)
            if not (bubble.get("isSimulatedMsg") is True and bubble.get("simulatedMsgReason") == 31
                    and str(bubble.get("text", "")).startswith(GOAL_MARKER)):
                return None
            human = db.execute(sql + " AND COALESCE(json_extract(value,'$.isSimulatedMsg'),0)=0" + order,
                               bounds).fetchone()
            parser = lambda value: datetime.fromisoformat(value["createdAt"].replace("Z", "+00:00")).timestamp()
            return parser(bubble), key, parser(json.loads(human[1])) if human else None
    except (OSError, sqlite3.Error, TypeError, ValueError, AttributeError):
        pass
    return None


def nonnegative(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def measured_tokens(usage):
    if not isinstance(usage, dict):
        return None
    counters = [usage.get(name) for name in TOKEN_FIELDS]
    return sum(counters) if all(type(value) is int and value >= 0 for value in counters) else None


def record_native_tokens(db, session, generation, payload):
    counters = (payload.get("input_tokens"), payload.get("output_tokens"))
    measured = None
    if all(type(value) is int and value >= 0 for value in counters) and sum(counters) < 2**63:
        measured = sum(counters)
    db.execute("""
        INSERT INTO native_tokens VALUES (?, ?, ?)
        ON CONFLICT(session, generation) DO UPDATE SET tokens=MAX(
            COALESCE(native_tokens.tokens, excluded.tokens),
            COALESCE(excluded.tokens, native_tokens.tokens)
        )
    """, (session, generation, measured))


def model_settings(path, session):
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.1)) as db:
            row = db.execute("SELECT value FROM cursorDiskKV WHERE key=?", ("composerData:" + session,)).fetchone()
            settings = json.loads(row[0]) if row else {}
            return settings if isinstance(settings, dict) else {}
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return {}


def parameters(entries):
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def context_size(value):
    if type(value) is int and value > 0:
        return value
    match = re.fullmatch(r"([1-9]\d*)([km]?)", value.strip().lower()) if isinstance(value, str) else None
    return int(match[1]) * {"": 1, "k": 1000, "m": 1000000}[match[2]] if match else None


def model_limits(policy, settings, hook_params):
    config = settings.get("modelConfig")
    config = config if isinstance(config, dict) else {}
    params = parameters(hook_params)
    for model in parameters(config.get("selectedModels")):
        params.extend(parameters(model.get("parameters")))
    fast = [str(param.get("value")).lower() for param in params if param.get("id") == "fast"]
    contexts = [context_size(param.get("value")) for param in params if param.get("id") == "context"]
    context = max((size for size in contexts if size is not None), default=None)
    if context is None:
        context = context_size(settings.get("contextTokenLimit"))
    max_mode = config.get("maxMode")
    context_limit = policy.get("max_context_tokens")
    context_enabled = nonnegative(context_limit) and context_limit > 0
    reasons = []
    if policy.get("deny_fast") and "true" in fast:
        reasons.append("fast_mode")
    if policy.get("deny_max_mode") and max_mode is True:
        reasons.append("max_mode")
    if context_enabled and context is not None and context >= context_limit:
        reasons.append("context_tokens")
    missing = (
        (policy.get("deny_fast") and not {"true", "false"}.intersection(fast))
        or (policy.get("deny_max_mode") and type(max_mode) is not bool)
        or (context_enabled and context is None)
    )
    if missing and policy.get("require_model_metadata_for_goal", True):
        reasons.append("model_metadata_unavailable")
    return reasons


def signed_authorization(folder, policy, session, reason, now):
    """Verify a human-issued SSH signature. Missing or invalid grants stay denied."""
    config = policy.get("authorization")
    if not isinstance(config, dict) or reason not in AUTH_LIMITS or not re.fullmatch(r"[\w-]{1,128}", session):
        return False
    try:
        directory = folder / Path(config["directory"]).expanduser()
        allowed = folder / Path(config["allowed_signers"]).expanduser()
        grant = directory / f"{session}.{reason}.json"
        signature = grant.with_suffix(".json.sig")
        if not signature.is_file() or signature.stat().st_size > 8192:
            return False
        with grant.open("rb") as stream:
            document = stream.read(4097)
        if len(document) > 4096:
            return False
        body = json.loads(document)
        issued, expires = body.get("issued_at_unix"), body.get("expires_at_unix")
        ttl = config.get("max_seconds", 3600)
        if not (
            body.get("version") == 1 and body.get("session") == session and body.get("limit") == reason
            and config.get("audience") and body.get("audience") == config["audience"]
            and nonnegative(issued) and nonnegative(expires) and nonnegative(ttl)
            and issued <= now < expires <= issued + ttl
        ):
            return False
        args = [
            "ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", config["principal"],
            "-n", "goal-guard", "-s", str(signature),
        ]
        if config.get("revoked_signers"):
            args.extend(("-r", str(folder / Path(config["revoked_signers"]).expanduser())))
        result = subprocess.run(
            args, input=document, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=.8, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return result.returncode == 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.TimeoutExpired):
        return False


def read_usage(folder, policy, session, now):
    """Read optional externally attributed usage, never account-wide usage deltas."""
    source = policy.get("usage_source")
    if not source:
        return None, None
    try:
        path = folder / Path(os.path.expandvars(source)).expanduser()
        with path.open(encoding="utf-8") as stream:
            row = json.load(stream)["sessions"][session]
        observed_at = row["observed_at_unix"]
        if not nonnegative(observed_at):
            return None, None
        age = now - observed_at
        if not 0 <= age <= policy.get("usage_max_age_seconds", 120):
            return None, None
        tokens = row.get("session_tokens")
        percent = None
        used, total = row.get("session_quota_used"), row.get("plan_quota_total")
        reset_at = row.get("quota_reset_at_unix")
        if (row.get("quota_window") and nonnegative(used) and nonnegative(total) and total > 0
                and nonnegative(reset_at) and now < reset_at):
            measured_percent = used / total * 100
            if nonnegative(measured_percent):
                percent = measured_percent
        return tokens if nonnegative(tokens) else None, percent
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return None, None


def admin_usage_snapshot(pages, *, observed_at, window_start, window_reset,
                         quota_total_cents=None, included_kinds=(), quota_models=()):
    """Normalize complete Cursor Admin API pages without storing account details."""
    if not pages or not 0 <= window_start < observed_at < window_reset:
        raise ValueError("invalid usage window")
    if quota_total_cents is not None and (
        not nonnegative(quota_total_cents) or quota_total_cents == 0 or not included_kinds
    ):
        raise ValueError("a positive quota and the exact included event kinds are required")
    expected_period = pages[0]["period"]
    start, end = expected_period["startDate"], expected_period["endDate"]
    if start > int(window_start * 1000) or end < int(observed_at * 1000):
        raise ValueError("usage export does not cover the configured window")
    expected_count = pages[0]["totalUsageEventsCount"]
    sessions = {}
    seen_events = 0
    for number, page in enumerate(pages, 1):
        pagination = page["pagination"]
        events = page["usageEvents"]
        if (
            page["period"] != expected_period or page["totalUsageEventsCount"] != expected_count
            or pagination["currentPage"] != number or pagination["numPages"] != len(pages)
            or pagination["hasNextPage"] is not (number < len(pages))
            or not isinstance(events, list)
        ):
            raise ValueError("inconsistent usage export pages")
        seen_events += len(events)
        for event in events:
            session = event.get("conversationId")
            at = int(event["timestamp"]) / 1000
            if not isinstance(session, str) or not session or not window_start <= at <= observed_at:
                continue
            totals = sessions.setdefault(session, {"session_tokens": 0, "session_quota_used": 0})
            tokens = measured_tokens(event.get("tokenUsage"))
            if tokens is None:
                totals["session_tokens"] = None
            elif totals["session_tokens"] is not None:
                totals["session_tokens"] += tokens
            if quota_total_cents is not None and event.get("kind") in included_kinds:
                if quota_models:
                    model = event.get("model")
                    if not isinstance(model, str) or not model:
                        totals["session_quota_used"] = None
                        continue
                    if model not in quota_models:
                        continue
                cents = event.get("chargedCents")
                if not nonnegative(cents):
                    totals["session_quota_used"] = None
                elif totals["session_quota_used"] is not None:
                    totals["session_quota_used"] += cents
    if seen_events != expected_count:
        raise ValueError("usage export is incomplete")
    for totals in sessions.values():
        totals.update(observed_at_unix=observed_at)
        if quota_total_cents is not None:
            totals.update(plan_quota_total=quota_total_cents, quota_reset_at_unix=window_reset,
                          quota_window=f"{int(window_start * 1000)}:{int(window_reset * 1000)}")
    return {"sessions": sessions}


def import_admin_usage(arguments):
    parser = argparse.ArgumentParser(description="Import complete Cursor Admin API usage pages.")
    parser.add_argument("pages", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--observed-at", type=float, required=True, help="export cutoff, in Unix UTC seconds")
    parser.add_argument("--window-start", type=float, required=True, help="Unix UTC seconds")
    parser.add_argument("--window-reset", type=float, required=True, help="Unix UTC seconds")
    parser.add_argument("--plan-quota-cents", type=float)
    parser.add_argument("--included-kind", action="append", default=[])
    parser.add_argument("--quota-model", action="append", default=[],
                        help="exact model name for this quota pool, repeat for multiple models")
    opts = parser.parse_args(arguments)
    try:
        pages = [json.loads(path.read_text(encoding="utf-8-sig")) for path in opts.pages]
        snapshot = admin_usage_snapshot(
            pages, observed_at=opts.observed_at, window_start=opts.window_start,
            window_reset=opts.window_reset, quota_total_cents=opts.plan_quota_cents,
            included_kinds=opts.included_kind, quota_models=opts.quota_model,
        )
    except (OSError, KeyError, TypeError, ValueError) as error:
        parser.error(str(error))
    write_snapshot(opts.output, snapshot)


def cli_usage_snapshot(records, observed_at):
    """Aggregate each Cursor CLI stream-json result once, using native IDs."""
    if not nonnegative(observed_at):
        raise ValueError("invalid CLI capture timestamp")
    requests = {}
    for record in records:
        if record.get("type") != "result":
            continue
        session, request = record.get("session_id"), record.get("request_id")
        if not all(isinstance(value, str) and value for value in (session, request)):
            raise ValueError("CLI result lacks a native session or request ID")
        key = session, measured_tokens(record.get("usage"))
        if requests.setdefault(request, key) != key:
            raise ValueError("conflicting CLI results for the same request")
    if not requests:
        raise ValueError("no CLI results found")
    sessions = {}
    for session, tokens in requests.values():
        row = sessions.setdefault(session, {"observed_at_unix": observed_at, "session_tokens": 0})
        if tokens is None:
            row["session_tokens"] = None
        elif row["session_tokens"] is not None:
            row["session_tokens"] += tokens
    return {"sessions": sessions}


def import_cli_usage(arguments):
    parser = argparse.ArgumentParser(description="Import Cursor CLI stream-json results.")
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--observed-at", required=True, type=float, help="capture cutoff, Unix UTC seconds")
    opts = parser.parse_args(arguments)

    def records():
        for path in opts.inputs:
            with path.open(encoding="utf-8-sig") as stream:
                for line in stream:
                    if line.strip():
                        yield json.loads(line)

    try:
        snapshot = cli_usage_snapshot(records(), opts.observed_at)
    except (OSError, TypeError, ValueError, AttributeError) as error:
        parser.error(str(error))
    write_snapshot(opts.output, snapshot)


def write_snapshot(output, snapshot):
    handle = tempfile.NamedTemporaryFile(mode="w", dir=output.parent, encoding="utf-8", delete=False)
    try:
        with handle:
            json.dump(snapshot, handle)
        os.replace(handle.name, output)
    finally:
        Path(handle.name).unlink(missing_ok=True)


def declared_events(folder, config):
    """Identify this package's hooks without executing config commands."""
    if config.get("version") != 1 or not isinstance(config.get("hooks"), dict):
        return set()
    events = set()
    for event in EVENTS:
        for item in parameters(config["hooks"].get(event)):
            try:
                words = shlex.split(item["command"], posix=os.name != "nt")
                interpreter, script = (word.strip("\"'") for word in words)
                location = (folder.parent / script).resolve()
                if location == (folder / "goal_guard.py").resolve() and shutil.which(interpreter):
                    events.add(event)
                    break
            except (KeyError, OSError, ValueError, TypeError):
                continue
    return events


def native_events(folder, conversation, not_before):
    try:
        state = (folder / "goal_guard.local.sqlite").resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(state, uri=True, timeout=.2)) as db:
            by_generation = {}
            for generation, event in db.execute("""
                SELECT generation, event FROM evidence
                WHERE session=:session AND at>=:start AND reason IS NULL
                UNION
                SELECT generation, 'stop' FROM turn_end
                WHERE session=:session AND at>=:start AND status='completed'
                ORDER BY generation, event
            """, {"session": conversation, "start": not_before}):
                by_generation.setdefault(generation, set()).add(event)
            return max(by_generation.values(), key=lambda events: len(events & EVENTS), default=set())
    except (OSError, sqlite3.Error):
        return set()


def doctor(folder, conversation=None, not_before=None):
    config_ok = False
    declared = set()
    try:
        config = json.loads((folder / "hooks.json").read_text(encoding="utf-8-sig"))
        config_ok = isinstance(config.get("goalGuard"), dict) and (folder / "goal_guard.py").is_file()
        declared = declared_events(folder, config)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    with tempfile.TemporaryDirectory() as scratch:
        policy = {"max_session_generations": 1}
        probe = Path(scratch)
        event = {"conversation_id": "doctor-goal", "generation_id": "one"}
        handle(probe, policy, {**event, "hook_event_name": "beforeSubmitPrompt", "prompt": "/goal Probe"})
        denied = handle(probe, policy, {**event, "hook_event_name": "preToolUse"})
        normal = {"conversation_id": "doctor-normal", "generation_id": "one", "hook_event_name": "preToolUse"}
        synthetic_ok = denied.get("permission") == "deny" and handle(
            probe, policy, normal, native_db=probe / "absent.sqlite"
        ) == {}
    native_missing = None
    if conversation is not None and not_before is not None:
        native_missing = sorted(EVENTS - native_events(folder, conversation, not_before))
    return {
        "ok": config_ok and declared == EVENTS and synthetic_ok and not native_missing,
        "config_ok": config_ok,
        "missing_config_events": sorted(EVENTS - declared),
        "synthetic_contract_ok": synthetic_ok,
        "native_canary": "not_requested" if native_missing is None else "missing" if native_missing else "verified",
        "missing_native_events": native_missing,
    }


def run_doctor(folder, argv):
    parser = argparse.ArgumentParser(description="Check the hook separately from its native Cursor canary.")
    parser.add_argument("--conversation")
    parser.add_argument("--not-before", type=float, help="canary start in Unix UTC seconds")
    args = parser.parse_args(argv)
    if (args.conversation is None) != (args.not_before is None):
        parser.error("--conversation and --not-before must be supplied together")
    if args.not_before is not None and not 0 <= args.not_before <= time.time():
        parser.error("--not-before must be a past Unix UTC timestamp")
    result = doctor(folder, args.conversation, args.not_before)
    print(json.dumps(result))
    return 0 if result["ok"] else 1


def claim_alert(db, policy, session, reason, now, *, display):
    window_seconds = policy.get("alert_window_seconds", 900)
    if type(window_seconds) is not int or window_seconds <= 0:
        window_seconds = 900
    key = session, reason, int(now // window_seconds), window_seconds
    db.execute(
        "INSERT OR IGNORE INTO alert(session,reason,window,window_seconds,at) VALUES (?,?,?,?,?)",
        (*key, now),
    )
    if not display:
        return False
    return db.execute(
        "UPDATE alert SET displayed=1 WHERE session=? AND reason=? AND window=? AND window_seconds=? "
        "AND displayed=0", key,
    ).rowcount == 1


def list_alerts(folder, argv):
    parser = argparse.ArgumentParser(description="Read the local alert stream without opening a window.")
    parser.add_argument("--after-id", type=int, default=0)
    args = parser.parse_args(argv)
    if args.after_id < 0:
        parser.error("--after-id must be non-negative")
    file = folder / "goal_guard.local.sqlite"
    if not file.is_file():
        print(json.dumps({"alerts": []}))
        return
    with closing(sqlite3.connect(file.resolve().as_uri() + "?mode=ro", uri=True, timeout=.2)) as db:
        db.row_factory = sqlite3.Row
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='alert'").fetchone()
        rows = db.execute("SELECT rowid AS id,session,reason,at FROM alert WHERE rowid>? ORDER BY rowid LIMIT 250",
                          (args.after_id,)).fetchall() if exists else []
    print(json.dumps({"alerts": [dict(row) for row in rows]}))


def handle(folder, policy, payload, *, native_db=None, now=None):
    event = payload.get("hook_event_name")
    session, generation = payload.get("conversation_id"), payload.get("generation_id")
    if event not in EVENTS or not all(isinstance(v, str) and v for v in (session, generation)):
        return {}
    now = time.time() if now is None else now
    with closing(sqlite3.connect(folder / "goal_guard.local.sqlite", timeout=.25)) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS generation (
                session TEXT, id TEXT, submitted INTEGER, explicit_goal INTEGER,
                marker TEXT, counted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(session, id), UNIQUE(session, marker)
            );
            CREATE TABLE IF NOT EXISTS evidence (
                at REAL, session TEXT, generation TEXT, event TEXT, active_goal INTEGER,
                generations INTEGER, tokens REAL, quota_percent REAL, reason TEXT
            );
            CREATE TABLE IF NOT EXISTS timing (
                session TEXT PRIMARY KEY, human_at REAL, goal_started_at REAL
            );
            CREATE TABLE IF NOT EXISTS turn_end (
                session TEXT, generation TEXT, at REAL, status TEXT,
                PRIMARY KEY(session, generation)
            );
            CREATE TABLE IF NOT EXISTS native_tokens (
                session TEXT, generation TEXT, tokens INTEGER,
                PRIMARY KEY(session, generation)
            );
            CREATE TABLE IF NOT EXISTS alert (
                session TEXT, reason TEXT, window INTEGER, window_seconds INTEGER, at REAL,
                displayed INTEGER NOT NULL DEFAULT 0,
                UNIQUE(session, reason, window, window_seconds)
            );
        """)
        with db:
            db.execute("BEGIN IMMEDIATE")
            native_meter = policy.get("measure_native_session_tokens") is True
            if native_meter and event in {"afterAgentResponse", "stop"}:
                record_native_tokens(db, session, generation, payload)
            if event == "stop":
                status = payload.get("status")
                if status in ("completed", "aborted", "error"):
                    db.execute("INSERT OR IGNORE INTO turn_end VALUES (?, ?, ?, ?)",
                               (session, generation, now, status))
                return {}
            if event == "beforeSubmitPrompt":
                explicit = bool(GOAL_COMMAND.match(str(payload.get("prompt", ""))))
                db.execute("INSERT OR IGNORE INTO generation VALUES (?, ?, 1, ?, NULL, 0)",
                           (session, generation, explicit))
                db.execute("INSERT OR IGNORE INTO timing(session) VALUES (?)", (session,))
            db.execute("INSERT OR IGNORE INTO generation VALUES (?, ?, 0, 0, NULL, 1)",
                       (session, generation))
            submitted, explicit, marker = db.execute(
                "SELECT submitted, explicit_goal, marker FROM generation WHERE session=? AND id=?",
                (session, generation)).fetchone()
            timing = db.execute(
                "SELECT human_at, goal_started_at FROM timing WHERE session=?", (session,)
            ).fetchone()
            human_at, started_at = timing or (None, None)
            if event != "beforeSubmitPrompt":
                db.execute("UPDATE generation SET counted=1 WHERE session=? AND id=?", (session, generation))
                if not submitted and not marker:
                    resume = latest_resume(native_db or cursor_database(), session)
                    if resume and 0 <= now - resume[0] <= 180:
                        updated = db.execute("UPDATE OR IGNORE generation SET marker=? WHERE session=? AND id=?",
                                             (resume[1], session, generation))
                        if updated.rowcount:
                            marker = resume[1]
                            if resume[2] is not None:
                                human_at = resume[2]
                                db.execute(
                                    "INSERT INTO timing(session, human_at) VALUES (?, ?) "
                                    "ON CONFLICT(session) DO UPDATE SET human_at=excluded.human_at",
                                    (session, human_at),
                                )
            active = bool(explicit or marker)
            count = db.execute("SELECT COUNT(*) FROM generation WHERE session=? AND counted=1",
                               (session,)).fetchone()[0]
            tokens, percent = read_usage(folder, policy, session, now) if active else (None, None)
            reasons = []
            if active:
                if native_meter:
                    tokens = db.execute(
                        "SELECT CASE WHEN COUNT(tokens)>0 AND COUNT(*)=COUNT(tokens) "
                        "THEN TOTAL(tokens) END FROM native_tokens WHERE session=?",
                        (session,),
                    ).fetchone()[0]
                idle = now - human_at if human_at is not None else None
                runtime = now - started_at if started_at is not None else None
                if event == "beforeSubmitPrompt":
                    idle = runtime = 0
                for name, value in (("session_generations", count), ("session_tokens", tokens),
                                    ("plan_percent", percent), ("idle_seconds", idle),
                                    ("goal_runtime_seconds", runtime)):
                    limit = policy.get("max_" + name)
                    if nonnegative(value) and nonnegative(limit) and limit > 0 and value >= limit:
                        reasons.append(name)
                if policy.get("require_usage_for_goal", True):
                    for name, value in (("session_tokens", tokens), ("plan_percent", percent)):
                        limit = policy.get("max_" + name)
                        if nonnegative(limit) and limit > 0 and value is None:
                            reasons.append("usage_unavailable")
                            break
                if event != "beforeSubmitPrompt" and any(
                    policy.get(field) for field in ("deny_fast", "deny_max_mode", "max_context_tokens")
                ):
                    settings = model_settings(native_db or cursor_database(), session)
                    reasons.extend(model_limits(policy, settings, payload.get("model_params")))
            reason = next((candidate for candidate in reasons
                           if not signed_authorization(folder, policy, session, candidate, now)), None)
            display = bool(reason and claim_alert(
                db, policy, session, reason, now, display=event in {"preToolUse", "beforeSubmitPrompt"},
            ))
            if event == "beforeSubmitPrompt" and not reason:
                if explicit:
                    db.execute("UPDATE timing SET human_at=?, goal_started_at=? WHERE session=?",
                               (now, now, session))
                else:
                    db.execute("UPDATE timing SET human_at=? WHERE session=?", (now, session))
            db.execute("INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (now, session, generation, event, active, count, tokens, percent, reason))
    if reason:
        message = {"user_message": f"goal_guard_limit:{reason}"} if display else {}
        if event == "preToolUse":
            return {"permission": "deny", **message}
        if event == "beforeSubmitPrompt":
            return {"continue": False, **message}
    return {}


def main():
    if sys.argv[1:] and sys.argv[1] == "import-admin-usage":
        return import_admin_usage(sys.argv[2:])
    if sys.argv[1:] and sys.argv[1] == "import-cli-usage":
        return import_cli_usage(sys.argv[2:])
    folder = Path(__file__).resolve().parent
    if sys.argv[1:2] == ["doctor"]:
        raise SystemExit(run_doctor(folder, sys.argv[2:]))
    if sys.argv[1:2] == ["list-alerts"]:
        return list_alerts(folder, sys.argv[2:])
    policy = json.loads((folder / "hooks.json").read_text(encoding="utf-8-sig"))["goalGuard"]
    native_source = policy.get("native_database")
    native_db = folder / Path(os.path.expandvars(native_source)).expanduser() if native_source else None
    print(json.dumps(handle(folder, policy, json.load(sys.stdin), native_db=native_db)))


if __name__ == "__main__":
    main()
