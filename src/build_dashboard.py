"""
build_dashboard.py

Regenerates recovery_dashboard_v2.html using whatever is currently in
logs/audit_log_v2.jsonl and logs/eval_report_v2.json.

WHY THIS EXISTS:
The dashboard HTML is a static, self-contained file — it has no live
connection to your logs folder. Every time it opens, it reads the data
that was baked into it AT THE TIME IT WAS GENERATED, nothing more recent.

So the correct workflow is:

    1. python generate_data_v2.py     (creates fresh data/transactions.csv)
    2. python executor_v2.py          (runs pipeline, writes logs/)
    3. python build_dashboard.py      (bakes fresh logs/ into new HTML)
    4. open the OUTPUT file in your browser


"""

import json
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)  # one level up from src/

AUDIT_LOG_PATH = os.path.join(PROJECT_ROOT, "logs", "audit_log_v2.jsonl")
EVAL_REPORT_PATH = os.path.join(PROJECT_ROOT, "logs", "eval_report_v2.json")
TEMPLATE_CANDIDATES = [
    os.path.join(SCRIPT_DIR, "dashboard_v2_template.html"),
    os.path.join(PROJECT_ROOT, "dashboard_v2_template.html"),
]
OUTPUT_PATH = os.path.join(PROJECT_ROOT, "recovery_dashboard_v2.html")


def find_template():
    for path in TEMPLATE_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def main():
    for path, label in [
        (AUDIT_LOG_PATH, "audit log"),
        (EVAL_REPORT_PATH, "eval report"),
    ]:
        if not os.path.exists(path):
            print(f"ERROR: {label} not found at '{path}'")
            print("Run generate_data_v2.py and executor_v2.py first, "
                  "or check that this script's paths match your folder layout.")
            return

    template_path = find_template()
    if template_path is None:
        print(f"ERROR: dashboard_v2_template.html not found in any of: {TEMPLATE_CANDIDATES}")
        print("Make sure the template file is in src/ or the project root.")
        return

    with open(AUDIT_LOG_PATH, encoding="utf-8") as f:
        audit_entries = [json.loads(line) for line in f]

    with open(EVAL_REPORT_PATH, encoding="utf-8") as f:
        eval_report = json.load(f)

    with open(template_path, encoding="utf-8") as f:
        template = f.read()

    output = (
        template
        .replace("__AUDIT_DATA__", json.dumps(audit_entries))
        .replace("__EVAL_DATA__", json.dumps(eval_report))
    )

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(output)

    print(f"Dashboard rebuilt from {len(audit_entries)} records -> {OUTPUT_PATH}")
    print(f"Template used: {template_path}")
    print(f"Recovery rate in this build: {eval_report.get('recovery_rate_pct')}%")
    print("Open that file in your browser to see the updated dashboard.")


if __name__ == "__main__":
    main()