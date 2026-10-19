"""The committed OpenAPI document (frontend/openapi.json), which the web app's
types are generated from, matches the API. Regenerate with
`python scripts/export_openapi.py && (cd frontend && npm run gen:api)`."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import export_openapi  # noqa: E402


def test_the_committed_openapi_document_is_current(capsys):
    assert export_openapi.main(["--check"]) == 0, capsys.readouterr().out


def test_the_new_endpoints_are_in_the_contract():
    paths = set(__import__("json").loads(export_openapi.render())["paths"])
    assert {"/events", "/symbols/search", "/tickers/{symbol}/movements/{day}", "/tickers/{symbol}/filings",
            "/tickers/{symbol}/earnings", "/market/pulse", "/watchlists", "/watchlists/{watchlist_id}/digest",
            "/health/live", "/health/ready"} <= paths
    assert "/metrics" not in paths
