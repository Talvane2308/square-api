```markdown
# Square Daily Sales Reports Automation

A Python automation toolkit that fetches raw sales and payment data from Square's APIs and generates CSV reports matching Square's official "Sales Summary Displayed by Location" format.

## Overview

This repository contains two main scripts:

- **`square-api.py`**: The primary bulk automation script. It loops through all accounts configured in your production credentials and downloads daily sales reports in bulk (defaults to yesterday's data).
- **`validate_reports.py`**: An audit tool that validates the reporting logic against a dedicated reference account covering the broadest set of transaction types. When the generated report matches Square's official dashboard for this account, it provides strong confidence that the same calculation logic is working correctly for the remaining accounts.

## Why This Project Exists

Square's APIs expose raw transactional data, but do not provide the same pre-aggregated daily sales reports available in the Square Dashboard. This script bridges that gap by:
- Fetching raw order and payment data from Square's APIs
- Calculating totals per location (Gross Sales, Fees, Tax, Tips, etc.)
- Generating CSV reports identical to Square's official format
- Validating core calculation logic through targeted reference-account audits

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
└── README.md                      # This file

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
* **`auth/test_sample.json`**: Configure your dedicated reference account used for logic auditing and quality assurance.

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
│   ├── account_name-daily-sales-report-2026-08-03.csv
│   └── another_account-daily-sales-report-2026-08-03.csv

```

Each CSV follows Square's format, including columns for Gross Sales, Net Sales, Tax, Tips, Fees, Net Total, and more.

---

## Important Notes

* **Order Inclusion Rules**: Orders are included if they have tenders (normal payments) or refunds, filtered by `created_at`.
* **Time Zone**: Calculations use the `America/Los_Angeles` timezone by default. Modify `TIMEZONE` in `square-api.py` if needed.
* **Security**: Never commit the `auth/` folder or real tokens to version control. The `.gitignore` file is pre-configured to protect them.

```

```
