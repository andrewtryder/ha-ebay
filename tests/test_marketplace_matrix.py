"""Marketplace matrix regression tests (Site ID → marketplace ID)."""

from __future__ import annotations

from custom_components.ebay.marketplace import marketplace_matrix_rows


def test_marketplace_matrix_210_is_ebay_ca_not_cafr() -> None:
    """Canada French (210) must map to EBAY_CA; EBAY_CAFR must not appear."""
    rows = marketplace_matrix_rows()
    assert rows, "marketplace_matrix_rows() returned no rows"
    marketplaces = {row["marketplace"] for row in rows}
    assert "EBAY_CAFR" not in marketplaces

    row_210 = next(row for row in rows if row["site_id"] == "210")
    assert row_210["marketplace"] == "EBAY_CA"
    assert row_210["accept_language"] == "fr-CA"

    for row in rows:
        assert row["marketplace"] != "EBAY_CAFR"
        assert "CAFR" not in row["marketplace"]
