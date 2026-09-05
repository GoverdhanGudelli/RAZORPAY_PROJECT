"""
test_real_razorpay.py -- Send demo transactions to generate real Razorpay Payment Links.

Requires:
  .env file containing:
    MOCK_MODE=false
    RAZORPAY_KEY_ID=rzp_test_...
    RAZORPAY_KEY_SECRET=...
"""

import os
import sys
import json
from pathlib import Path

# Add src to path
ROOT = Path(__file__).parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(usecwd=True), override=True)
except ImportError:
    pass

key_id = os.environ.get("RAZORPAY_KEY_ID")
key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
mock_mode = os.environ.get("MOCK_MODE", "true").lower() != "false"

print("=" * 60)
print("  RAZORPAY REAL PAYMENT LINK GENERATOR")
print("=" * 60)
print(f"MOCK_MODE           : {mock_mode}")
print(f"RAZORPAY_KEY_ID     : {key_id[:15] if key_id else 'NOT SET'}")

if mock_mode or not key_id or not key_secret or key_id == "rzp_test_YOUR_KEY_ID":
    print("\n[ERROR] Real Razorpay test keys are required in .env!")
    print("Please edit .env file and set:")
    print("  MOCK_MODE=false")
    print("  RAZORPAY_KEY_ID=rzp_test_YOUR_ACTUAL_KEY_ID")
    print("  RAZORPAY_KEY_SECRET=YOUR_ACTUAL_KEY_SECRET")
    sys.exit(1)

from razorpay_client import RazorpayClient, execute_action
from router_v2 import route
from schemas import TransactionFailure, ActionType

demo_failures = [
    {
        "record_id": "DEMO_REAL_001",
        "merchant_id": "mer_demo",
        "customer_id": "cust_priya",
        "customer_name": "Priya Sharma",
        "customer_email": "priya.sharma@example.com",
        "customer_phone": "+919876543210",
        "transaction_type": "one_time_payment",
        "failure_reason": "insufficient_funds",
        "amount_inr": 1499,
        "currency": "INR",
        "attempt_count": 1,
        "last_attempt_date": "2026-09-05",
        "past_recovery_success": False,
    },
    {
        "record_id": "DEMO_REAL_002",
        "merchant_id": "mer_demo",
        "customer_id": "cust_rahul",
        "customer_name": "Rahul Verma",
        "customer_email": "rahul.verma@example.com",
        "customer_phone": "+919812345678",
        "transaction_type": "subscription",
        "subscription_id": "sub_demo_002",
        "failure_reason": "card_expired",
        "amount_inr": 2999,
        "currency": "INR",
        "attempt_count": 1,
        "last_attempt_date": "2026-09-05",
        "past_recovery_success": False,
    },
    {
        "record_id": "DEMO_REAL_003",
        "merchant_id": "mer_demo",
        "customer_id": "cust_ananya",
        "customer_name": "Ananya Roy",
        "customer_email": "ananya.roy@example.com",
        "customer_phone": "+919988776655",
        "transaction_type": "checkout_abandonment",
        "failure_reason": "cart_abandoned",
        "amount_inr": 4999,
        "currency": "INR",
        "attempt_count": 1,
        "last_attempt_date": "2026-09-05",
        "past_recovery_success": True,
    }
]

print("\nGenerating Payment Links via Razorpay Test API...\n")
client = RazorpayClient()

for i, failure_data in enumerate(demo_failures, 1):
    failure = TransactionFailure(**failure_data)
    action = route(failure)
    print(f"[{i}] Record ID    : {failure.record_id}")
    print(f"    Customer     : {failure.customer_name}")
    print(f"    Reason       : {failure.failure_reason.value}")
    print(f"    Routed Action: {action.action.value}")
    
    result = execute_action(action, failure, client=client)
    api_resp = result.get("api_response", {})
    
    payment_link = api_resp.get("short_url") or api_resp.get("url") or "N/A"
    link_id = api_resp.get("id") or "N/A"
    
    print(f"    Status       : {result.get('outcome')}")
    print(f"    Link ID      : {link_id}")
    print(f"    Payment Link : {payment_link}")
    print("-" * 60)

print("\nDone! Open any of the links above to test real payments on Razorpay Test Checkout.")
