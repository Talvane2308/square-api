#!/usr/bin/env python3
"""
Wrapper for weekly validation of reports against the official Square dashboard.
Usage: python validate_reports.py --date YYYY-MM-DD
Example: python validate_reports.py --date 2026-08-03

This uses auth/test_sample.json, which contains only the Daily Sales Report
account (the one with the highest transaction volume, for the most
comprehensive test).
"""

import sys
import subprocess
import json

def main():
    if "--date" not in sys.argv:
        print("Usage: python validate_reports.py --date YYYY-MM-DD")
        print("Example: python validate_reports.py --date 2026-08-03")
        sys.exit(1)

    try:
        with open("auth/test_sample.json", "r") as f:
            json.load(f)
    except FileNotFoundError:
        print("Error: auth/test_sample.json not found")
        sys.exit(1)
    except json.JSONDecodeError:
        print("Error: auth/test_sample.json is not valid JSON")
        sys.exit(1)

    cmd = [
        sys.executable,
        "-c",
        """
import sys
sys.path.insert(0, '.')
import importlib.util
spec = importlib.util.spec_from_file_location("square_api", "square-api.py")
square_api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(square_api)
square_api.ACCOUNTS_JSON_PATH = 'auth/test_sample.json'
square_api.main()
""",
        *sys.argv[1:],
    ]

    result = subprocess.run(cmd)
    sys.exit(result.returncode)

if __name__ == "__main__":
    main()
