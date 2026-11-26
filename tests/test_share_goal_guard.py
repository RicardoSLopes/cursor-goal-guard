import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "share_goal_guard", Path(__file__).resolve().parent.parent / ".cursor" / "goal_guard.py"
)
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)


class ShareGoalGuardTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.native_db = self.folder / "native.sqlite"
        with closing(sqlite3.connect(self.native_db)) as db, db:
            db.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
        self.policy = {"max_session_generations": 2}
        self.now = 1_700_000_000

    def event(self, event, generation, *, session="chat", prompt="", status=None, model_params=None, counters=None):
        self.now += 1
        return GUARD.handle(self.folder, self.policy, {
            "hook_event_name": event, "conversation_id": session, "generation_id": generation,
            "prompt": prompt, "status": status, "model_params": model_params, **(counters or {}),
        }, native_db=self.native_db, now=self.now)

    def bubble(self, name="resume", *, simulated=True, at=None):
        with closing(sqlite3.connect(self.native_db)) as db, db:
            db.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", ("bubbleId:chat:" + name, json.dumps({
                "type": 1, "text": GUARD.GOAL_MARKER, "isSimulatedMsg": simulated,
                "simulatedMsgReason": 31,
                "createdAt": datetime.fromtimestamp(self.now if at is None else at, timezone.utc).isoformat(),
            })))

    def write_usage(self, **metrics):
        (self.folder / "usage.json").write_text(json.dumps({"sessions": {"chat": metrics}}), encoding="utf-8")

    def test_budget_blocks_native_resume_but_not_plain_chat(self):
        self.event("beforeSubmitPrompt", "initial", prompt="/goal work")
        self.assertEqual(self.event("preToolUse", "initial"), {})
        self.event("afterAgentResponse", "initial")
        self.bubble()
        self.assertEqual(self.event("preToolUse", "resumed")["permission"], "deny")
        self.event("beforeSubmitPrompt", "manual", prompt="Run one command")
        self.assertEqual(self.event("preToolUse", "manual"), {})
        for generation in ("one", "two"):
            self.assertEqual(self.event("preToolUse", generation, session="other"), {})

    def test_old_or_quoted_native_marker_does_not_match(self):
        self.policy["max_session_generations"] = 1
        self.bubble("quoted", simulated=False)
        self.assertEqual(self.event("preToolUse", "quoted"), {})
        self.bubble("old")
        self.now += 181
        self.assertEqual(self.event("preToolUse", "old"), {})

    def test_tokens_are_checked_only_when_goal_is_active(self):
        self.policy = {"max_session_tokens": 100, "usage_source": "usage.json"}
        self.write_usage(observed_at_unix=self.now, session_tokens=100)
        self.event("beforeSubmitPrompt", "manual")
        self.assertEqual(self.event("preToolUse", "manual"), {})
        r = self.event("beforeSubmitPrompt", "new-goal", prompt="/goal work")
        self.assertEqual(r, {"continue": False, "user_message": "goal_guard_limit:session_tokens"})

    def test_percent_requires_attribution_and_resets_with_the_window(self):
        self.policy = {"max_plan_percent": 2, "usage_source": "usage.json", "require_usage_for_goal": False}
        self.bubble()
        self.write_usage(observed_at_unix=self.now, session_quota_used=2, plan_quota_total=100,
                         quota_window="example-window", quota_reset_at_unix=self.now + 10)
        self.assertIn("plan_percent", self.event("preToolUse", "resumed")["user_message"])
        self.now += 10
        self.assertEqual(self.event("preToolUse", "resumed"), {})
        self.write_usage(observed_at_unix=self.now, account_quota_used=99, plan_quota_total=100,
                         quota_window="example-window", quota_reset_at_unix=self.now + 10)
        self.assertEqual(self.event("preToolUse", "resumed"), {})

    def test_native_marker_is_not_reused_for_another_generation(self):
        self.policy["max_session_generations"] = 1
        self.bubble()
        self.assertEqual(self.event("preToolUse", "resumed")["permission"], "deny")
        self.assertEqual(self.event("preToolUse", "unrelated"), {})

    def test_preexisting_goal_resumed_after_six_days_blocks_first_tool(self):
        self.policy = {"max_idle_seconds": 3600}
        self.bubble("last-human", simulated=False, at=self.now - 6 * 86400)
        self.bubble()
        decision = self.event("preToolUse", "first-observed-resume")
        self.assertEqual(decision["user_message"], "goal_guard_limit:idle_seconds")
        self.assertEqual(self.event("preToolUse", "ordinary", session="other"), {})

    def test_native_bubbles_across_local_midnight_order_by_utc(self):
        self.policy = {"max_idle_seconds": 3600}
        human = "2026-02-07T23:58:59-03:00"
        resume = "2026-02-07T22:59:00-05:00"
        self.now = int(datetime.fromisoformat(resume).timestamp())
        with closing(sqlite3.connect(self.native_db)) as db, db:
            for name, timestamp, simulated in (("human", human, False), ("resume", resume, True)):
                db.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", ("bubbleId:chat:" + name, json.dumps({
                    "type": 1, "text": GUARD.GOAL_MARKER if simulated else "Work",
                    "isSimulatedMsg": simulated, "simulatedMsgReason": 31 if simulated else None,
                    "createdAt": timestamp,
                })))
        decision = self.event("preToolUse", "first-resume")
        self.assertEqual(decision["user_message"], "goal_guard_limit:idle_seconds")

    def test_long_history_uses_timestamps_instead_of_uuid_order(self):
        self.policy = {"max_idle_seconds": 3600}
        old = datetime.fromtimestamp(self.now - 6 * 86400, timezone.utc).isoformat()
        with closing(sqlite3.connect(self.native_db)) as db, db:
            db.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)", (
                (f"bubbleId:chat:{idx:04}", json.dumps({"type": 2, "createdAt": old}))
                for idx in range(2100)
            ))
        self.bubble("zz-last-human", simulated=False, at=self.now - 6 * 86400)
        self.bubble("0000-resume")
        self.assertEqual(self.event("preToolUse", "resume")["user_message"],
                         "goal_guard_limit:idle_seconds")

    def test_runtime_limit_is_distinct_from_inactivity(self):
        self.policy = {"max_goal_runtime_seconds": 10}
        self.event("beforeSubmitPrompt", "initial", prompt="/goal Work")
        self.assertEqual(self.event("preToolUse", "initial"), {})
        self.now += 10
        self.bubble()
        decision = self.event("preToolUse", "resumed")
        self.assertEqual(decision["user_message"], "goal_guard_limit:goal_runtime_seconds")

    def test_invalid_or_stale_usage_cannot_trigger_token_or_percent_limits(self):
        self.policy = {
            "max_session_tokens": 100, "max_plan_percent": 1,
            "usage_source": "usage.json", "usage_max_age_seconds": 3, "require_usage_for_goal": False,
        }
        self.event("beforeSubmitPrompt", "start", prompt="/goal Work")
        self.assertEqual(self.event("preToolUse", "start"), {})
        self.write_usage(observed_at_unix=self.now - 3, session_tokens=200,
                         session_quota_used=20, plan_quota_total=100,
                         quota_window="window", quota_reset_at_unix=self.now + 100)
        self.assertEqual(self.event("preToolUse", "start"), {})
        (self.folder / "usage.json").write_text("{", encoding="utf-8")
        self.assertEqual(self.event("preToolUse", "start"), {})

    def test_configured_external_budget_requires_fresh_usage_only_for_goal(self):
        self.policy = {"max_plan_percent": 1, "usage_source": "usage.json", "usage_max_age_seconds": 3}
        self.write_usage(observed_at_unix=self.now - 3, session_quota_used=0, plan_quota_total=100,
                         quota_window="window", quota_reset_at_unix=self.now + 100)
        self.event("beforeSubmitPrompt", "manual")
        self.assertEqual(self.event("preToolUse", "manual"), {})
        self.bubble()
        self.assertEqual(self.event("preToolUse", "resume"),
                         {"permission": "deny", "user_message": "goal_guard_limit:usage_unavailable"})

    def test_zero_usage_is_a_valid_measurement_below_threshold(self):
        self.policy = {"max_session_tokens": 1, "max_plan_percent": .5, "usage_source": "usage.json"}
        self.write_usage(observed_at_unix=self.now, session_tokens=0,
                         session_quota_used=0, plan_quota_total=100,
                         quota_window="window", quota_reset_at_unix=self.now + 100)
        self.assertEqual(GUARD.read_usage(self.folder, self.policy, "chat", self.now), (0, 0))
        self.assertEqual(self.event("beforeSubmitPrompt", "start", prompt="/goal Work"), {})

    def test_usage_rejects_backward_clock_and_nonfinite_quota(self):
        self.policy = {"max_plan_percent": .1, "usage_source": "usage.json"}
        sample = {
            "observed_at_unix": self.now, "session_quota_used": 1, "plan_quota_total": 1000,
            "quota_window": "window", "quota_reset_at_unix": self.now + 10,
        }
        self.write_usage(**sample)
        self.assertEqual(GUARD.read_usage(self.folder, self.policy, "chat", self.now), (None, .1))
        self.assertEqual(GUARD.read_usage(self.folder, self.policy, "chat", self.now - 1), (None, None))
        self.assertEqual(GUARD.read_usage(self.folder, self.policy, "chat", self.now + 10), (None, None))
        invalid_samples = ({"quota_reset_at_unix": float("inf")}, {"observed_at_unix": True},
                           {"session_quota_used": 1e300, "plan_quota_total": 1e-100})
        for index, patch in enumerate(invalid_samples):
            with self.subTest(patch=patch):
                self.write_usage(**{**sample, **patch})
                decision = self.event("beforeSubmitPrompt", f"invalid-{index}", prompt="/goal Work")
                self.assertIs(decision["continue"], False)
                self.assertEqual(decision.get("user_message"),
                                 "goal_guard_limit:usage_unavailable" if index == 0 else None)

    def admin_pages(self):
        common = {"timestamp": str(self.now * 1000), "kind": "Included in Business"}
        tokens = dict(zip(GUARD.TOKEN_FIELDS, (20, 10, 40, 30)))
        pages = []
        for number, events in enumerate(([
            {**common, "conversationId": "chat", "chargedCents": 1, "tokenUsage": tokens},
            {**common, "conversationId": "other", "chargedCents": 85},
        ], [
            {**common, "conversationId": "chat", "kind": "Usage-based", "chargedCents": 50, "tokenUsage": tokens},
            {**common, "chargedCents": 100},
        ]), 1):
            pages.append({
                "period": {"startDate": (self.now - 10) * 1000, "endDate": self.now * 1000},
                "totalUsageEventsCount": 4,
                "pagination": {"currentPage": number, "numPages": 2, "hasNextPage": number == 1},
                "usageEvents": events,
            })
        return pages

    def snapshot(self, pages, *, quota_models=()):
        return GUARD.admin_usage_snapshot(
            pages, observed_at=self.now, window_start=self.now - 10, window_reset=self.now + 100,
            quota_total_cents=1000, included_kinds=["Included in Business"], quota_models=quota_models,
        )

    def test_admin_usage_attributes_point_one_percent_to_its_conversation(self):
        result = self.snapshot(self.admin_pages())
        self.assertEqual(result["sessions"]["chat"]["session_tokens"], 200)
        self.assertEqual(result["sessions"]["chat"]["session_quota_used"], 1)
        self.assertEqual(result["sessions"]["other"]["session_quota_used"], 85)
        self.assertIsNone(result["sessions"]["other"]["session_tokens"])
        (self.folder / "usage.json").write_text(json.dumps(result), encoding="utf-8")
        self.policy = {"max_plan_percent": .1, "usage_source": "usage.json"}
        self.assertEqual(self.event("beforeSubmitPrompt", "normal", prompt="Continue"), {})
        self.assertEqual(self.event("beforeSubmitPrompt", "goal", prompt="/goal Work"),
                         {"continue": False, "user_message": "goal_guard_limit:plan_percent"})

    def test_admin_usage_rejects_missing_pages_and_incomplete_window(self):
        pages = self.admin_pages()
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            self.snapshot(pages[:1])
        pages[0]["period"]["startDate"] += 1
        with self.assertRaisesRegex(ValueError, "does not cover"):
            self.snapshot(pages)

    def test_admin_quota_pool_distinguishes_zero_unknown_and_consumed(self):
        pages = self.admin_pages()
        pages[0]["usageEvents"][0]["model"] = "example-included-model"
        pages[0]["usageEvents"][1]["model"] = "example-other-pool-model"
        result = self.snapshot(pages, quota_models=["example-included-model"])
        del pages[0]["usageEvents"][1]["model"]
        missing_model = self.snapshot(pages, quota_models=["example-included-model"])
        self.assertIsNone(missing_model["sessions"]["other"]["session_quota_used"])
        GUARD.write_snapshot(self.folder / "usage.json", result)
        self.policy = {"max_plan_percent": .1, "usage_source": "usage.json"}
        self.assertEqual(self.event("beforeSubmitPrompt", "zero", session="other", prompt="/goal Work"), {})
        unknown = self.event("beforeSubmitPrompt", "unknown", session="missing", prompt="/goal Work")
        self.assertEqual(unknown["user_message"], "goal_guard_limit:usage_unavailable")
        self.assertEqual(self.event("beforeSubmitPrompt", "ordinary", session="missing", prompt="Hello"), {})
        limited = self.event("beforeSubmitPrompt", "limited", prompt="/goal Work")
        self.assertEqual(limited["user_message"], "goal_guard_limit:plan_percent")

    def test_native_stop_receipt_does_not_increment_conversation_usage(self):
        self.event("stop", "aborted", status="aborted")
        self.event("stop", "aborted", status="aborted")
        with closing(sqlite3.connect(self.folder / "goal_guard.local.sqlite")) as db:
            self.assertEqual(db.execute("SELECT session,generation,status FROM turn_end").fetchall(),
                             [("chat", "aborted", "aborted")])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM generation").fetchone()[0], 0)

    def test_native_meter_counts_only_observed_input_and_output_once_per_generation(self):
        self.policy = {"max_session_tokens": 100, "measure_native_session_tokens": True}
        first = {"input_tokens": 70, "output_tokens": 10, "cache_read_tokens": 60}
        final = {**first, "output_tokens": 12}
        self.event("beforeSubmitPrompt", "normal", prompt="Say hello")
        self.event("afterAgentResponse", "normal", counters=first)
        self.event("stop", "normal", status="completed", counters=final)
        self.event("stop", "normal", status="completed", counters=final)
        self.assertEqual(self.event("beforeSubmitPrompt", "goal", prompt="/goal Work"), {})
        self.event("afterAgentResponse", "goal", counters={"input_tokens": 14, "output_tokens": 4})
        self.assertEqual(self.event("preToolUse", "goal")["user_message"], "goal_guard_limit:session_tokens")
        self.assertEqual(self.event("beforeSubmitPrompt", "manual", prompt="Say hello again"), {})
        self.event("stop", "another", session="other", status="completed",
                   counters={"input_tokens": 0, "output_tokens": 0})
        self.assertEqual(self.event("beforeSubmitPrompt", "goal", session="other", prompt="/goal Work"), {})
        unknown = self.event("beforeSubmitPrompt", "goal", session="unseen", prompt="/goal Work")
        self.assertEqual(unknown["user_message"], "goal_guard_limit:usage_unavailable")

    def test_native_meter_rejects_incomplete_counts_without_affecting_chat(self):
        self.policy = {"max_session_tokens": 100, "measure_native_session_tokens": True}
        self.event("afterAgentResponse", "measured", counters={"input_tokens": 1, "output_tokens": 2})
        self.event("stop", "measured", status="completed")
        self.assertEqual(self.event("beforeSubmitPrompt", "first-goal", prompt="/goal Work"), {})
        self.event("afterAgentResponse", "unmeasured", counters={"input_tokens": True, "output_tokens": 2})
        self.assertEqual(self.event("beforeSubmitPrompt", "manual", prompt="Continue"), {})
        reply = self.event("beforeSubmitPrompt", "later-goal", prompt="/goal Work")
        self.assertEqual(reply["user_message"], "goal_guard_limit:usage_unavailable")
        self.event("stop", "unmeasured", status="completed", counters={"input_tokens": 1, "output_tokens": 2})
        self.assertEqual(self.event("beforeSubmitPrompt", "after-recovery", prompt="/goal Work"), {})

    def test_distributed_script_import_cli_writes_only_attributed_metrics(self):
        pages = self.admin_pages()
        paths = []
        for index, page in enumerate(pages):
            path = self.folder / f"input-{index}.json"
            page["usageEvents"][0]["userEmail"] = "sample@example.invalid"
            path.write_text(json.dumps(page), encoding="utf-8")
            paths.append(str(path))
        output = self.folder / "snapshot.json"
        result = subprocess.run([
            sys.executable, "-B", str(SPEC.origin), "import-admin-usage", *paths,
            "--output", str(output), "--observed-at", str(self.now),
            "--window-start", str(self.now - 10), "--window-reset", str(self.now + 100),
            "--plan-quota-cents", "1000", "--included-kind", "Included in Business",
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = output.read_text(encoding="utf-8")
        self.assertNotIn("sample@example.invalid", snapshot)
        self.assertEqual(json.loads(snapshot), self.snapshot(pages))

    def save_model_config(self, **settings):
        with closing(sqlite3.connect(self.native_db)) as db, db:
            db.execute("INSERT OR REPLACE INTO cursorDiskKV VALUES (?, ?)",
                       ("composerData:chat", json.dumps(settings)))

    def test_persisted_max_mode_blocks_goal_even_with_zero_spend(self):
        self.policy = {"deny_max_mode": True, "usage_source": "usage.json"}
        self.write_usage(observed_at_unix=self.now, session_tokens=0)
        self.save_model_config(modelConfig={"maxMode": True})
        self.event("beforeSubmitPrompt", "active", prompt="/goal Work")
        self.assertEqual(self.event("preToolUse", "active")["user_message"], "goal_guard_limit:max_mode")
        self.event("beforeSubmitPrompt", "normal")
        self.assertEqual(self.event("preToolUse", "normal"), {})

    def test_hook_fast_and_context_scoped_to_goal(self):
        self.policy = {"deny_fast": True, "max_context_tokens": 500000}
        self.save_model_config(modelConfig={"maxMode": False}, contextTokenLimit=1000000)
        self.event("beforeSubmitPrompt", "active", prompt="/goal Work")
        fast = {"id": "fast", "value": "true"}
        slow = {"id": "fast", "value": False}
        high = {"id": "context", "value": "500k"}
        safe = {"id": "context", "value": "256k"}
        self.assertEqual(self.event("preToolUse", "active", model_params=[fast, safe])["user_message"],
                         "goal_guard_limit:fast_mode")
        self.assertEqual(self.event("preToolUse", "active", model_params=[slow, high])["user_message"],
                         "goal_guard_limit:context_tokens")
        self.assertEqual(self.event("preToolUse", "active", model_params=[slow, safe]), {})

    def test_unknown_configured_model_parameter_fails_only_for_goal(self):
        self.policy = {"deny_fast": True}
        self.event("beforeSubmitPrompt", "explicit", prompt="/goal Work")
        self.assertEqual(self.event("preToolUse", "explicit")["user_message"],
                         "goal_guard_limit:model_metadata_unavailable")
        self.event("beforeSubmitPrompt", "manual")
        self.assertEqual(self.event("preToolUse", "manual"), {})

    @unittest.skipUnless(shutil.which("ssh-keygen"), "requires OpenSSH")
    def test_real_signature_only_releases_its_limit_session_and_audience(self):
        def ssh(*args):
            run = subprocess.run(["ssh-keygen", *args], capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)

        key = self.folder / "ephemeral"
        ssh("-q", "-t", "ed25519", "-N", "", "-f", str(key))
        public = key.with_suffix(".pub").read_text(encoding="utf-8")
        (self.folder / "allowed").write_text("test-operator " + public, encoding="utf-8")
        grant = self.folder / "chat.session_generations.json"
        grant.write_text(json.dumps({
            "version": 1, "session": "chat", "limit": "session_generations", "audience": "trial",
            "issued_at_unix": self.now, "expires_at_unix": self.now + 30,
        }), encoding="utf-8")
        ssh("-Y", "sign", "-f", str(key), "-n", "goal-guard", str(grant))
        self.policy = {
            "max_session_generations": 1, "max_session_tokens": 100, "usage_source": "usage.json",
            "authorization": {"directory": ".", "allowed_signers": "allowed", "principal": "test-operator",
                              "audience": "trial"},
        }
        self.write_usage(observed_at_unix=self.now, session_tokens=99)
        self.assertEqual(self.event("beforeSubmitPrompt", "explicit", prompt="/goal Work"), {})
        self.write_usage(observed_at_unix=self.now, session_tokens=100)
        self.assertEqual(self.event("preToolUse", "explicit")["user_message"], "goal_guard_limit:session_tokens")
        self.write_usage(observed_at_unix=self.now, session_tokens=99)
        self.assertEqual(self.event("preToolUse", "explicit"), {})
        self.policy["authorization"]["audience"] = "other-trial"
        self.assertFalse(GUARD.signed_authorization(self.folder, self.policy, "chat", "session_generations", self.now))
        self.policy["authorization"]["audience"] = "trial"
        shutil.copyfile(grant, self.folder / "other.session_generations.json")
        signature = grant.with_suffix(".json.sig")
        shutil.copyfile(signature, self.folder / "other.session_generations.json.sig")
        self.assertFalse(GUARD.signed_authorization(self.folder, self.policy, "other", "session_generations", self.now))
        self.assertFalse(GUARD.signed_authorization(self.folder, self.policy, "chat", "session_generations", self.now + 31))
        (self.folder / "revoked").write_text(public, encoding="utf-8")
        self.policy["authorization"]["revoked_signers"] = "revoked"
        self.assertFalse(GUARD.signed_authorization(self.folder, self.policy, "chat", "session_generations", self.now))
        del self.policy["authorization"]["revoked_signers"]
        grant.write_text(grant.read_text(encoding="utf-8") + " ", encoding="utf-8")
        self.assertFalse(GUARD.signed_authorization(self.folder, self.policy, "chat", "session_generations", self.now))

    def test_cli_result_usage_is_scoped_and_replays_are_deduplicated(self):
        tokens = dict(zip(GUARD.TOKEN_FIELDS, (20, 10, 40, 30)))
        first = {"type": "result", "session_id": "chat", "request_id": "one", "usage": tokens}
        second = {**first, "request_id": "two"}
        other = {**first, "session_id": "other", "request_id": "three", "usage": None}
        snap = GUARD.cli_usage_snapshot([first, first, second, other], self.now)
        self.assertEqual(snap["sessions"]["chat"]["session_tokens"], 200)
        self.assertIsNone(snap["sessions"]["other"]["session_tokens"])
        self.assertNotIn("plan_quota_total", snap["sessions"]["chat"])
        with self.assertRaisesRegex(ValueError, "same request"):
            GUARD.cli_usage_snapshot([first, {**first, "session_id": "wrong"}], self.now)

    def test_doctor_distinguishes_config_and_native_canary_failures(self):
        self.folder = self.folder / ".cursor"
        self.folder.mkdir()
        config = self.folder / "hooks.json"
        source_config = Path(SPEC.origin).with_name("hooks.json")
        shutil.copyfile(source_config, config)
        shutil.copyfile(SPEC.origin, self.folder / "goal_guard.py")
        local = GUARD.doctor(self.folder)
        self.assertTrue(local["ok"])
        self.assertEqual(local["native_canary"], "not_requested")
        started = self.now
        for event in ("beforeSubmitPrompt", "preToolUse", "afterAgentResponse", "stop"):
            self.event(event, "canary", status="completed")
        native = GUARD.doctor(self.folder, "chat", started)
        self.assertTrue(native["ok"])
        self.assertEqual(native["native_canary"], "verified")
        old = GUARD.doctor(self.folder, "chat", self.now + 1)
        self.assertFalse(old["ok"])
        self.assertEqual(set(old["missing_native_events"]), GUARD.EVENTS)
        content = json.loads(config.read_text(encoding="utf-8"))
        content["hooks"]["preToolUse"] = [{"command": "python unrelated.py"}]
        config.write_text(json.dumps(content), encoding="utf-8")
        damaged = GUARD.doctor(self.folder, "chat", started)
        self.assertFalse(damaged["ok"])
        self.assertEqual(damaged["missing_config_events"], ["preToolUse"])
        self.assertTrue(damaged["synthetic_contract_ok"])
        self.assertEqual(damaged["native_canary"], "verified")

    def test_native_canary_requires_one_complete_turn(self):
        started = self.now
        self.event("beforeSubmitPrompt", "chat-a")
        self.event("preToolUse", "chat-a")
        self.event("afterAgentResponse", "chat-b")
        self.event("stop", "chat-b", status="completed")
        self.assertNotEqual(GUARD.native_events(self.folder, "chat", started), GUARD.EVENTS)

        self.event("afterAgentResponse", "chat-a")
        self.event("stop", "chat-a", status="aborted")
        self.assertEqual(GUARD.native_events(self.folder, "chat", started), GUARD.EVENTS - {"stop"})

    def test_alert_deduplicates_concurrent_denials_and_preserves_all_blocks(self):
        self.policy = {"max_session_generations": 1, "alert_window_seconds": 60}
        self.event("beforeSubmitPrompt", "goal", prompt="/goal Work")
        self.event("afterAgentResponse", "goal")
        action = {"hook_event_name": "preToolUse", "conversation_id": "chat", "generation_id": "goal"}
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: GUARD.handle(self.folder, self.policy, action, now=self.now), range(16)))
        self.assertTrue(all(row["permission"] == "deny" for row in results))
        self.assertEqual(sum("user_message" in row for row in results), 1)
        self.now += 60
        self.assertIn("user_message", self.event("preToolUse", "goal"))
        self.event("beforeSubmitPrompt", "other", session="other", prompt="/goal Work")
        self.assertIn("user_message", self.event("preToolUse", "other", session="other"))
        with closing(sqlite3.connect(self.folder / "goal_guard.local.sqlite")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM alert").fetchone()[0], 3)
