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
6. Fetches the real fee for each payment via the Payments API, in parallel
   (WORKERS_PER_ACCOUNT workers)
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

BATCH_PAUSE_SECONDS = 15
BATCH_SIZE = 6
WORKERS_PER_ACCOUNT = 8

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
    response = requests.get(f"{SQUARE_API_BASE}/locations", headers=headers, timeout=30)

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

        response = requests.post(url, headers=headers, json=body, timeout=30)

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


def fetch_payment_fee(access_token, payment_id):
    """Fetches the real fee for a payment via the Payments API (not
    available in the Orders API). The returned value is always positive
    (represents the amount charged) -- callers should subtract it, not add."""
    headers = {
        "Square-Version": SQUARE_VERSION,
        "Authorization": f"Bearer {access_token}",
    }
    response = requests.get(
        f"{SQUARE_API_BASE}/payments/{payment_id}", headers=headers, timeout=30
    )

    total_fee = 0
    if response.status_code == 200:
        payment = response.json().get("payment", {})
        for fee in payment.get("processing_fee", []):
            total_fee += abs(fee.get("amount_money", {}).get("amount", 0))

    return total_fee


def fetch_fees_in_parallel(access_token, payment_ids):
    """Fetches fees for several payment_ids at once, using a worker pool --
    this is what actually speeds up processing for accounts with many
    orders (previously it was one sequential network call per payment).
    Returns a dict {payment_id: fee_cents}."""
    result = {}
    unique_ids = list(set(payment_ids))

    if not unique_ids:
        return result

    with ThreadPoolExecutor(max_workers=WORKERS_PER_ACCOUNT) as executor:
        futures = {
            executor.submit(fetch_payment_fee, access_token, pid): pid
            for pid in unique_ids
        }
        for future in as_completed(futures):
            pid = futures[future]
            result[pid] = future.result()

    return result


def sum_orders_by_location(orders, access_token):
    """Groups and sums the financial totals of each order, by location_id.
    An order is included if it has tenders (normal payment) OR refunds
    (return/cancellation order -- these don't have their own tenders, only
    a refunds[] field, so they need to be accepted too)."""
    totals = {}

    valid_orders = [o for o in orders if o.get("tenders") or o.get("refunds")]

    all_payment_ids = []
    for order in valid_orders:
        for tender in order.get("tenders", []):
            payment_id = tender.get("payment_id") or tender.get("id")
            if payment_id:
                all_payment_ids.append(payment_id)

    fee_cache = fetch_fees_in_parallel(access_token, all_payment_ids)

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

        gross = sum(
            item.get("gross_sales_money", {}).get("amount", 0)
            for item in order.get("line_items", [])
        )

        t["gross_sales"] += gross
        t["items"] += gross
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
        totals = sum_orders_by_location(orders, access_token)

        path = generate_csv(account_name, totals, sorted_locations, day, day_start_hour, REPORTS_FOLDER)
        return f"[OK] {account_name}: {path} ({len(orders)} orders, {len(sorted_locations)} active locations)"

    except Exception as e:
        return f"[ERROR] {account_name}: {e}"


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

    for i, batch in enumerate(batches):
        print(f"--- Batch {i+1}/{len(batches)}: {', '.join(batch)} ---")

        with ThreadPoolExecutor(max_workers=len(batch)) as executor:
            futures = {
                executor.submit(process_account, name, accounts[name], manual_date): name
                for name in batch
            }
            for future in as_completed(futures):
                result = future.result()
                print(f"  {result}")

        is_last_batch = (i == len(batches) - 1)
        if not is_last_batch:
            print(f"Pausing {BATCH_PAUSE_SECONDS}s before next batch...")
            time.sleep(BATCH_PAUSE_SECONDS)

    print("Done.")


if __name__ == "__main__":
    main()
