"""Randomized daily check-in schedule: plan generation, persistence, firing."""

from __future__ import annotations

import json
import logging
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from src import auth
from src import main as main_module

TZ = main_module._CHECKIN_TIMEZONE


def _noon() -> datetime:
    return datetime.now(TZ).replace(hour=12, minute=0, second=0, microsecond=0)


class CheckinPlanBuildTests(unittest.TestCase):
    def setUp(self):
        self.ids = [f"acc-{i}" for i in range(10)]

    def test_entries_inside_window_with_min_gap(self):
        with (
            patch.object(main_module, "CHECKIN_WINDOW_START", "06:00"),
            patch.object(main_module, "CHECKIN_WINDOW_END", "21:00"),
            patch.object(main_module, "CHECKIN_MIN_GAP_SECONDS", 600.0),
        ):
            plan = main_module._build_checkin_plan(self.ids, _noon())
        self.assertEqual(plan["date"], _noon().date().isoformat())
        self.assertEqual(len(plan["entries"]), 10)
        self.assertEqual({e["account_id"] for e in plan["entries"]}, set(self.ids))
        start = _noon().replace(hour=6, minute=0).timestamp()
        end = _noon().replace(hour=21, minute=0).timestamp()
        times = sorted(e["at"] for e in plan["entries"])
        for at in times:
            self.assertGreaterEqual(at, start)
            self.assertLessEqual(at, end + 1)
        for prev, nxt in zip(times, times[1:]):
            self.assertGreaterEqual(nxt - prev, 600.0 - 0.001)

    def test_gap_compressed_with_warning_when_window_too_small(self):
        with (
            patch.object(main_module, "CHECKIN_WINDOW_START", "06:00"),
            patch.object(main_module, "CHECKIN_WINDOW_END", "07:00"),
            patch.object(main_module, "CHECKIN_MIN_GAP_SECONDS", 600.0),
        ):
            with self.assertLogs(level="WARNING") as logs:
                plan = main_module._build_checkin_plan(self.ids, _noon())
        self.assertTrue(any("compressed gap" in line for line in logs.output))
        self.assertAlmostEqual(plan["effective_gap_seconds"], 3600 / 9, places=3)
        times = sorted(e["at"] for e in plan["entries"])
        for prev, nxt in zip(times, times[1:]):
            self.assertGreaterEqual(nxt - prev, 3600 / 9 - 0.001)

    def test_randomized_across_runs(self):
        with (
            patch.object(main_module, "CHECKIN_WINDOW_START", "06:00"),
            patch.object(main_module, "CHECKIN_WINDOW_END", "21:00"),
            patch.object(main_module, "CHECKIN_MIN_GAP_SECONDS", 600.0),
        ):
            first = main_module._build_checkin_plan(self.ids, _noon())
            second = main_module._build_checkin_plan(self.ids, _noon())
        self.assertNotEqual(
            [e["account_id"] for e in first["entries"]],
            [e["account_id"] for e in second["entries"]],
        )


class CheckinPlanPersistenceTests(unittest.TestCase):
    def test_today_plan_reused_across_calls_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkin_plan.json"
            with (
                patch.dict("os.environ", {"TRAE_CHECKIN_PLAN_FILE": str(path)}),
                patch.object(main_module, "_CHECKIN_PLAN_CACHE", None),
                patch.object(
                    auth, "get_accounts_raw", return_value=[("a1", {}), ("a2", {})]
                ),
            ):
                plan1 = main_module._checkin_today_plan(_noon())
                saved = json.loads(path.read_text("utf-8"))
                self.assertEqual(saved["date"], _noon().date().isoformat())
                plan2 = main_module._checkin_today_plan(_noon())
                self.assertEqual(plan1["entries"], plan2["entries"])
                # Simulate a container restart: cache cleared, file remains.
                with patch.object(main_module, "_CHECKIN_PLAN_CACHE", None):
                    plan3 = main_module._checkin_today_plan(_noon())
                self.assertEqual(plan1["entries"], plan3["entries"])

    def test_stale_date_regenerated(self):
        yesterday = {"date": "2000-01-01", "entries": [{"account_id": "old", "at": 1}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkin_plan.json"
            path.write_text(json.dumps(yesterday), "utf-8")
            with (
                patch.dict("os.environ", {"TRAE_CHECKIN_PLAN_FILE": str(path)}),
                patch.object(main_module, "_CHECKIN_PLAN_CACHE", None),
                patch.object(auth, "get_accounts_raw", return_value=[("a1", {})]),
            ):
                plan = main_module._checkin_today_plan(_noon())
        self.assertEqual(plan["date"], _noon().date().isoformat())
        self.assertEqual(plan["entries"][0]["account_id"], "a1")


class CheckinScheduleLoopTests(unittest.TestCase):
    def test_due_entries_claimed_once_and_persisted(self):
        now_ts = time.time()
        plan = {
            "date": datetime.now(TZ).date().isoformat(),
            "window_start": "06:00",
            "window_end": "21:00",
            "min_gap_seconds": 600.0,
            "effective_gap_seconds": 600.0,
            "entries": [
                {"account_id": "due", "at": now_ts - 60, "time": "11:00:00"},
                {"account_id": "later", "at": now_ts + 3600, "time": "12:00:00"},
                {"account_id": "done", "at": now_ts - 120, "time": "10:00:00", "done": True},
            ],
        }
        with (
            patch.object(main_module, "_checkin_today_plan", return_value=plan),
            patch.object(main_module, "_checkin_cooldown_remaining", return_value=0),
            patch.object(
                auth, "get_account_record", side_effect=lambda aid: {"checkin": {}}
            ),
            patch.object(
                main_module,
                "_claim_checkin_account",
                new=AsyncMock(return_value={"success": True}),
            ) as claim,
            patch.object(main_module, "_save_checkin_plan") as save,
        ):
            import asyncio

            asyncio.new_event_loop().run_until_complete(
                main_module._checkin_schedule_cycle()
            )
        claim.assert_awaited_once_with("due")
        save.assert_called_once()
        due = next(e for e in plan["entries"] if e["account_id"] == "due")
        later = next(e for e in plan["entries"] if e["account_id"] == "later")
        self.assertTrue(due["done"])
        self.assertEqual(due["result"], "ok")
        self.assertNotIn("done", later)

    def test_cooldown_entry_left_pending(self):
        now_ts = time.time()
        plan = {
            "date": datetime.now(TZ).date().isoformat(),
            "window_start": "06:00",
            "window_end": "21:00",
            "min_gap_seconds": 600.0,
            "effective_gap_seconds": 600.0,
            "entries": [{"account_id": "cool", "at": now_ts - 60, "time": "11:00:00"}],
        }
        with (
            patch.object(main_module, "_checkin_today_plan", return_value=plan),
            patch.object(main_module, "_checkin_cooldown_remaining", return_value=300),
            patch.object(
                main_module, "_claim_checkin_account", new=AsyncMock()
            ) as claim,
            patch.object(main_module, "_save_checkin_plan") as save,
        ):
            import asyncio

            asyncio.new_event_loop().run_until_complete(
                main_module._checkin_schedule_cycle()
            )
        claim.assert_not_awaited()
        save.assert_not_called()
        self.assertNotIn("done", plan["entries"][0])

    def test_already_checked_in_marked_skipped(self):
        now_ts = time.time()
        plan = {
            "date": datetime.now(TZ).date().isoformat(),
            "window_start": "06:00",
            "window_end": "21:00",
            "min_gap_seconds": 600.0,
            "effective_gap_seconds": 600.0,
            "entries": [{"account_id": "a1", "at": now_ts - 60, "time": "11:00:00"}],
        }
        with (
            patch.object(main_module, "_checkin_today_plan", return_value=plan),
            patch.object(main_module, "_checkin_cooldown_remaining", return_value=0),
            patch.object(
                auth,
                "get_account_record",
                return_value={
                    "checkin": {"checked_in": True},
                    "checkin_status_updated_at": time.time(),
                },
            ),
            patch.object(
                main_module, "_claim_checkin_account", new=AsyncMock()
            ) as claim,
            patch.object(main_module, "_save_checkin_plan") as save,
        ):
            import asyncio

            asyncio.new_event_loop().run_until_complete(
                main_module._checkin_schedule_cycle()
            )
        claim.assert_not_awaited()
        save.assert_called_once()
        self.assertEqual(plan["entries"][0]["result"], "skipped")


class CheckinScheduleApiTests(unittest.TestCase):
    def test_schedule_endpoint(self):
        plan = {
            "date": "2026-11-12",
            "window_start": "06:00",
            "window_end": "21:00",
            "min_gap_seconds": 600.0,
            "effective_gap_seconds": 600.0,
            "entries": [
                {"account_id": "a1", "at": 1, "time": "08:00:00", "done": True, "result": "ok"}
            ],
        }
        with (
            patch.object(main_module, "_checkin_today_plan", return_value=plan),
            patch.object(
                auth, "get_accounts_raw", return_value=[("a1", {"label": "主号"})]
            ),
        ):
            client = TestClient(main_module.app)
            resp = client.get("/api/checkin/schedule")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["entries"][0]["label"], "主号")
        self.assertTrue(data["entries"][0]["done"])


if __name__ == "__main__":
    unittest.main()
