# Square Daily Sales Reports Automation

A Python automation toolkit that fetches raw sales and payment data from Square's APIs and generates CSV reports matching Square's official "Sales Summary Displayed by Location" format.

## Overview

This repository contains two main scripts:

- **`square-api.py`**: The primary bulk automation script. It loops through all accounts configured in your production credentials and downloads daily sales reports in bulk (defaults to yesterday's data). Processes multiple accounts in parallel batches, with resilient error handling and automatic retry on rate limits.
- **`validate_reports.py`**: An audit tool that validates the reporting logic against a dedicated reference account covering the broadest set of transaction types. When the generated report matches Square's official dashboard for this account, it provides strong confidence that the same calculation logic is working correctly for the remaining accounts.

## Why This Project Exists

Square's APIs expose raw transactional data, but do not provide the same pre-aggregated daily sales reports available in the Square Dashboard. This script bridges that gap by:
- Fetching raw order and payment data from Square's APIs
- Calculating totals per location (Gross Sales, Fees, Tax, Tips, Refunds, Gift Card Sales, etc.)
- Generating CSV reports identical to Square's official format
- Validating core calculation logic through targeted reference-account audits
- Operating efficiently with exponential backoff retry logic on transient failures (429 rate limits, network timeouts)

## Key Features

- **Bulk Account Processing**: Handles 37+ accounts in ~2-3 minutes per day
- **Resilient Retry Logic**: Automatic exponential backoff with jitter for rate limits and network errors
- **Payment Fee Optimization**: Uses ListPayments API in bulk per-location (2 requests for 2 locations, vs 58+ sequential GetPayment calls)
- **Order Inclusion Rules**: Correctly handles delivery orders (grouped by `created_at`, not `closed_at`), returns/cancellations with refunds, and gift card sales
- **CSV Format Matching**: Exactly replicates Square's official "Sales Summary Displayed by Location" output, including proper comma-quoting for values ≥ $1,000
- **Error Summary**: Clear reporting of failed accounts at end of run; terminal window stays open on error for visibility

## Directory Structure

```text
square-api/
├── auth/                          # Local configuration folder (ignored by git)
│   ├── accounts_token.json        # Production credentials (ignored)
│   └── test_sample.json           # Validation account (ignored)
├── reports/                       # Generated CSV reports (ignored by git)
├── .gitignore                     # Excludes sensitive files from git
├── accounts_token.example.json    # Template for accounts_token.json
├── test_sample.example.json       # Template for test_sample.json
├── square-api.py                  # Main bulk automation script
├── validate_reports.py            # Audit & validation wrapper script
└── README.md                       # This file
```

## Requirements & Setup

### 1. Requirements

* Python 3.7+
* `requests` library

### 2. Virtual Environment & Dependencies

It is recommended to use a virtual environment:

```bash
python -m venv venv
source venv/bin/activate  # Use venv\Scripts\activate on Windows
pip install requests
```

### 3. Configure Your Credentials

1. Create a folder named `auth` in the root directory of the project.
2. Copy the example template files into the `auth/` folder and rename them:
   * Copy `accounts_token.example.json` to `auth/accounts_token.json`
   * Copy `test_sample.example.json` to `auth/test_sample.json`

### 4. Edit Configuration Files

* **`auth/accounts_token.json`**: Add your bulk production accounts and access tokens.
  ```json
  {
    "account_name": {
      "access_token": "YOUR_SQUARE_ACCESS_TOKEN",
      "day_start_hour": 0
    }
  }
  ```
  - `day_start_hour`: (Optional, default 0) Set to non-zero if business day doesn't start at midnight (e.g., 4 for 4am start). Sales window will be [day_start_hour] to [day_start_hour-1] next day.

* **`auth/test_sample.json`**: Configure your dedicated reference account used for logic auditing and quality assurance. Same format as above.

---

## Usage

### Run the Main Bulk Script

Download yesterday's reports for all production accounts:

```bash
python square-api.py
```

Download reports for a specific account or date:

```bash
python square-api.py --id account_name
python square-api.py --id account_name --date 2026-08-03
python square-api.py --date 2026-08-03
```

### Run Audit / Validation

Test the calculation logic by running the wrapper against your reference account and comparing the generated CSV directly against the official Square dashboard report:

```bash
python validate_reports.py --date 2026-08-03
```

---

## Output

Reports are saved in the `reports/` folder, organized by date:

```text
reports/
├── 08-03/
│   ├── account_name-daily-sales-report-2026-08-03-2026-08-03.csv
│   └── another_account-daily-sales-report-2026-08-03-2026-08-03.csv
└── 08-04/
    └── account_name-daily-sales-report-2026-08-04-2026-08-04.csv
```

Each CSV follows Square's format with columns:
- Gross Sales
- Items (equals Gross Sales)
- Service Charges
- Refunds
- Discounts & Comps
- Net Sales
- Gift Card Sales
- Tax
- Tip
- Partial Refunds
- Total Collected
- Fees
- Net Total

Values with comma separators (≥ $1,000) are wrapped in double quotes per CSV standard.

---

## Performance & Configuration

### Default Batch Settings

```python
BATCH_SIZE = 8              # Accounts per parallel batch
BATCH_PAUSE_SECONDS = 10    # Pause between batches (respect rate limits)
WORKERS_PER_ACCOUNT = 8     # Parallel fee lookups per account
```

These values have been tested with high-volume accounts (~20k/day sales) and are calibrated to stay within Square's adaptive rate limits. Adjusting them without testing is not recommended.

### Retry Settings

```python
MAX_RETRIES = 3                    # Total attempt count (1 initial + 2 retries)
RETRY_BASE_DELAY_SECONDS = 2       # Exponential backoff: 2^1, 2^2, 2^3 seconds
```

Transient failures (429 rate limit, network timeouts) automatically retry with exponential backoff + random jitter. Non-retryable errors (401 bad token, 404 missing endpoint) fail immediately.

---

## Important Notes

### Order Inclusion Rules

- **Tenders**: Normal payments; orders with `tenders[]` are included
- **Refunds**: Return/cancellation orders with `refunds[]` but no `tenders[]` are included
- **Time Window**: Filtered by `created_at`, not `closed_at`
  - This matters for delivery orders (DoorDash, Uber Eats) that may be paid the next day but created earlier
  - Ensures delivery orders land in the correct sales day even if payment processing is delayed
- **Status Filter**: Only payments with status `COMPLETED` are included in fee calculations
  - ListPayments API returns all statuses (COMPLETED, APPROVED, PENDING, FAILED, CANCELED)
  - Explicit filter ensures only confirmed, settled payments contribute to the fee cache

### Time Zone

Calculations use the `America/Los_Angeles` timezone by default. Modify `TIMEZONE` in `square-api.py` if needed.

### Security

- Never commit the `auth/` folder or real tokens to version control
- The `.gitignore` file is pre-configured to protect sensitive files
- Use the `.example.json` templates to document the expected structure

### CSV Format (ASCII Encoding)

Reports are generated with ASCII encoding (`encoding="ascii"`). If store names or locations contain non-ASCII characters (accents, special symbols), the script will fail. Ensure all location names in your Square account use ASCII-compatible characters only.

---

## Validation & Quality Assurance

Before running production automation on all accounts, validate your logic against a high-volume reference account:

1. Run `python validate_reports.py --date <known_date>`
2. Export the official report from Square Dashboard for the same date
3. Compare the two CSV files
   - Line-by-line values should match exactly (totals, fees, taxes, etc.)
   - Store order may differ if you haven't applied alphabetical sorting
   - All numeric values must align

If discrepancies appear, check:
- That all orders in the dashboard appear in your CSV (counts match)
- That fee calculations align with Payments API data for the same period
- That timezone/day-start-hour configuration matches your business hours

---

## Troubleshooting

### Terminal Window Closes on Error

The script keeps the terminal window open when errors occur (for visibility), but closes immediately on success. You can always check the `reports/` folder or run with `--date` flag to verify output.

### Some Accounts Fail While Others Succeed

The script processes accounts in parallel batches. If one account fails (e.g., bad token, API rate limit), others in the batch continue. Failed accounts are listed in the final error summary. Fix the credential or check Square's status, then re-run just that account with `--id account_name`.

### Payment Fees Don't Match Dashboard

Ensure:
- All payment_ids in your orders are captured (no `None` or empty values)
- Payments have status `COMPLETED` (other statuses are filtered)
- The date range matches exactly (including `day_start_hour` logic)
- Network/API errors during fee lookup didn't silently skip payments (check error summary)

---

## License & Contributing

This project is a personal reference implementation. Feel free to use, fork, or adapt it for your own needs.
