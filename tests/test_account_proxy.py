"""Per-account upstream egress proxy (feature two).

Covers proxy validation/masking, persistence semantics, immediate-effect
httpx kwargs resolution, the panel API endpoint, and that direct (unset)
accounts keep the original no-proxy client construction.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import auth, main as main_module


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
        self.assertEqual(auth.proxy_httpx_kwargs(""), {})

    def test_corrupt_stored_proxy_falls_back_to_direct(self):
        auth._accounts["acc-1"]["proxy"] = "ftp://broken"
        self.assertEqual(auth.get_account_proxy("acc-1"), "")
        self.assertEqual(auth.proxy_httpx_kwargs("acc-1"), {})

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
            patch("httpx.AsyncClient") as factory,
        ):
            kwargs = auth.proxy_httpx_kwargs("acc-x")
            import httpx

            httpx.AsyncClient(timeout=30, **kwargs)
            factory.assert_called_once_with(timeout=30, proxy="http://p:8080")

    def test_direct_client_unchanged(self):
        with (
            patch.object(auth, "get_account_record", return_value={}),
            patch("httpx.AsyncClient") as factory,
        ):
            kwargs = auth.proxy_httpx_kwargs("acc-x")
            import httpx

            httpx.AsyncClient(timeout=30, **kwargs)
            factory.assert_called_once_with(timeout=30)


if __name__ == "__main__":
    unittest.main()
