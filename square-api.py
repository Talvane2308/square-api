"""
Daily Sales Report Automation - Square API

What this script does:
1. Reads accounts_token.json (account name -> access_token + day_start_hour)
2. Automatically discovers all ACTIVE locations for each account (via API)
   -- new stores appear automatically, no need to manually list location_ids
3. For each account, calculates the previous day's window in the
   America/Los_Angeles timezone (adjustable via day_start_hour, for accounts
   whose business hours cross midnight)
4. Fetches all completed orders within that window, filtering by created_at
   (not closed_at -- this matters for delivery orders that get paid after
   the order was originally placed, which could push them across the
   day boundary if we filtered by closed_at instead)
5. Includes orders that have either tenders (normal payment) or refunds
   (return/cancellation orders -- these don't have their own tenders, only
   a refunds[] field, so they need to be accepted too)
6. Fetches the real fee for all payments via the ListPayments API, in bulk
   per location (drastically fewer requests than one GetPayment call per
   payment_id). ListPayments doesn't support a status filter server-side,
   so only payments with status == COMPLETED are kept when building the
   fee cache -- matching the same COMPLETED-only guarantee the old
   GetPayment-per-id approach had implicitly (via orders/search already
   filtering to COMPLETED orders). Locations are queried in parallel
   (WORKERS_PER_ACCOUNT workers), since ListPayments only accepts one
   location_id per call (unlike orders/search, which accepts up to 10).
7. Sums totals per location (Gross Sales, Net Sales, Tax, Tip, Fees, Refunds, etc)
8. Generates a CSV in the exact same format as Square's native
   "Sales Summary Displayed by Location" report, with stores sorted
   alphabetically (matching the order the Locations API returns)
9. Processes accounts in PARALLEL, in batches of BATCH_SIZE -- all accounts
   in a batch start together, and the next batch only starts once every
   account in the current batch has finished (even if one takes much
   longer than the others). A pause of BATCH_PAUSE_SECONDS happens between
   batches to stay within Square's (undocumented) rate limits.

Output file name: <account_name>-daily-sales-report-YYYY-MM-DD-YYYY-MM-DD.csv
Output location: reports/MM-DD/ (subfolder per report date, relative to this script)
"""

import json
import random
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import os

# ---------- CONFIGURATION ----------

ACCOUNTS_JSON_PATH = "auth/accounts_token.json"
REPORTS_FOLDER = "reports"
TIMEZONE = ZoneInfo("America/Los_Angeles")
SQUARE_API_BASE = "https://connect.squareup.com/v2"
SQUARE_VERSION = "2026-05-20"

BATCH_PAUSE_SECONDS = 10
BATCH_SIZE = 8
WORKERS_PER_ACCOUNT = 8

# Retry settings for transient failures (429 rate limit, network errors).
# Square doesn't publish a fixed requests/sec limit -- their own docs say
# it can vary per endpoint and recommend exponential backoff with a
# randomized delay (jitter), so multiple parallel requests that get rate
# limited at the same moment don't all retry at the exact same instant
# and collide again. Non-retryable errors (401, 404, etc) fail immediately
# since waiting and trying again wouldn't fix a bad token or missing
# resource.
MAX_RETRIES = 3
RETRY_BASE_DELAY_SECONDS = 2

COLUMNS = [
    "Gross Sales", "Items", "Service Charges", "Refunds",
    "Discounts & Comps", "Net Sales", "Gift Card Sales", "Tax",
    "Tip", "Partial Refunds", "Total Collected", "Fees", "Net Total"
]


def calculate_previous_day_window(manual_date=None, day_start_hour=0):
    """Calculates the 'sales day' window (yesterday, by default) in the
    Los Angeles timezone, converted to UTC (the format Square's API expects).
    day_start_hour defines when the business day begins (0 = midnight,
    default; 4 = 4am, for accounts with different business hours that
    cross the midnight boundary). The window runs from day_start_hour of
    the 'day' until day_start_hour-1 of the following day (e.g. 4am
    yesterday to 3:59am today). If manual_date is provided (format
    YYYY-MM-DD), it's used instead of 'yesterday' -- used for validating
    against already-known reports."""
    if manual_date:
        day = datetime.strptime(manual_date, "%Y-%m-%d").date()
    else:
        now_la = datetime.now(TIMEZONE)
        day = now_la.date() - timedelta(days=1)

    start_la = datetime.combine(day, datetime.min.time(), tzinfo=TIMEZONE) + timedelta(hours=day_start_hour)
    end_la = start_la + timedelta(days=1) - timedelta(seconds=1)

    start_utc = start_la.astimezone(ZoneInfo("UTC"))
    end_utc = end_la.astimezone(ZoneInfo("UTC"))

    return day, start_utc, end_utc


def format_api_date(dt):
    """Formats a datetime into the ISO 8601 format Square's API requires."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def request_with_retry(method, url, **kwargs):
    """Wraps requests.get/requests.post with retry logic for transient
    failures. Retries on 429 (rate limited) and on network-level errors
    (timeout, connection reset, etc) -- these are the cases where trying
    again has a real chance of succeeding. Does NOT retry on other status
    codes (401, 404, etc), since those indicate a real problem (bad token,
    wrong endpoint) that waiting won't fix -- the response is returned
    as-is so the caller's existing status-code check handles it.

    Uses exponential backoff (2s, 4s, 8s) with a randomized jitter added
    on top, so that when several accounts get rate limited around the
    same moment (likely, since accounts run in parallel batches), their
    retries don't all land on the API at the exact same instant and
    trigger the same 429 again."""
    last_exception = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.request(method, url, **kwargs)
        except requests.exceptions.RequestException as e:
            last_exception = e
            response = None

        is_last_attempt = attempt == MAX_RETRIES

        if response is not None and response.status_code != 429:
            # Success or a non-retryable error -- return immediately either way.
            return response

        if is_last_attempt:
            if response is not None:
                return response  # let the caller's status check report the final 429
            raise last_exception  # network error persisted through every retry

        delay = (RETRY_BASE_DELAY_SECONDS ** (attempt + 1)) + random.uniform(0, 1)
        time.sleep(delay)

    return response  # unreachable, satisfies linters


def fetch_active_locations(access_token):
    """Automatically discovers all ACTIVE locations linked to the account.
    Returns a list of (location_id, name), already in the alphabetical
    order the API returns -- so we don't need to manually list location_ids
    in accounts_token.json, and new stores show up automatically on the
    next run."""
    headers = {
        "Square-Version": SQUARE_VERSION,
        "Authorization": f"Bearer {access_token}",
    }
    response = request_with_retry("GET", f"{SQUARE_API_BASE}/locations", headers=headers, timeout=30)

    if response.status_code != 200:
        raise RuntimeError(
            f"Error fetching locations (status {response.status_code}): {response.text}"
        )

    locations = response.json().get("locations", [])
    active = [
        (loc["id"], loc.get("name", loc["id"]))
        for loc in locations
        if loc.get("status") == "ACTIVE"
    ]
    return active


def fetch_orders_one_batch(access_token, location_ids_batch, start_utc, end_utc):
    """Fetches orders for a batch of up to 10 locations (API limit)."""
    url = f"{SQUARE_API_BASE}/orders/search"
    headers = {
        "Square-Version": SQUARE_VERSION,
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    all_orders = []
    cursor = None

    while True:
        body = {
            "location_ids": location_ids_batch,
            "query": {
                "filter": {
                    "state_filter": {"states": ["COMPLETED"]},
                    "date_time_filter": {
                        "created_at": {
                            "start_at": format_api_date(start_utc),
                            "end_at": format_api_date(end_utc),
                        }
                    },
                },
                "sort": {"sort_field": "CLOSED_AT", "sort_order": "ASC"},
            },
            "limit": 500,
            "return_entries": False,
        }
        if cursor:
            body["cursor"] = cursor

        response = request_with_retry("POST", url, headers=headers, json=body, timeout=30)

        if response.status_code != 200:
            raise RuntimeError(
                f"Square API error (status {response.status_code}): {response.text}"
            )

        data = response.json()
        orders = data.get("orders", [])
        all_orders.extend(orders)

        cursor = data.get("cursor")
        if not cursor:
            break

    return all_orders


def fetch_orders(access_token, location_ids, start_utc, end_utc):
    """Fetches all completed orders within the window, for a list of
    locations. Square's API accepts a maximum of 10 location_ids per
    call, so we split into batches of 10 and merge the results."""
    all_orders = []
    LOCATION_BATCH_SIZE = 10

    for i in range(0, len(location_ids), LOCATION_BATCH_SIZE):
        batch = location_ids[i:i + LOCATION_BATCH_SIZE]
        batch_orders = fetch_orders_one_batch(access_token, batch, start_utc, end_utc)
        all_orders.extend(batch_orders)

    return all_orders


def cents_to_dollars(cents):
    """Converts cents (integer, as returned by the API) to a dollar float."""
    return (cents or 0) / 100.0


def fetch_payments_for_one_location(access_token, loc_id, start_utc, end_utc):
    """Fetches every payment for a single location within the window,
    paginating as needed, and returns a dict {payment_id: fee_cents} --
    but ONLY for payments whose status is COMPLETED.

    ListPayments has no server-side status filter (confirmed against
    Square's own API reference and developer forum -- there is no status
    query parameter), and it returns payments of every status, including
    FAILED and CANCELED (the v1 endpoint didn't; v2 does). The old
    GetPayment-per-id approach never had this problem, because it only
    ever looked up payment_ids that came from orders already filtered to
    state_filter: COMPLETED by orders/search. This filter recreates that
    same guarantee explicitly, so the fee cache can never contain a
    payment tied to a CANCELED/FAILED/APPROVED/PENDING attempt -- rather
    than relying on the caller to happen to never look those IDs up.

    ListPayments only accepts one location_id per call (unlike
    orders/search, which accepts up to 10), so this is called once per
    location -- the parallelism across locations happens in
    fetch_payments_for_locations."""
    headers = {
        "Square-Version": SQUARE_VERSION,
        "Authorization": f"Bearer {access_token}",
    }

    fees = {}
    cursor = None

    while True:
        params = {
            "location_id": loc_id,
            "begin_time": format_api_date(start_utc),
            "end_time": format_api_date(end_utc),
            "limit": 100,
        }
        if cursor:
            params["cursor"] = cursor

        response = request_with_retry(
            "GET", f"{SQUARE_API_BASE}/payments", headers=headers, params=params, timeout=30
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Square API error fetching payments (status {response.status_code}): {response.text}"
            )

        data = response.json()
        for payment in data.get("payments", []):
            if payment.get("status") != "COMPLETED":
                continue  # skip FAILED/CANCELED/APPROVED/PENDING -- see docstring

            pid = payment.get("id")
            if not pid:
                continue

            total_fee = 0
            for fee in payment.get("processing_fee", []):
                total_fee += abs(fee.get("amount_money", {}).get("amount", 0))

            fees[pid] = total_fee

        cursor = data.get("cursor")
        if not cursor:
            break

    return fees


def fetch_payments_for_locations(access_token, location_ids, start_utc, end_utc):
    """Fetches fees for all COMPLETED payments across all of an account's
    locations, in parallel (WORKERS_PER_ACCOUNT workers) -- since
    ListPayments only accepts one location per call, this is what keeps a
    many-location account from paying a sequential cost per location.
    Returns a merged dict {payment_id: fee_cents} across every location."""
    fee_cache = {}

    if not location_ids:
        return fee_cache

    with ThreadPoolExecutor(max_workers=WORKERS_PER_ACCOUNT) as executor:
        futures = {
            executor.submit(fetch_payments_for_one_location, access_token, loc_id, start_utc, end_utc): loc_id
            for loc_id in location_ids
        }
        for future in as_completed(futures):
            fee_cache.update(future.result())

    return fee_cache


def sum_orders_by_location(orders, fee_cache):
    """Groups and sums the financial totals of each order, by location_id.
    An order is included if it has tenders (normal payment) OR refunds
    (return/cancellation order -- these don't have their own tenders, only
    a refunds[] field, so they need to be accepted too). fee_cache is a
    pre-built {payment_id: fee_cents} dict (see fetch_payments_for_locations),
    already restricted to COMPLETED payments."""
    totals = {}

    valid_orders = [o for o in orders if o.get("tenders") or o.get("refunds")]

    for order in valid_orders:
        loc_id = order.get("location_id")
        if loc_id not in totals:
            totals[loc_id] = {
                "gross_sales": 0,
                "items": 0,
                "service_charges": 0,
                "refunds": 0,
                "discounts": 0,
                "net_sales": 0,
                "gift_card_sales": 0,
                "tax": 0,
                "tip": 0,
                "partial_refunds": 0,
                "total_collected": 0,
                "fees": 0,
                "net_total": 0,
            }

        t = totals[loc_id]

        total_money = order.get("total_money", {}).get("amount", 0)
        total_tax_money = order.get("total_tax_money", {}).get("amount", 0)
        total_tip_money = order.get("total_tip_money", {}).get("amount", 0)
        total_discount_money = order.get("total_discount_money", {}).get("amount", 0)
        total_service_charge_money = order.get("total_service_charge_money", {}).get("amount", 0)

        # Gift card activation/reload line items don't count as regular
        # Gross Sales -- Square reports them in a separate "Gift Card
        # Sales" column instead. Only line items with item_type == "ITEM"
        # (or missing item_type, which defaults to a regular item) count
        # toward gross_sales/items.
        gross = 0
        gift_card_sales = 0
        for item in order.get("line_items", []):
            amount = item.get("gross_sales_money", {}).get("amount", 0)
            if item.get("item_type") == "GIFT_CARD":
                gift_card_sales += amount
            else:
                gross += amount

        t["gross_sales"] += gross
        t["items"] += gross
        t["gift_card_sales"] += gift_card_sales
        t["service_charges"] += total_service_charge_money
        t["discounts"] += total_discount_money
        t["tax"] += total_tax_money
        t["tip"] += total_tip_money
        t["net_sales"] += gross - total_discount_money

        order_refunds = sum(
            r.get("amount_money", {}).get("amount", 0)
            for r in order.get("refunds", [])
        )
        t["refunds"] += order_refunds
        t["net_sales"] -= order_refunds

        t["total_collected"] += total_money - order_refunds

        order_fees = 0
        for tender in order.get("tenders", []):
            payment_id = tender.get("payment_id") or tender.get("id")
            if payment_id:
                order_fees += fee_cache.get(payment_id, 0)
        t["fees"] += order_fees

        t["net_total"] += total_money - order_refunds - order_fees

    return totals


def format_dollar_value(cents):
    """Formats cents into Square's standard format: $1,234.56 or -$12.34
    if negative. When the formatted value contains a comma (numbers >=
    1000), it's wrapped in double quotes, matching Square's official CSV
    behavior (standard CSV practice to prevent the number's comma from
    being read as a column separator)."""
    value = cents_to_dollars(cents)
    sign = "-" if value < 0 else ""
    abs_value = abs(value)
    text = f"{sign}${abs_value:,.2f}"
    if "," in text:
        text = f'"{text}"'
    return text


def generate_csv(account_name, totals_by_location, sorted_locations, day, day_start_hour, output_folder):
    """Generates the CSV file in Square's exact Sales Summary format.
    sorted_locations is the (location_id, name) list in alphabetical order
    coming from the API -- stores with no activity that day simply don't
    appear, matching the web dashboard's behavior. When day_start_hour > 0,
    the 'sales day' crosses midnight, so the file name reflects that
    (e.g. 2026-08-02-2026-08-03), matching Square's own naming. The file
    is saved inside a MM-DD subfolder of output_folder, for easy browsing
    by date."""
    start_date_str = day.strftime("%Y-%m-%d")
    if day_start_hour > 0:
        end_date = day + timedelta(days=1)
    else:
        end_date = day
    end_date_str = end_date.strftime("%Y-%m-%d")

    date_folder = day.strftime("%m-%d")
    file_name = f"{account_name}-daily-sales-report-{start_date_str}-{end_date_str}.csv"
    full_path = f"{output_folder}/{date_folder}/{file_name}"

    os.makedirs(os.path.dirname(full_path), exist_ok=True)

    lines = []
    lines.append("Sales Summary")
    lines.append("Display By Location")
    lines.append(
        "Sales Summary Displayed by Location," + ",".join(COLUMNS)
    )

    for loc_id, store_name in sorted_locations:
        if loc_id not in totals_by_location:
            continue

        t = totals_by_location[loc_id]
        values = [
            format_dollar_value(t["gross_sales"]),
            format_dollar_value(t["items"]),
            format_dollar_value(t["service_charges"]),
            format_dollar_value(-t["refunds"] if t["refunds"] else 0),
            format_dollar_value(-t["discounts"] if t["discounts"] else 0),
            format_dollar_value(t["net_sales"]),
            format_dollar_value(t.get("gift_card_sales", 0)),
            format_dollar_value(t["tax"]),
            format_dollar_value(t["tip"]),
            format_dollar_value(-t["partial_refunds"] if t["partial_refunds"] else 0),
            format_dollar_value(t["total_collected"]),
            format_dollar_value(-t["fees"] if t["fees"] else 0),
            format_dollar_value(t["net_total"]),
        ]
        line = f'{store_name},' + ",".join(values)
        lines.append(line)

    content = "\n".join(lines) + "\n"

    with open(full_path, "w", encoding="ascii", newline="\n") as f:
        f.write(content)

    return full_path


def process_account(account_name, account_data, manual_date):
    """Processes an entire account: discovers locations, fetches orders,
    sums totals, and generates the CSV. Returns a result message (success
    or error) -- used by the account-level ThreadPoolExecutor threads."""
    access_token = account_data["access_token"]
    day_start_hour = account_data.get("day_start_hour", 0)

    day, start_utc, end_utc = calculate_previous_day_window(manual_date, day_start_hour)

    try:
        sorted_locations = fetch_active_locations(access_token)
        location_ids = [loc_id for loc_id, _name in sorted_locations]

        orders = fetch_orders(access_token, location_ids, start_utc, end_utc)
        fee_cache = fetch_payments_for_locations(access_token, location_ids, start_utc, end_utc)
        totals = sum_orders_by_location(orders, fee_cache)

        path = generate_csv(account_name, totals, sorted_locations, day, day_start_hour, REPORTS_FOLDER)
        return True, f"[OK] {account_name}: {path} ({len(orders)} orders, {len(sorted_locations)} active locations)"

    except Exception as e:
        return False, f"[ERROR] {account_name}: {e}"


def main():
    import sys

    manual_date = None
    specific_account = None
    args = sys.argv[1:]
    if "--date" in args:
        manual_date = args[args.index("--date") + 1]
    if "--id" in args:
        specific_account = args[args.index("--id") + 1]

    with open(ACCOUNTS_JSON_PATH, "r", encoding="utf-8") as f:
        accounts = json.load(f)

    os.makedirs(REPORTS_FOLDER, exist_ok=True)

    account_names = [specific_account] if specific_account else list(accounts.keys())

    # Splits accounts into batches of BATCH_SIZE. Each batch runs in
    # parallel (all accounts in the batch at the same time); the next
    # batch only starts once every account in the current batch has
    # finished (even if one takes much longer than the others -- the
    # ThreadPoolExecutor context manager waits for all before releasing).
    # Within each account, fee lookups also run in parallel
    # (WORKERS_PER_ACCOUNT).
    batches = [
        account_names[i:i + BATCH_SIZE]
        for i in range(0, len(account_names), BATCH_SIZE)
    ]

    # Tracks which accounts failed (name -> error message), so we can
    # print a clear summary at the end regardless of how many batches
    # ran or in what order they finished.
    failed_accounts = {}

    for i, batch in enumerate(batches):
        print(f"--- Batch {i+1}/{len(batches)}: {', '.join(batch)} ---")

        with ThreadPoolExecutor(max_workers=len(batch)) as executor:
            futures = {
                executor.submit(process_account, name, accounts[name], manual_date): name
                for name in batch
            }
            for future in as_completed(futures):
                account_name = futures[future]
                ok, result = future.result()
                print(f"  {result}")
                if not ok:
                    failed_accounts[account_name] = result

        is_last_batch = (i == len(batches) - 1)
        if not is_last_batch:
            print(f"Pausing {BATCH_PAUSE_SECONDS}s before next batch...")
            time.sleep(BATCH_PAUSE_SECONDS)

    print("Done.")

    # Final summary -- makes failures impossible to miss even when the
    # terminal output has scrolled past the batch that had the error.
    if failed_accounts:
        print()
        print(f"=== {len(failed_accounts)} account(s) FAILED to generate a report ===")
        for account_name, error_message in failed_accounts.items():
            print(f"  - {error_message}")
        # Keeps the terminal window open only when something failed (e.g.
        # when the script is launched by double-clicking a shortcut instead
        # of from an already open terminal), so the failure summary above
        # stays visible instead of the window closing immediately. On a
        # clean run the window is allowed to close right away.
        input("\nPress Enter to close...")
    else:
        print()
        print("All accounts processed successfully.")


if __name__ == "__main__":
    main()
