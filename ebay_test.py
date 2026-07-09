#!/usr/bin/env python3

"""
Read-only eBay My Buying test.

Fetches:
- Watched items
- Items you are bidding on
- Active selling items

Does NOT:
- Place bids
- Buy items
- Sell items
- Modify watchlist
- Call any mutation/write API

Required:
    pip install -r requirements.txt

One-time OAuth setup:
    1. In eBay's production User Tokens page, set Auth Accepted URL to:
       http://localhost:8765/ebay-oauth-callback
    2. Set EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, and EBAY_RUNAME.
    3. Run:
       python3 ebay_test.py --oauth-login
    4. Save the printed EBAY_REFRESH_TOKEN in .env.

Auth options:

Option A: Use a short-lived OAuth user access token:
    export EBAY_ACCESS_TOKEN="v^1.1#..."

Option B: Use refresh-token flow:
    export EBAY_CLIENT_ID="..."
    export EBAY_CLIENT_SECRET="..."
    export EBAY_REFRESH_TOKEN="..."

Optional:
    export EBAY_ENV="production"   # default
    export EBAY_ENV="sandbox"
    export EBAY_SITE_ID="0"        # 0 = US
    export EBAY_SELLING_ENTRIES_PER_PAGE="50"
    export EBAY_CALLBACK_URL="https://example.ngrok.app/ebay-oauth-callback"

Production setup:
    Use the production App ID/Client ID and Cert ID/Client Secret from eBay,
    plus a production user access token or production refresh token.
    The Dev ID is not used by this OAuth Trading API call.
    To fetch selling item views from Sell Analytics, mint your token with:
       https://api.ebay.com/oauth/api_scope/sell.analytics.readonly
"""

from __future__ import annotations

import argparse
import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
import re
import secrets
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
import webbrowser

import requests
from dotenv import load_dotenv

load_dotenv()


EBAY_XML_NS = "urn:ebay:apis:eBLBaseComponents"
NS = {"e": EBAY_XML_NS}

READ_ONLY_CALL_NAME = "GetMyeBayBuying"
SELLING_CALL_NAME = "GetMyeBaySelling"
SELLER_LIST_CALL_NAME = "GetSellerList"
BEST_OFFERS_CALL_NAME = "GetBestOffers"
COMPATIBILITY_LEVEL = "1209"


def get_env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is not None:
        value = value.strip()
    return value if value not in ("", None) else default


def ebay_environment() -> str:
    env = get_env("EBAY_ENV", "production").lower()
    if env not in {"production", "sandbox"}:
        raise ValueError("EBAY_ENV must be either 'production' or 'sandbox'")
    return env


def trading_endpoint() -> str:
    if ebay_environment() == "sandbox":
        return "https://api.sandbox.ebay.com/ws/api.dll"
    return "https://api.ebay.com/ws/api.dll"


def oauth_token_endpoint() -> str:
    if ebay_environment() == "sandbox":
        return "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
    return "https://api.ebay.com/identity/v1/oauth2/token"


def analytics_endpoint() -> str:
    if ebay_environment() == "sandbox":
        return "https://api.sandbox.ebay.com/sell/analytics/v1/traffic_report"
    return "https://api.ebay.com/sell/analytics/v1/traffic_report"


def oauth_authorize_endpoint() -> str:
    if ebay_environment() == "sandbox":
        return "https://auth.sandbox.ebay.com/oauth2/authorize"
    return "https://auth.ebay.com/oauth2/authorize"


def credential_environment(client_id: str | None, client_secret: str | None) -> str | None:
    values = " ".join(value or "" for value in (client_id, client_secret)).upper()
    if "SBX" in values:
        return "sandbox"
    if "PRD" in values:
        return "production"
    return None


def validate_credential_environment(
    client_id: str | None, client_secret: str | None
) -> None:
    env = ebay_environment()
    credential_env = credential_environment(client_id, client_secret)
    if credential_env and credential_env != env:
        raise RuntimeError(
            f"EBAY_ENV is {env!r}, but the configured app credentials look like "
            f"{credential_env!r} credentials. Use matching eBay credentials and "
            "tokens for the selected environment."
        )


def auth_source() -> str:
    if get_env("EBAY_ACCESS_TOKEN"):
        return "EBAY_ACCESS_TOKEN"
    return "refresh token"


def ebay_client_credentials() -> tuple[str, str]:
    client_id = get_env("EBAY_CLIENT_ID") or get_env("EBAY_APP_ID")
    client_secret = get_env("EBAY_CLIENT_SECRET") or get_env("EBAY_CERT_ID")

    missing = [
        name
        for name, value in {
            "EBAY_CLIENT_ID": client_id,
            "EBAY_CLIENT_SECRET": client_secret,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing required OAuth config: {', '.join(missing)}")

    validate_credential_environment(client_id, client_secret)
    return client_id, client_secret


def basic_auth_header(client_id: str, client_secret: str) -> str:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return f"Basic {basic}"


def refresh_access_token() -> str:
    client_id, client_secret = ebay_client_credentials()
    refresh_token = get_env("EBAY_REFRESH_TOKEN")

    if not refresh_token:
        raise RuntimeError(
            "No EBAY_ACCESS_TOKEN found, and refresh-token auth is incomplete. "
            "Missing: EBAY_REFRESH_TOKEN"
        )

    response = requests.post(
        oauth_token_endpoint(),
        headers={
            "Authorization": basic_auth_header(client_id, client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            # Deliberately omit scope.
            # eBay allows refresh requests to omit scope and default to the
            # original consent request's scopes.
        },
        timeout=30,
    )
    response.raise_for_status()

    payload = response.json()
    access_token = payload.get("access_token")
    if not access_token:
        raise RuntimeError(f"No access_token in OAuth response: {payload}")

    return access_token


def oauth_scope() -> str:
    return get_env(
        "EBAY_OAUTH_SCOPE",
        "https://api.ebay.com/oauth/api_scope "
        "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly",
    )


def build_consent_url(state: str) -> str:
    client_id, _ = ebay_client_credentials()
    runame = get_env("EBAY_RUNAME")
    if not runame:
        raise RuntimeError(
            "Missing EBAY_RUNAME. Copy the OAuth-enabled RuName from eBay's "
            "production User Tokens page."
        )

    return (
        f"{oauth_authorize_endpoint()}?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": runame,
                "response_type": "code",
                "scope": oauth_scope(),
                "state": state,
            }
        )
    )


def exchange_authorization_code(code: str) -> dict[str, Any]:
    client_id, client_secret = ebay_client_credentials()
    runame = get_env("EBAY_RUNAME")
    if not runame:
        raise RuntimeError("Missing EBAY_RUNAME")

    response = requests.post(
        oauth_token_endpoint(),
        headers={
            "Authorization": basic_auth_header(client_id, client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": runame,
        },
        timeout=30,
    )

    if response.status_code >= 400:
        print(response.text)
        response.raise_for_status()

    payload: dict[str, Any] = response.json()
    if not payload.get("access_token") or not payload.get("refresh_token"):
        raise RuntimeError(f"OAuth response did not include both tokens: {payload}")
    return payload


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        expected_path = self.server.callback_path  # type: ignore[attr-defined]
        if parsed.path != expected_path:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return

        query = parse_qs(parsed.query)
        self.server.oauth_code = query.get("code", [None])[0]  # type: ignore[attr-defined]
        self.server.oauth_state = query.get("state", [None])[0]  # type: ignore[attr-defined]
        self.server.oauth_error = query.get("error", [None])[0]  # type: ignore[attr-defined]

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<h1>eBay OAuth received</h1>"
            b"<p>You can close this browser tab and return to the terminal.</p>"
        )

    def log_message(self, format: str, *args: Any) -> None:
        return


def run_oauth_login(open_browser: bool) -> int:
    host = get_env("EBAY_CALLBACK_HOST", "localhost")
    port = int(get_env("EBAY_CALLBACK_PORT", "8765"))
    callback_path = get_env("EBAY_CALLBACK_PATH", "/ebay-oauth-callback")
    callback_url = get_env("EBAY_CALLBACK_URL", f"http://{host}:{port}{callback_path}")
    expected_state = secrets.token_urlsafe(24)
    consent_url = build_consent_url(expected_state)

    server = HTTPServer((host, port), OAuthCallbackHandler)
    server.callback_path = callback_path  # type: ignore[attr-defined]
    server.oauth_code = None  # type: ignore[attr-defined]
    server.oauth_state = None  # type: ignore[attr-defined]
    server.oauth_error = None  # type: ignore[attr-defined]

    print(f"Environment:       {ebay_environment()}")
    print(f"Callback URL:      {callback_url}")
    print(f"OAuth token URL:   {oauth_token_endpoint()}")
    print("\nIn eBay Developer, set your Auth Accepted URL to the Callback URL above.")
    print("Then approve the consent page that opens.\n")
    print(f"Consent URL:\n{consent_url}\n")

    if open_browser:
        webbrowser.open(consent_url)

    print("Waiting for eBay redirect...")
    server.handle_request()
    server.server_close()

    if server.oauth_error:  # type: ignore[attr-defined]
        raise RuntimeError(f"eBay OAuth error: {server.oauth_error}")  # type: ignore[attr-defined]
    if server.oauth_state != expected_state:  # type: ignore[attr-defined]
        raise RuntimeError("OAuth state mismatch. Refusing to exchange the code.")
    if not server.oauth_code:  # type: ignore[attr-defined]
        raise RuntimeError("No OAuth code received from eBay.")

    payload = exchange_authorization_code(server.oauth_code)  # type: ignore[attr-defined]

    print("\nOAuth success. Add this to .env:\n")
    print(f"EBAY_REFRESH_TOKEN={payload['refresh_token']}")
    print("\nOptional short-lived access token:")
    print(f"EBAY_ACCESS_TOKEN={payload['access_token']}")
    print(f"\nAccess token expires in {payload.get('expires_in', 'unknown')} seconds.")
    print(
        "Keep EBAY_REFRESH_TOKEN for normal use; remove EBAY_ACCESS_TOKEN after testing "
        "so the script refreshes automatically."
    )
    return 0


def get_access_token() -> str:
    access_token = get_env("EBAY_ACCESS_TOKEN")
    if access_token:
        return access_token
    return refresh_access_token()


def build_get_my_ebay_buying_xml(entries_per_page: int = 20, page: int = 1) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBayBuyingRequest xmlns="{EBAY_XML_NS}">
  <Version>{COMPATIBILITY_LEVEL}</Version>

  <WatchList>
    <Include>true</Include>
    <Pagination>
      <EntriesPerPage>{entries_per_page}</EntriesPerPage>
      <PageNumber>{page}</PageNumber>
    </Pagination>
    <Sort>EndTime</Sort>
  </WatchList>

  <BidList>
    <Include>true</Include>
    <Pagination>
      <EntriesPerPage>{entries_per_page}</EntriesPerPage>
      <PageNumber>{page}</PageNumber>
    </Pagination>
    <Sort>EndTime</Sort>
  </BidList>
</GetMyeBayBuyingRequest>
"""


def build_get_my_ebay_selling_xml(entries_per_page: int = 50, page: int = 1) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingRequest xmlns="{EBAY_XML_NS}">
  <DetailLevel>ReturnAll</DetailLevel>

  <ActiveList>
    <Include>true</Include>
    <Pagination>
      <EntriesPerPage>{entries_per_page}</EntriesPerPage>
      <PageNumber>{page}</PageNumber>
    </Pagination>
    <Sort>EndTime</Sort>
  </ActiveList>

  <ScheduledList>
    <Include>false</Include>
  </ScheduledList>
  <SoldList>
    <Include>false</Include>
  </SoldList>
  <UnsoldList>
    <Include>false</Include>
  </UnsoldList>
  <SellingSummary>
    <Include>true</Include>
  </SellingSummary>
</GetMyeBaySellingRequest>
"""


def build_get_seller_list_xml() -> str:
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=120)
    start_text = ebay_time(now)
    end_text = ebay_time(end)

    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetSellerListRequest xmlns="{EBAY_XML_NS}">
  <GranularityLevel>Fine</GranularityLevel>
  <EndTimeFrom>{start_text}</EndTimeFrom>
  <EndTimeTo>{end_text}</EndTimeTo>
  <Pagination>
    <EntriesPerPage>200</EntriesPerPage>
    <PageNumber>1</PageNumber>
  </Pagination>
</GetSellerListRequest>
"""


def build_get_best_offers_xml() -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetBestOffersRequest xmlns="{EBAY_XML_NS}">
  <DetailLevel>ReturnAll</DetailLevel>
  <BestOfferStatus>Active</BestOfferStatus>
</GetBestOffersRequest>
"""


def call_trading_api(access_token: str, call_name: str, xml_body: str) -> ET.Element:
    site_id = get_env("EBAY_SITE_ID", "0")

    response = requests.post(
        trading_endpoint(),
        headers={
            "Content-Type": "text/xml;charset=UTF-8",
            "X-EBAY-API-COMPATIBILITY-LEVEL": COMPATIBILITY_LEVEL,
            "X-EBAY-API-CALL-NAME": call_name,
            "X-EBAY-API-SITEID": site_id,
            "X-EBAY-API-IAF-TOKEN": access_token,
        },
        data=xml_body.encode("utf-8"),
        timeout=30,
    )

    # Helpful during first setup.
    if response.status_code >= 400:
        print(response.text)
        response.raise_for_status()

    root = ET.fromstring(response.text)

    ack = text(root, "e:Ack")
    if ack not in {"Success", "Warning"}:
        print("\nRaw eBay response:\n")
        print(response.text)
        raise RuntimeError(f"eBay returned Ack={ack!r}")

    return root


def call_get_my_ebay_buying(access_token: str) -> ET.Element:
    return call_trading_api(
        access_token,
        READ_ONLY_CALL_NAME,
        build_get_my_ebay_buying_xml(),
    )


def call_get_my_ebay_selling(access_token: str) -> ET.Element:
    entries_per_page = int(get_env("EBAY_SELLING_ENTRIES_PER_PAGE", "50"))
    return call_trading_api(
        access_token,
        SELLING_CALL_NAME,
        build_get_my_ebay_selling_xml(entries_per_page=entries_per_page),
    )


def call_get_seller_list(access_token: str) -> ET.Element:
    return call_trading_api(
        access_token,
        SELLER_LIST_CALL_NAME,
        build_get_seller_list_xml(),
    )


def call_get_best_offers(access_token: str) -> ET.Element:
    return call_trading_api(
        access_token,
        BEST_OFFERS_CALL_NAME,
        build_get_best_offers_xml(),
    )


def call_get_traffic_report(access_token: str, item_ids: list[str]) -> dict[str, Any]:
    if not item_ids:
        return {}

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=30)
    date_range = f"[{start:%Y%m%d}..{now:%Y%m%d}]"
    listing_ids = "{" + "|".join(item_ids) + "}"

    response = requests.get(
        analytics_endpoint(),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        params={
            "dimension": "LISTING",
            "filter": f"listing_ids:{listing_ids},date_range:{date_range}",
            "metric": "LISTING_VIEWS_TOTAL",
            "sort": "LISTING_VIEWS_TOTAL",
        },
        timeout=30,
    )

    if response.status_code in {400, 401, 403}:
        print(
            "\nNote: eBay did not return Analytics views. If you need views, "
            "regenerate the refresh token with the sell.analytics.readonly scope."
        )
        return {}

    if response.status_code >= 400:
        print(response.text)
        response.raise_for_status()

    payload: dict[str, Any] = response.json()
    return payload


def text(parent: ET.Element | None, path: str) -> str | None:
    if parent is None:
        return None
    value = parent.findtext(path, namespaces=NS)
    return value.strip() if value else None


def first_text(parent: ET.Element | None, paths: list[str]) -> str | None:
    for path in paths:
        value = text(parent, path)
        if value:
            return value
    return None


def money(item: ET.Element, path: str) -> dict[str, Any] | None:
    node = item.find(path, namespaces=NS)
    if node is None or node.text is None:
        return None

    try:
        amount: float | str = float(node.text)
    except ValueError:
        amount = node.text

    return {
        "amount": amount,
        "currency": node.attrib.get("currencyID"),
    }


def ebay_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_ebay_time(value: str | None) -> datetime | None:
    if not value:
        return None

    # eBay timestamps are typically UTC/GMT, e.g. 2026-07-09T01:24:00.000Z
    normalized = value.replace("Z", "+00:00")

    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def seconds_until(value: str | None) -> int | None:
    end_time = parse_ebay_time(value)
    if not end_time:
        return None

    if end_time.tzinfo is None:
        end_time = end_time.replace(tzinfo=timezone.utc)

    remaining = int((end_time - datetime.now(timezone.utc)).total_seconds())
    return max(0, remaining)


def format_seconds(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"

    if seconds <= 0:
        return "ended"

    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)

    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def parse_item(item: ET.Element, kind: str) -> dict[str, Any]:
    end_time = first_text(
        item,
        [
            "e:ListingDetails/e:EndTime",
            "e:EndTime",
        ],
    )
    secs_left = seconds_until(end_time)

    data: dict[str, Any] = {
        "kind": kind,
        "item_id": text(item, "e:ItemID"),
        "title": text(item, "e:Title"),
        "listing_type": text(item, "e:ListingType"),
        "end_time": end_time,
        "time_left_api": text(item, "e:TimeLeft"),
        "seconds_left": secs_left,
        "time_left_calculated": format_seconds(secs_left),
        "current_price": money(item, "e:SellingStatus/e:CurrentPrice"),
        "bid_count": text(item, "e:SellingStatus/e:BidCount"),
        "url": first_text(
            item,
            [
                "e:ListingDetails/e:ViewItemURL",
                "e:ListingDetails/e:ViewItemURLForNaturalSearch",
                "e:ViewItemURL",
            ],
        ),
        "image": first_text(
            item,
            [
                "e:PictureDetails/e:GalleryURL",
                "e:GalleryURL",
            ],
        ),
    }

    if kind == "bid":
        data.update(
            {
                "max_bid": money(item, "e:BiddingDetails/e:MaxBid"),
                "winning": text(item, "e:BiddingDetails/e:Winning"),
            }
        )

    return data


def parse_selling_item(item: ET.Element) -> dict[str, Any]:
    end_time = first_text(
        item,
        [
            "e:ListingDetails/e:EndTime",
            "e:EndTime",
        ],
    )
    secs_left = seconds_until(end_time)

    return {
        "kind": "selling",
        "item_id": text(item, "e:ItemID"),
        "title": text(item, "e:Title"),
        "listing_type": text(item, "e:ListingType"),
        "end_time": end_time,
        "seconds_left": secs_left,
        "time_left_calculated": format_seconds(secs_left),
        "current_price": money(item, "e:SellingStatus/e:CurrentPrice")
        or money(item, "e:StartPrice"),
        "bid_count": text(item, "e:SellingStatus/e:BidCount"),
        "quantity": text(item, "e:Quantity"),
        "quantity_available": text(item, "e:QuantityAvailable"),
        "quantity_sold": text(item, "e:SellingStatus/e:QuantitySold"),
        "views": text(item, "e:HitCount"),
        "watchers": text(item, "e:WatchCount"),
        "questions": text(item, "e:QuestionCount"),
        "offers": text(item, "e:BestOfferDetails/e:BestOfferCount"),
        "url": first_text(
            item,
            [
                "e:ListingDetails/e:ViewItemURL",
                "e:ListingDetails/e:ViewItemURLForNaturalSearch",
                "e:ViewItemURL",
            ],
        ),
        "image": first_text(
            item,
            [
                "e:PictureDetails/e:GalleryURL",
                "e:GalleryURL",
            ],
        ),
    }


def parse_container(root: ET.Element, container_name: str, kind: str) -> list[dict[str, Any]]:
    container = root.find(f"e:{container_name}", namespaces=NS)
    if container is None:
        return []

    items = container.findall("e:ItemArray/e:Item", namespaces=NS)
    return [parse_item(item, kind) for item in items]


def parse_selling_container(root: ET.Element) -> list[dict[str, Any]]:
    container = root.find("e:ActiveList", namespaces=NS)
    if container is None:
        return []

    items = container.findall("e:ItemArray/e:Item", namespaces=NS)
    return [parse_selling_item(item) for item in items]


def seller_list_views_by_item_id(root: ET.Element) -> dict[str, str]:
    views: dict[str, str] = {}
    for item in root.findall("e:ItemArray/e:Item", namespaces=NS):
        item_id = text(item, "e:ItemID")
        hit_count = text(item, "e:HitCount")
        if item_id and hit_count:
            views[item_id] = hit_count
    return views


def analytics_views_by_item_id(payload: dict[str, Any]) -> dict[str, str]:
    views: dict[str, str] = {}
    for record in payload.get("records", []):
        dimension_values = record.get("dimensionValues") or []
        metric_values = record.get("metricValues") or []
        if not dimension_values or not metric_values:
            continue

        item_id = str(dimension_values[0].get("value") or "")
        value = metric_values[0].get("value")
        applicable = metric_values[0].get("applicable", True)
        if item_id and applicable and value is not None:
            views[item_id] = str(value)
    return views


def active_offers_by_item_id(root: ET.Element) -> dict[str, int]:
    offers: dict[str, int] = {}
    for item_offers in root.findall("e:ItemBestOffersArray/e:ItemBestOffers", namespaces=NS):
        role = text(item_offers, "e:Role")
        if role and role != "Seller":
            continue

        item_id = text(item_offers, "e:Item/e:ItemID")
        if not item_id:
            continue

        count = 0
        for offer in item_offers.findall("e:BestOfferArray/e:BestOffer", namespaces=NS):
            status = text(offer, "e:Status")
            if status in {None, "Active", "Countered", "Pending"}:
                count += 1

        offers[item_id] = offers.get(item_id, 0) + count
    return offers


def merge_selling_views(
    selling_items: list[dict[str, Any]], views_by_item_id: dict[str, str]
) -> None:
    for item in selling_items:
        item_id = item.get("item_id")
        if item_id and views_by_item_id.get(item_id):
            item["views"] = views_by_item_id[item_id]


def merge_selling_offers(
    selling_items: list[dict[str, Any]], offers_by_item_id: dict[str, int]
) -> None:
    for item in selling_items:
        item_id = item.get("item_id")
        if item_id and item_id in offers_by_item_id:
            item["offers"] = str(offers_by_item_id[item_id])


def print_items(title: str, items: list[dict[str, Any]]) -> None:
    print(f"\n{title}: {len(items)}")

    if not items:
        print("  None found.")
        return

    for i, item in enumerate(items, start=1):
        price = item.get("current_price") or {}
        price_str = (
            f"{price.get('amount')} {price.get('currency')}"
            if price
            else "unknown price"
        )

        print(f"\n  {i}. {item.get('title') or '(untitled)'}")
        print(f"     Item ID:     {item.get('item_id')}")
        print(f"     Price:       {price_str}")
        print(f"     Bid count:   {item.get('bid_count') or 'unknown'}")
        print(f"     End time:    {item.get('end_time') or 'unknown'}")
        print(f"     Time left:   {item.get('time_left_calculated')}")

        if item["kind"] == "bid":
            max_bid = item.get("max_bid") or {}
            max_bid_str = (
                f"{max_bid.get('amount')} {max_bid.get('currency')}"
                if max_bid
                else "unknown"
            )
            print(f"     Max bid:     {max_bid_str}")
            print(f"     Winning:     {item.get('winning') or 'unknown'}")

        if item.get("url"):
            print(f"     URL:         {item['url']}")


def print_selling_items(title: str, items: list[dict[str, Any]]) -> None:
    print(f"\n{title}: {len(items)}")

    if not items:
        print("  None found.")
        return

    for i, item in enumerate(items, start=1):
        price = item.get("current_price") or {}
        price_str = (
            f"{price.get('amount')} {price.get('currency')}"
            if price
            else "unknown price"
        )

        print(f"\n  {i}. {item.get('title') or '(untitled)'}")
        print(f"     Item ID:     {item.get('item_id')}")
        print(f"     Price:       {price_str}")
        print(f"     Quantity:    {item.get('quantity_available') or 'unknown'} available")
        print(f"     Sold:        {item.get('quantity_sold') or '0'}")
        print(f"     Bid count:   {item.get('bid_count') or '0'}")
        print(f"     Views:       {item.get('views') or 'unknown'}")
        print(f"     Watchers:    {item.get('watchers') or '0'}")
        print(f"     Offers:      {item.get('offers') or '0'}")
        print(f"     Questions:   {item.get('questions') or '0'}")
        print(f"     End time:    {item.get('end_time') or 'unknown'}")
        print(f"     Time left:   {item.get('time_left_calculated')}")

        if item.get("url"):
            print(f"     URL:         {item['url']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only eBay My Buying test")
    parser.add_argument(
        "--oauth-login",
        action="store_true",
        help="run a one-time OAuth consent flow and print tokens",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="print the consent URL without opening a browser",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.oauth_login:
        return run_oauth_login(open_browser=not args.no_browser)

    print(f"Environment: {ebay_environment()}")
    print(f"Endpoint:    {trading_endpoint()}")
    print(
        f"Calls:       {READ_ONLY_CALL_NAME}, {SELLING_CALL_NAME}, "
        f"{SELLER_LIST_CALL_NAME}, {BEST_OFFERS_CALL_NAME}"
    )
    print(f"Auth:        {auth_source()}")

    access_token = get_access_token()
    buying_root = call_get_my_ebay_buying(access_token)
    selling_root = call_get_my_ebay_selling(access_token)
    seller_list_root = call_get_seller_list(access_token)
    best_offers_root = call_get_best_offers(access_token)

    watch_items = parse_container(buying_root, "WatchList", "watch")
    bid_items = parse_container(buying_root, "BidList", "bid")
    selling_items = parse_selling_container(selling_root)
    merge_selling_views(selling_items, seller_list_views_by_item_id(seller_list_root))
    merge_selling_offers(selling_items, active_offers_by_item_id(best_offers_root))
    analytics_payload = call_get_traffic_report(
        access_token,
        [str(item["item_id"]) for item in selling_items if item.get("item_id")],
    )
    merge_selling_views(selling_items, analytics_views_by_item_id(analytics_payload))

    print_items("Watched items", watch_items)
    print_items("Items you are bidding on", bid_items)
    print_selling_items("Active selling items", selling_items)

    print("\nDone. This script made only read-only eBay Trading API calls.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
