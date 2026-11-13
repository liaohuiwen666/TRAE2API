"""Focused regression coverage for the 1.3.0 security hardening path."""

from __future__ import annotations

import asyncio
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch

from src import auth, main, trae_security, trae_tls


class _CurlTransportHandler(BaseHTTPRequestHandler):
    body = b"first chunk\nsecond chunk\n"
    requests: list[str] = []

    def log_message(self, *_args):
        pass

    def do_GET(self):
        type(self).requests.append(self.path)
        self.send_response(207)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("X-Curl-Transport", "local-server")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body[:12])
        self.wfile.flush()
        self.wfile.write(self.body[12:])


class TlsImpersonationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _CurlTransportHandler.requests.clear()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _CurlTransportHandler)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/stream"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_shared_sync_transport_keeps_chrome_profile_and_proxy(self):
        client = trae_tls.upstream_sync_client(
            proxy="http://127.0.0.1:8080", timeout=5
        )
        try:
            self.assertIsInstance(client, trae_tls.CurlSyncClient)
            self.assertEqual(client.impersonate, "chrome")
            self.assertEqual(client.proxy, "http://127.0.0.1:8080")
            self.assertIn("curl_cffi", type(client._session).__module__)
        finally:
            client.close()

    def test_real_curl_transports_stream_and_close_cleanly(self):
        async def exercise_async_transport():
            async with trae_tls.upstream_async_client(timeout=5) as client:
                self.assertIsInstance(client, trae_tls.CurlAsyncClient)
                async with client.stream("GET", self.url) as response:
                    self.assertIsInstance(response, trae_tls.CurlAsyncResponse)
                    self.assertEqual(response.status_code, 207)
                    self.assertEqual(response.headers["x-curl-transport"], "local-server")
                    chunks = [chunk async for chunk in response.aiter_raw()]
                    self.assertTrue(chunks)
                    self.assertEqual(b"".join(chunks), _CurlTransportHandler.body)

        asyncio.run(exercise_async_transport())

        client = trae_tls.upstream_sync_client(timeout=5)
        try:
            self.assertIsInstance(client, trae_tls.CurlSyncClient)
            request = client.build_request("GET", self.url)
            response = client.send(request, stream=True)
            self.assertEqual(response.status_code, 207)
            self.assertEqual(response.read(), _CurlTransportHandler.body)
            response.close()

            streamed = client.send(client.build_request("GET", self.url), stream=True)
            self.assertEqual(b"".join(streamed.iter_bytes()), _CurlTransportHandler.body)
            streamed.close()
        finally:
            client.close()

        self.assertGreaterEqual(_CurlTransportHandler.requests.count("/stream"), 3)


class FailureClassificationTests(unittest.TestCase):
    def test_tiered_failure_classes_and_client_4xx_behavior(self):
        self.assertEqual(
            trae_security.classify_upstream_failure("quota exhausted"),
            trae_security.FAILURE_QUOTA_EXHAUSTED,
        )
        self.assertEqual(
            trae_security.classify_upstream_failure("too many requests", status_code=429),
            trae_security.FAILURE_RATE_LIMIT,
        )
        self.assertEqual(
            trae_security.classify_upstream_failure("invalid token", status_code=401),
            trae_security.FAILURE_AUTH_DEAD,
        )
        self.assertEqual(
            trae_security.classify_upstream_failure("network timed out"),
            trae_security.FAILURE_TRANSIENT,
        )
        self.assertIsNone(
            trae_security.classify_upstream_failure("bad request", status_code=400)
        )
        self.assertGreater(
            trae_security.cooldown_seconds(trae_security.FAILURE_QUOTA_EXHAUSTED),
            trae_security.cooldown_seconds(trae_security.FAILURE_RATE_LIMIT),
        )
        self.assertEqual(
            trae_security.cooldown_seconds(
                trae_security.FAILURE_RATE_LIMIT, retry_after="123"
            ),
            123.0,
        )

    def test_client_400_never_parks_chat_account(self):
        with patch.object(main, "_CHAT_ACCOUNT_COOLDOWNS", {}):
            self.assertIsNone(main._record_chat_failure("account-a", "token", "HTTP 400"))
            self.assertEqual(main._CHAT_ACCOUNT_COOLDOWNS, {})


class ChatPacingTests(unittest.TestCase):
    def test_same_account_starts_are_spaced_and_jitter_varies(self):
        async def run():
            with (
                patch.object(main, "_CHAT_PACING_LOCKS", {}),
                patch.object(main, "_CHAT_PACING_LAST_STARTED", {}),
                patch.object(main, "_CHAT_PACING_MULTIPLIER", {}),
                patch.object(main.secrets, "randbelow", side_effect=[0, 120]),
            ):
                starts = []
                await main._pace_chat_request("account-a")
                starts.append(time.monotonic())
                await main._pace_chat_request("account-a")
                starts.append(time.monotonic())
                await main._pace_chat_request("account-a")
                starts.append(time.monotonic())
                return starts

        starts = asyncio.run(run())
        first_gap = starts[1] - starts[0]
        second_gap = starts[2] - starts[1]
        self.assertGreaterEqual(first_gap, 0.38)
        self.assertGreater(second_gap, first_gap + 0.07)

    def test_rate_limit_increases_following_gap_multiplier(self):
        with (
            patch.object(main, "_CHAT_ACCOUNT_COOLDOWNS", {}),
            patch.object(main, "_CHAT_PACING_MULTIPLIER", {}),
        ):
            main._record_chat_failure("account-a", "token", "HTTP 429 too many requests")
            self.assertGreater(main._CHAT_PACING_MULTIPLIER["account-a"], 1.0)


def _model_token(account_id: str) -> str:
    return f"header.{account_id}{'p' * 140}.signature"


def _model_account(account_id: str, remaining: float) -> dict:
    return {
        "user_id": account_id,
        "token": _model_token(account_id),
        "source": "web-login",
        "expired_at": "2099-01-01T00:00:00Z",
        "provider_specific": {},
        "checkin": {"account_credits": {"remaining": remaining}},
    }


class StickyLeaseValidationTests(unittest.TestCase):
    def setUp(self):
        self.accounts = {
            "account-a": _model_account("account-a", 10),
            "account-b": _model_account("account-b", 9),
        }
        self.patches = [
            patch.object(auth, "_accounts", self.accounts),
            patch.object(auth, "_active_account", "account-a"),
            patch.object(auth, "_poll_enabled", False),
            patch.object(auth, "_polling_mode", "credit-priority"),
            patch.object(auth, "_MODEL_ACCOUNT_INFLIGHT", {}),
            patch.object(auth, "_MODEL_ACCOUNT_RESERVED", {}),
            patch.object(auth, "_MODEL_ACCOUNT_RESERVATION_OWNERS", {}),
            patch.object(auth, "_save_accounts"),
            patch.object(main, "UPSTREAM_MODE", "raw"),
            patch.object(main, "_CHAT_ACCOUNT_COOLDOWNS", {}),
        ]
        for item in self.patches:
            item.start()
        main._UPSTREAM_SESSION_LEASES.clear()
        main._CHAT_HISTORY_SESSIONS.clear()

    def tearDown(self):
        main._UPSTREAM_SESSION_LEASES.clear()
        main._CHAT_HISTORY_SESSIONS.clear()
        for item in reversed(self.patches):
            item.stop()

    def _new_lease(self):
        bound = main._bind_chat_session(
            [{"role": "user", "content": "first"}],
            {},
            requested_session_id="sticky",
            requested_model="auto",
        )
        # Binding reserves for the pending transport. Simulate that transport
        # completing before the next client turn without changing the lease.
        auth.cancel_account_slot_reservation(
            bound["_account_slot_reservation_account"],
            bound["_account_slot_reservation_id"],
        )
        return bound

    def _next_turn(self):
        return main._bind_chat_session(
            [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "answer"},
                {"role": "user", "content": "next"},
            ],
            {},
            requested_session_id="sticky",
            requested_model="auto",
        )

    def test_disabled_exhausted_or_full_sticky_account_rebinds(self):
        for state in ("disabled", "exhausted", "full"):
            with self.subTest(state=state):
                self._new_lease()
                if state == "disabled":
                    self.accounts["account-a"]["model_enabled"] = False
                elif state == "exhausted":
                    self.accounts["account-a"]["checkin"]["account_credits"]["remaining"] = 0
                else:
                    self.accounts["account-a"]["model_parallel_limit"] = 1
                    self.assertTrue(auth.try_reserve_account_slot("account-a"))
                rebound = self._next_turn()
                self.assertEqual(rebound["_account_id"], "account-b")
                auth.cancel_account_slot_reservation(
                    rebound["_account_slot_reservation_account"],
                    rebound["_account_slot_reservation_id"],
                )
                auth.release_account_slot("account-a")
                self.accounts["account-a"].pop("model_enabled", None)
                self.accounts["account-a"].pop("model_parallel_limit", None)
                self.accounts["account-a"]["checkin"]["account_credits"]["remaining"] = 10
                main._UPSTREAM_SESSION_LEASES.clear()

    def test_healthy_lease_is_reused_without_migration(self):
        first = self._new_lease()
        next_turn = self._next_turn()
        self.assertEqual(first["_account_id"], "account-a")
        self.assertEqual(next_turn["_account_id"], "account-a")
