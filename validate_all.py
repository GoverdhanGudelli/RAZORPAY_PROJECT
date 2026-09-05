"""
Full end-to-end validation of all Revenue Recovery Agent API endpoints.
Tests every Track 03 feature in mock mode to confirm everything works
before switching to real Razorpay keys.
"""
import requests
import json
import time

BASE = "http://127.0.0.1:8000"
API_KEY = "SAN5XkAQDyy65VVr5sg65x8B"
HEADERS = {"X-Api-Key": API_KEY, "Content-Type": "application/json"}

results = []

def test(name, method, path, expected_status=200, json_body=None):
    url = f"{BASE}{path}"
    try:
        if method == "GET":
            r = requests.get(url, headers=HEADERS, timeout=10)
        elif method == "POST":
            r = requests.post(url, headers=HEADERS, json=json_body, timeout=10)
        elif method == "PATCH":
            r = requests.patch(url, headers=HEADERS, json=json_body, timeout=10)
        
        status = "PASS" if r.status_code == expected_status else "FAIL"
        results.append((name, status, r.status_code))
        
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:100]
        print(f"  [{status}] {name}")
        print(f"         {method} {path} -> {r.status_code}")
        if status == "FAIL":
            print(f"         Expected {expected_status}, got {r.status_code}")
            print(f"         Body: {json.dumps(body, indent=2, default=str)[:200]}")
        else:
            # Print key info from response
            if isinstance(body, dict):
                keys = list(body.keys())[:6]
                print(f"         Keys: {keys}")
        print()
        return body
    except Exception as e:
        results.append((name, "ERROR", str(e)[:80]))
        print(f"  [ERROR] {name}: {e}")
        print()
        return None


print("=" * 60)
print("REVENUE RECOVERY AGENT - FULL VALIDATION")
print("=" * 60)
print()

# -------------------------------------------------------
# 1. Process a failed transaction (Track 03: core agent)
# -------------------------------------------------------
print("--- FEATURE 1: Process Failed Transaction ---")
test("Process checkout abandonment (DEMO001)", "POST", "/process-failure", 200, {
    "record_id": "TEST_CHECKOUT",
    "transaction_type": "checkout_abandonment",
    "failure_reason": "cart_abandoned",
    "amount_inr": 4999,
    "attempt_count": 0,
    "last_attempt_date": "2026-09-04",
    "customer_name": "Priya Sharma",
    "customer_language_pref": "hinglish",
    "past_recovery_success": True,
    "checkout_session_id": "order_test_01",
    "hours_since_checkout": 2
})

test("Process subscription failure (DEMO002)", "POST", "/process-failure", 200, {
    "record_id": "TEST_SUB",
    "transaction_type": "subscription",
    "failure_reason": "insufficient_funds",
    "amount_inr": 499,
    "attempt_count": 0,
    "last_attempt_date": "2026-09-03",
    "customer_name": "Arjun Mehta",
    "customer_language_pref": "hi",
    "payment_method": "upi_autopay",
    "subscription_id": "SUB_TEST_02",
    "days_past_due": 3
})

test("Process mandate revoked (DEMO003)", "POST", "/process-failure", 200, {
    "record_id": "TEST_MANDATE",
    "transaction_type": "subscription",
    "failure_reason": "mandate_revoked",
    "amount_inr": 799,
    "attempt_count": 0,
    "last_attempt_date": "2026-09-03",
    "customer_name": "Neha Kapoor",
    "customer_language_pref": "hinglish",
    "payment_method": "upi_autopay",
    "past_recovery_success": True,
    "subscription_id": "SUB_TEST_03",
    "days_past_due": 2
})

test("Process B2B invoice (DEMO004)", "POST", "/process-failure", 200, {
    "record_id": "TEST_B2B",
    "transaction_type": "b2b_invoice",
    "failure_reason": "invoice_overdue",
    "amount_inr": 85000,
    "attempt_count": 1,
    "last_attempt_date": "2026-08-28",
    "customer_name": "Vikram Reddy",
    "customer_language_pref": "en",
    "past_recovery_success": True,
    "invoice_id": "INV_TEST_04",
    "due_date": "2026-08-10",
    "po_reference": "PO-TEST04",
    "counterparty_business_name": "TechNova Solutions Pvt Ltd"
})

test("Process bank downtime (DEMO007)", "POST", "/process-failure", 200, {
    "record_id": "TEST_DOWNTIME",
    "transaction_type": "one_time_payment",
    "failure_reason": "bank_downtime",
    "amount_inr": 3500,
    "attempt_count": 1,
    "last_attempt_date": "2026-09-04",
    "customer_name": "Meera Iyer",
    "customer_language_pref": "en",
    "payment_method": "netbanking"
})

# -------------------------------------------------------
# 2. Payment Degradation Detection
# -------------------------------------------------------
print("--- FEATURE 2: Payment Degradation Detection ---")
test("Detect degradation patterns", "GET", "/analyze/degradation")

# -------------------------------------------------------
# 3. Scheduler (deferred retries)
# -------------------------------------------------------
print("--- FEATURE 3: Scheduler ---")
test("List pending scheduled actions", "GET", "/scheduler/pending")
test("Run scheduled retries", "POST", "/scheduler/run")

# -------------------------------------------------------
# 4. Promise-to-Pay
# -------------------------------------------------------
print("--- FEATURE 4: Promise-to-Pay ---")
promise_resp = test("Log a promise-to-pay", "POST", "/promises", 200, {
    "record_id": "TEST_CHECKOUT",
    "contact_name": "Priya Sharma",
    "promised_amount_inr": 4999,
    "promised_date": "2026-09-06",
    "notes": "Customer promised to pay by Friday"
})

test("List promise reminders (24h)", "GET", "/promises/reminders")
test("List overdue promises", "GET", "/promises/overdue")

if promise_resp and isinstance(promise_resp, dict) and "promise_id" in promise_resp:
    pid = promise_resp["promise_id"]
    test(f"Update promise status", "PATCH", f"/promises/{pid}", 200, {
        "status": "fulfilled"
    })

# -------------------------------------------------------
# 5. Recovery Metrics (The Bar)
# -------------------------------------------------------
print("--- FEATURE 5: Recovery Metrics (The Bar) ---")
test("Get measured recovery metrics", "GET", "/metrics/recovery")

# -------------------------------------------------------
# 6. Webhook endpoint (signature check - will fail without valid sig, expect 401)
# -------------------------------------------------------
print("--- FEATURE 6: Webhook Endpoint ---")
test("Webhook without signature (expect 401)", "POST", "/webhooks/razorpay", 401, {
    "event": "payment.captured",
    "payload": {"payment": {"entity": {"id": "pay_test123", "amount": 100}}}
})

# -------------------------------------------------------
# Summary
# -------------------------------------------------------
print("=" * 60)
print("VALIDATION SUMMARY")
print("=" * 60)
passed = sum(1 for _, s, _ in results if s == "PASS")
failed = sum(1 for _, s, _ in results if s == "FAIL")
errors = sum(1 for _, s, _ in results if s == "ERROR")
total = len(results)

print(f"\n  Total: {total}  |  PASS: {passed}  |  FAIL: {failed}  |  ERROR: {errors}")
print()

for name, status, code in results:
    icon = "PASS" if status == "PASS" else ("FAIL" if status == "FAIL" else "ERR ")
    print(f"  [{icon}] {name} ({code})")

print()
if failed == 0 and errors == 0:
    print("  ALL TESTS PASSED - Ready for real keys!")
else:
    print("  SOME TESTS FAILED - Fix before using real keys")
print()
