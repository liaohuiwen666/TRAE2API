"""Per-account upstream egress proxy (feature two).

Covers proxy validation/masking, persistence semantics, immediate-effect
httpx kwargs resolution, the panel API endpoint, and that direct (unset)
accounts keep the original no-proxy client construction.
"""

import tempfile
import unittest
import asyncio
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import auth, main as main_module, trae_client


class ProxyValidationTests(unittest.TestCase):
    def test_valid_schemes(self):
        for value in (
            "http://127.0.0.1:8080",
            "https://proxy.example.com:8443",
            "http://user:pass@proxy.local:3128",
            "socks5://127.0.0.1:1080",
            "socks5h://user:pw@10.0.0.1:1080",
        ):
            self.assertEqual(auth.validate_account_proxy(value), value)

    def test_empty_means_direct(self):
        self.assertEqual(auth.validate_account_proxy(""), "")
        self.assertEqual(auth.validate_account_proxy(None), "")
        self.assertEqual(auth.validate_account_proxy("   "), "")

    def test_invalid_rejected(self):
        for value in (
            "ftp://proxy:21",
            "not-a-url",
            "http://",
            "http://host",
            "http://host:99999",
            "http://host:notaport",
            "socks4://host:1080",
        ):
            with self.assertRaises(ValueError, msg=value):
                auth.validate_account_proxy(value)

    def test_masking_hides_credentials(self):
        self.assertEqual(
            auth.mask_account_proxy("http://alice:secret@proxy.local:3128"),
            "http://***@proxy.local:3128",
        )
        self.assertEqual(
            auth.mask_account_proxy("socks5://10.0.0.1:1080"),
            "socks5://10.0.0.1:1080",
        )
        self.assertNotIn("secret", auth.mask_account_proxy("http://a:secret@h:1"))
        self.assertEqual(auth.mask_account_proxy(""), "")


class ProxyStoreTests(unittest.TestCase):
    def setUp(self):
        self._accounts = dict(auth._accounts)
        auth._accounts.clear()
        auth._accounts.update(
            {
                "acc-1": {"token": "t1", "user_id": "u1"},
                "acc-2": {"token": "t2", "user_id": "u2", "proxy": "socks5://old:1080"},
            }
        )

    def tearDown(self):
        auth._accounts.clear()
        auth._accounts.update(self._accounts)

    def test_set_and_get(self):
        with patch.object(auth, "_save_accounts"):
            self.assertTrue(auth.set_account_proxy("acc-1", "http://p:8080"))
        self.assertEqual(auth.get_account_proxy("acc-1"), "http://p:8080")
        self.assertEqual(auth._accounts["acc-1"]["proxy"], "http://p:8080")

    def test_clear_restores_direct(self):
        with patch.object(auth, "_save_accounts"):
            self.assertTrue(auth.set_account_proxy("acc-2", ""))
        self.assertNotIn("proxy", auth._accounts["acc-2"])
        self.assertEqual(auth.get_account_proxy("acc-2"), "")
        self.assertEqual(auth.proxy_httpx_kwargs("acc-2"), {})

    def test_unknown_account_returns_false(self):
        with patch.object(auth, "_save_accounts"):
            self.assertFalse(auth.set_account_proxy("missing", "http://p:1"))

    def test_invalid_raises_and_keeps_old_value(self):
        with self.assertRaises(ValueError):
            auth.set_account_proxy("acc-2", "ftp://bad")
        self.assertEqual(auth._accounts["acc-2"]["proxy"], "socks5://old:1080")

    def test_kwargs_shape(self):
        self.assertEqual(auth.proxy_httpx_kwargs("acc-2"), {"proxy": "socks5://old:1080"})
        self.assertEqual(auth.proxy_httpx_kwargs("acc-1"), {})
        with self.assertRaises(auth.AccountProxyConfigurationError):
            auth.proxy_httpx_kwargs("")

    def test_corrupt_stored_proxy_fails_closed(self):
        auth._accounts["acc-1"]["proxy"] = "ftp://broken"
        with self.assertRaises(auth.AccountProxyConfigurationError):
            auth.get_account_proxy("acc-1")
        with self.assertRaises(auth.AccountProxyConfigurationError):
            auth.proxy_httpx_kwargs("acc-1")

    def test_persisted_in_accounts_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.json"
            with patch.object(auth, "ACCOUNTS_PATH", path):
                auth.set_account_proxy("acc-1", "http://u:p@proxy:8080")
                import json

                data = json.loads(path.read_text("utf-8"))
                self.assertEqual(data["accounts"]["acc-1"]["proxy"], "http://u:p@proxy:8080")

    def test_list_accounts_masks_proxy(self):
        with patch.object(auth, "_save_accounts"):
            auth.set_account_proxy("acc-1", "http://alice:secret@proxy:8080")
        rows = {a["id"]: a for a in auth.list_accounts()}
        self.assertEqual(rows["acc-1"]["proxy"], "http://***@proxy:8080")
        self.assertTrue(rows["acc-1"]["has_proxy"])
        self.assertFalse(rows["acc-2"]["has_proxy"] is False)  # acc-2 has one
        self.assertNotIn("secret", str(rows))

    def test_malformed_stored_proxy_does_not_break_listing(self):
        auth._accounts["acc-1"]["proxy"] = "http://host:notaport"
        rows = {row["id"]: row for row in auth.list_accounts()}
        self.assertEqual(rows["acc-1"]["proxy"], "invalid")
        self.assertFalse(rows["acc-1"]["has_proxy"])

    def test_accounts_without_proxy_keep_legacy_shape(self):
        rows = {row["id"]: row for row in auth.list_accounts()}
        self.assertNotIn("proxy", rows["acc-1"])
        self.assertNotIn("has_proxy", rows["acc-1"])

    def test_alias_resolves_to_the_configured_account_proxy(self):
        auth._accounts["acc-1"].update(
            {
                "user_id": "stored-user",
                "provider_specific": {"webId": "jwt-user"},
                "proxy": "http://proxy:8080",
            }
        )
        self.assertEqual(
            auth.get_account_proxy("jwt-user"), "http://proxy:8080"
        )

    def test_unresolved_identity_fails_closed_when_proxy_is_configured(self):
        auth._accounts["acc-1"]["proxy"] = "http://proxy:8080"
        with self.assertRaises(auth.AccountProxyConfigurationError):
            auth.get_account_proxy("unknown-user")


class ProxyApiTests(unittest.TestCase):
    def setUp(self):
        self._accounts = dict(auth._accounts)
        auth._accounts.clear()
        auth._accounts.update({"acc-1": {"token": "t1", "user_id": "u1"}})
        self.client = TestClient(main_module.app)

    def tearDown(self):
        auth._accounts.clear()
        auth._accounts.update(self._accounts)

    def test_set_via_api(self):
        with patch.object(auth, "_save_accounts"):
            resp = self.client.post(
                "/api/accounts/proxy",
                json={"account_id": "acc-1", "proxy": "socks5://127.0.0.1:1080"},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["proxy"], "socks5://127.0.0.1:1080")
        self.assertTrue(body["has_proxy"])
        self.assertEqual(auth.get_account_proxy("acc-1"), "socks5://127.0.0.1:1080")

    def test_proxy_mutation_requires_api_key_when_configured(self):
        with patch.object(main_module, "API_KEYS", ["proxy-key"]):
            rejected = self.client.post(
                "/api/accounts/proxy",
                json={"account_id": "acc-1", "proxy": "http://p:8080"},
            )
            with patch.object(auth, "_save_accounts"):
                accepted = self.client.post(
                    "/api/accounts/proxy",
                    json={"account_id": "acc-1", "proxy": "http://p:8080"},
                    headers={"Authorization": "Bearer proxy-key"},
                )
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(accepted.json()["success"])

    def test_clear_via_api(self):
        with patch.object(auth, "_save_accounts"):
            self.client.post(
                "/api/accounts/proxy",
                json={"account_id": "acc-1", "proxy": "http://p:8080"},
            )
            resp = self.client.post(
                "/api/accounts/proxy", json={"account_id": "acc-1", "proxy": ""}
            )
        self.assertTrue(resp.json()["success"])
        self.assertFalse(resp.json()["has_proxy"])
        self.assertEqual(auth.proxy_httpx_kwargs("acc-1"), {})

    def test_invalid_proxy_400(self):
        resp = self.client.post(
            "/api/accounts/proxy", json={"account_id": "acc-1", "proxy": "ftp://x"}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("scheme", resp.json()["error"])

    def test_missing_account_404(self):
        resp = self.client.post(
            "/api/accounts/proxy", json={"account_id": "nope", "proxy": "http://p:1"}
        )
        self.assertEqual(resp.status_code, 404)

    def test_missing_id_400(self):
        resp = self.client.post("/api/accounts/proxy", json={"proxy": "http://p:1"})
        self.assertEqual(resp.status_code, 400)

    def test_credentials_masked_in_api_list(self):
        with patch.object(auth, "_save_accounts"):
            self.client.post(
                "/api/accounts/proxy",
                json={"account_id": "acc-1", "proxy": "http://u:secret@p:8080"},
            )
        resp = self.client.get("/api/accounts")
        self.assertEqual(resp.status_code, 200)
        text = resp.text
        self.assertNotIn("secret", text)
        self.assertIn("http://***@p:8080", text)

    def test_panel_shows_proxy_editor(self):
        resp = self.client.get("/web/login")
        self.assertIn("/api/accounts/proxy", resp.text)
        self.assertIn("toggleAccountProxyEditor", resp.text)


class ProxyEgressTests(unittest.TestCase):
    """The httpx client factory receives the account proxy at creation time."""

    def test_async_client_uses_proxy(self):
        with (
            patch.object(auth, "get_account_record", return_value={"proxy": "http://p:8080"}),
            patch.object(auth, "resolve_model_account_id", return_value="acc-x"),
            patch("httpx.AsyncClient") as factory,
        ):
            kwargs = auth.proxy_httpx_kwargs("acc-x")
            import httpx

            httpx.AsyncClient(timeout=30, **kwargs)
            factory.assert_called_once_with(timeout=30, proxy="http://p:8080")

    def test_direct_client_unchanged(self):
        with (
            patch.object(auth, "get_account_record", return_value={}),
            patch.object(auth, "resolve_model_account_id", return_value="acc-x"),
            patch("httpx.AsyncClient") as factory,
        ):
            kwargs = auth.proxy_httpx_kwargs("acc-x")
            import httpx

            httpx.AsyncClient(timeout=30, **kwargs)
            factory.assert_called_once_with(timeout=30)

    def test_credit_lookup_uses_explicit_stored_account_id_for_proxy(self):
        seen_accounts = []

        class Response:
            status_code = 200
            text = "{}"

            def json(self):
                return {"code": 0}

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, *_args, **_kwargs):
                return Response()

        def proxy_kwargs(account_id):
            seen_accounts.append(account_id)
            return {"proxy": "http://proxy:8080"}

        with (
            patch.object(trae_client.auth, "proxy_httpx_kwargs", side_effect=proxy_kwargs),
            patch.object(trae_client.httpx, "AsyncClient", return_value=Client()),
        ):
            asyncio.run(
                trae_client.fetch_account_credits(
                    "opaque-token", account_id="stored-account"
                )
            )
        self.assertEqual(seen_accounts, ["stored-account"])

    def test_usage_tracker_keeps_proxy_bound_stored_account_id(self):
        """JWT billing identity must not replace the proxy-routing key."""

        with (
            patch.object(main_module, "_account_has_configured_proxy", return_value=True),
            patch.object(main_module, "_account_id_from_token", return_value="jwt-owner"),
        ):
            tracker = main_module._UsageTracker(
                "model",
                "/v1/chat/completions",
                False,
                {
                    "_account_id": "stored-account",
                    "_billing_id": "jwt-owner",
                    "_auth_token": "opaque-token",
                },
            )
        try:
            self.assertEqual(tracker.account_id, "stored-account")
            self.assertEqual(tracker.billing_id, "jwt-owner")
        finally:
            if tracker._credit_snapshot_started:
                main_module._end_credit_snapshot(tracker.account_id)


if __name__ == "__main__":
    unittest.main()
