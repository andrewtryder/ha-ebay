"""Per-API HTTP client mixins extracted from ``api.py``.

Section metadata lives in ``sections.SECTION_SPECS``. Fetch orchestration
(``async_fetch_data`` / ``async_fetch_sections``) remains on ``EbayApiClient``
in ``api.py``; mixins provide the per-API fetchers.
"""
