import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import _api


class JsonResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            error = RuntimeError(f"HTTP {self.status_code}")
            error.response = self
            raise error


class ApiClientTests(unittest.TestCase):
    def setUp(self):
        _api._CREDENTIALS = {}

    def tearDown(self):
        _api._CREDENTIALS = None

    def test_default_host_is_post_relaunch_v2(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                _api.get_aftermath_host(),
                "https://v2-preview.aftermath.finance",
            )

    def test_configured_host_is_normalized(self):
        with patch.dict(
            os.environ,
            {"AFTERMATH_HOST": " https://example.test/ "},
            clear=True,
        ):
            self.assertEqual(_api.get_aftermath_host(), "https://example.test")

    def test_post_uses_normalized_base_url(self):
        response = JsonResponse({"markets": []})
        with patch.object(_api._requests, "post", return_value=response) as post:
            _api.af_post(
                "/api/perpetuals/all-markets",
                {"collateralCoinType": "coin"},
                host="https://v2-preview.aftermath.finance",
            )
        post.assert_called_once_with(
            "https://v2-preview.aftermath.finance/api/perpetuals/all-markets",
            json={"collateralCoinType": "coin"},
            timeout=30,
        )

    def test_preview_error_payload_raises_on_http_200(self):
        response = JsonResponse({"error": "invalid order"})
        with patch.object(_api._requests, "post", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "invalid order"):
                _api.af_post("/api/perpetuals/account/previews/place-limit-order")


if __name__ == "__main__":
    unittest.main()
