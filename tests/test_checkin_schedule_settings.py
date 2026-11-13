"""Panel-editable randomized check-in schedule settings."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

from src import auth
from src import main as main_module


def _accounts(count: int) -> list[tuple[str, dict]]:
    return [(f"account-{index}", {}) for index in range(count)]


class _JsonRequest:
    """Minimal request double for directly exercising the FastAPI handler."""

    def __init__(self, body: dict):
        self.body = body

    async def json(self):
        return self.body


@contextmanager
def _isolated_schedule_store():
    """Keep persistence and generated plans out of the working tree."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        env_path = root / ".env"
        with (
            patch.object(auth, "ACCOUNTS_PATH", root / "accounts.json"),
            patch.object(auth, "ENV_PATH", env_path),
            patch.object(auth, "_settings", {}),
            patch.object(main_module, "_CHECKIN_PLAN_CACHE", None),
            # The POST handler starts the schedule worker when the saved
            # settings enable it. These tests verify persistence /
            # validation / plan regeneration, not the worker: starting a
            # real task on asyncio.run's throwaway loop would leak it into
            # the module-global _CHECKIN_SCHEDULE_TASK and break later
            # TestClient lifespans (foreign-loop task at shutdown gather).
            patch.object(main_module, "_ensure_checkin_schedule_loop", lambda: None),
            patch.dict(
                os.environ,
                {"TRAE_CHECKIN_PLAN_FILE": str(root / "checkin_plan.json")},
                clear=False,
            ),
        ):
            try:
                yield root
            finally:
                task = main_module._CHECKIN_SCHEDULE_TASK
                if task is not None:
                    if not task.done():
                        try:
                            task.cancel()
                        except RuntimeError:
                            pass
                    main_module._CHECKIN_SCHEDULE_TASK = None


class CheckinScheduleSettingsApiTests(unittest.TestCase):
    def _post(self, **overrides):
        body = {
            "enabled": True,
            "window_start": "06:00",
            "window_minutes": 120,
            "min_gap_minutes": 10,
        }
        body.update(overrides)
        response = asyncio.run(main_module.api_set_checkin_schedule_settings(_JsonRequest(body)))
        return response.status_code, json.loads(response.body)

    def test_post_validation_rejects_invalid_window_and_gap_without_saving(self):
        invalid_bodies = (
            {"window_minutes": 0},
            {"window_start": "23:00", "window_minutes": 61},
            {"window_start": "morning"},
            {"min_gap_minutes": -1},
        )
        with _isolated_schedule_store(), patch.object(main_module, "API_KEYS", []):
            previous = {
                "enabled": False,
                "window_start": "06:00",
                "window_minutes": 900,
                "min_gap_minutes": 10,
            }
            auth._settings["checkin_schedule"] = dict(previous)
            for overrides in invalid_bodies:
                with self.subTest(overrides=overrides):
                    status, data = self._post(**overrides)
                    self.assertEqual(status, 400)
                    self.assertFalse(data["success"])
                    self.assertEqual(auth._settings["checkin_schedule"], previous)

    def test_gap_must_fit_current_account_count_and_valid_gap_saves(self):
        with (
            _isolated_schedule_store(),
            patch.object(main_module, "API_KEYS", []),
            patch.object(auth, "get_accounts_raw", return_value=_accounts(10)),
        ):
            previous = {
                "enabled": False,
                "window_start": "06:00",
                "window_minutes": 900,
                "min_gap_minutes": 10,
            }
            auth._settings["checkin_schedule"] = dict(previous)
            status, too_large = self._post(min_gap_minutes=15)
            self.assertEqual(status, 400)
            self.assertIn("maximum allowed value", too_large["error"])
            self.assertEqual(auth._settings["checkin_schedule"], previous)

            status, data = self._post(min_gap_minutes=13)
            self.assertEqual(status, 200)
            self.assertTrue(data["success"])
            self.assertEqual(data["min_gap_minutes"], 13)
            self.assertEqual(data["max_gap_minutes"], 13)
            self.assertEqual(auth._settings["checkin_schedule"]["min_gap_minutes"], 13)

    def test_get_settings_derives_end_and_reports_minute_values(self):
        with (
            _isolated_schedule_store(),
            patch.object(main_module, "API_KEYS", []),
            patch.object(auth, "get_accounts_raw", return_value=_accounts(10)),
        ):
            auth._settings["checkin_schedule"] = {
                "enabled": True,
                "window_start": "06:00",
                "window_minutes": 120,
                "min_gap_minutes": 10,
            }
            response = asyncio.run(main_module.api_get_checkin_schedule_settings())
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["window_start"], "06:00")
        self.assertEqual(data["window_minutes"], 120)
        self.assertEqual(data["window_end"], "08:00")
        self.assertEqual(data["min_gap_minutes"], 10)
        self.assertEqual(data["max_gap_minutes"], 13)
        self.assertEqual(data["effective_gap_minutes"], 10)

    def test_settings_change_regenerates_todays_plan(self):
        with (
            _isolated_schedule_store() as root,
            patch.object(main_module, "API_KEYS", []),
            patch.object(auth, "get_accounts_raw", return_value=_accounts(2)),
        ):
            status, _data = self._post(
                window_start="10:00",
                window_minutes=120,
                min_gap_minutes=5,
            )
            self.assertEqual(status, 200)
            plan = main_module._load_checkin_plan()
            self.assertIsNotNone(plan)
            self.assertEqual(plan["window_start"], "10:00")
            self.assertEqual(plan["window_end"], "12:00")
            self.assertEqual(plan["min_gap_seconds"], 300)
            self.assertTrue((root / "checkin_plan.json").exists())

    def test_post_requires_management_key(self):
        with _isolated_schedule_store(), patch.object(main_module, "API_KEYS", ["panel-key"]):
            request = Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/checkin/schedule/settings",
                    "headers": [],
                }
            )

            async def downstream(_request):
                return main_module.JSONResponse({"success": True})

            response = asyncio.run(main_module.auth_middleware(request, downstream))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(json.loads(response.body)["error"]["message"], "Invalid API key")


class CheckinScheduleSettingsMathTests(unittest.TestCase):
    def test_auth_settings_fall_back_to_env_and_persist_derived_end(self):
        with (
            _isolated_schedule_store() as root,
            patch.dict(
                os.environ,
                {
                    "TRAE_CHECKIN_SCHEDULE_ENABLED": "1",
                    "TRAE_CHECKIN_WINDOW_START": "07:00",
                    "TRAE_CHECKIN_WINDOW_END": "09:30",
                    "TRAE_CHECKIN_MIN_GAP_SECONDS": "90",
                },
                clear=False,
            ),
        ):
            self.assertEqual(
                auth.get_checkin_schedule_settings(),
                {
                    "enabled": True,
                    "window_start": "07:00",
                    "window_minutes": 150,
                    "min_gap_minutes": 1.5,
                    "source": "env",
                },
            )
            saved = auth.set_checkin_schedule_settings(True, "22:00", 120, 1.5)
            self.assertEqual(saved["source"], "console")
            self.assertEqual(saved["window_start"], "22:00")
            self.assertEqual(saved["window_minutes"], 120)
            self.assertEqual(os.environ["TRAE_CHECKIN_WINDOW_END"], "24:00")
            self.assertEqual(os.environ["TRAE_CHECKIN_MIN_GAP_SECONDS"], "90")
            self.assertIn("TRAE_CHECKIN_WINDOW_END=24:00", (root / ".env").read_text("utf-8"))

    def test_maximum_feasible_gap_and_safety_net_compression(self):
        self.assertEqual(main_module._checkin_max_feasible_gap_seconds(10, 120), 800)
        self.assertIsNone(main_module._checkin_max_feasible_gap_seconds(1, 120))
        with patch.object(
            main_module,
            "_effective_checkin_schedule_settings",
            return_value={
                "enabled": True,
                "window_start": "06:00",
                "window_minutes": 900,
                "min_gap_minutes": 7000 / 60,
                "source": "console",
            },
        ):
            plan = main_module._build_checkin_plan(
                [f"account-{index}" for index in range(10)],
                datetime(2026, 11, 13, 12, tzinfo=main_module._CHECKIN_TIMEZONE),
            )
        self.assertEqual(plan["min_gap_seconds"], 7000)
        self.assertEqual(plan["effective_gap_seconds"], 6000)


if __name__ == "__main__":
    unittest.main()
