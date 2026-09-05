"""
run.py — single entrypoint for the Revenue Recovery Agent pipeline.

Usage (from the project root):
    python run.py                  # full pipeline: generate → execute → dashboard
    python run.py --skip-generate  # re-use existing data/transactions.csv
    python run.py --no-dashboard   # skip HTML dashboard generation

Env vars (optional):
    MOCK_MODE=false                # switch to real Razorpay Test Mode keys
    RAZORPAY_KEY_ID=rzp_test_xxx   # required when MOCK_MODE=false
    RAZORPAY_KEY_SECRET=xxx        # required when MOCK_MODE=false
"""

import sys
import argparse
import subprocess
from pathlib import Path
try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except ImportError:
    pass

ROOT = Path(__file__).parent
SRC  = ROOT / "src"


def run_step(label: str, script: Path):
    print(f"\n{'='*60}")
    print(f"  STEP: {label}")
    print(f"{'='*60}")
    result = subprocess.run(
        [sys.executable, str(script)],
        env=None,          # inherit current env (passes MOCK_MODE etc.)
        check=False,
    )
    if result.returncode != 0:
        print(f"\n[ERROR] {label} exited with code {result.returncode}. Aborting.")
        sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description="Revenue Recovery Agent — full pipeline runner")
    parser.add_argument("--skip-generate", action="store_true",
                        help="Skip data generation (re-use existing transactions.csv)")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="Skip HTML dashboard generation")
    args = parser.parse_args()

    print("\n+======================================================+")
    print("|   Revenue Recovery Agent v2 -- Pipeline Runner      |")
    print("+======================================================+")

    if not args.skip_generate:
        run_step("Generate synthetic dataset (100 records -> data/transactions.csv)",
                 SRC / "generate_data_v2.py")
    else:
        print("\n  [skip] Data generation skipped -- using existing transactions.csv")

    run_step("Execute recovery pipeline (router -> executor -> audit log + eval report)",
             SRC / "executor_v2.py")

    if not args.no_dashboard:
        run_step("Build HTML dashboard (bakes logs into recovery_dashboard_v2.html)",
                 SRC / "build_dashboard.py")

    print("\n+======================================================+")
    print("|   Pipeline complete. Outputs:                       |")
    print(f"|   data/transactions.csv         ({'exists' if (ROOT/'data'/'transactions.csv').exists() else 'missing':7})|")
    print(f"|   logs/audit_log_v2.jsonl       ({'exists' if (ROOT/'logs'/'audit_log_v2.jsonl').exists() else 'missing':7})|")
    print(f"|   logs/eval_report_v2.json      ({'exists' if (ROOT/'logs'/'eval_report_v2.json').exists() else 'missing':7})|")
    print(f"|   recovery_dashboard_v2.html    ({'exists' if (ROOT/'recovery_dashboard_v2.html').exists() else 'missing':7})|")
    print("+======================================================+")
    if not args.no_dashboard:
        print(f"\n  >>  Open in browser: {ROOT / 'recovery_dashboard_v2.html'}")


if __name__ == "__main__":
    main()
