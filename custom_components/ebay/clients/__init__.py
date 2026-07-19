"""Per-API HTTP client mixins extracted from ``api.py``.

Section metadata lives in ``sections.SECTION_SPECS``. Fetch orchestration
(``async_fetch_sections``) lives in ``section_fetch.SectionFetchMixin``;
``api.EbayApiClient`` composes the mixins and exposes ``async_fetch_data``.
"""
