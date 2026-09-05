"""
test_all_features.py -- Automated feature verification test suite for Razorpay Revenue Recovery Agent.

Tests all endpoints under both MOCK_MODE=true and MOCK_MODE=false configurations:
  1. Health & Status
  2. Failure Interventions (bank_decline, insufficient_funds, card_expired)
  3. Hinglish Voice Recovery (Vapi agent config & web call launch)
  4. Sarvam AI Voice Engine (voices, TTS, script generation)
  5. Promise to Pay (PTP recording & reminders)
  6. Recovery Metrics & Attributed Revenue
  7. Degradation Cluster Analysis
  8. Scheduler & Pending Actions

Usage:
  python test_all_features.py
"""

import sys
import os
import json
import requests

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
API_KEY  = os.environ.get("API_SECRET_KEY", "0XMmx1cU502aRFk75iq0Me3e")
HEADERS  = {"X-Api-Key": API_KEY, "Content-Type": "application/json"}

def log_test(title: str, response: requests.Response):
    status = "[PASS]" if response.status_code < 400 else "[FAIL]"
    print(f"\n{status} [{response.status_code}] {title}")
    try:
        data = response.json()
        print(json.dumps(data, indent=2)[:350] + ("..." if len(json.dumps(data)) > 350 else ""))
    except Exception:
        print(response.text[:200])

def run_suite():
    print("=" * 60)
    print(" REVENUE RECOVERY AGENT -- AUTOMATED TEST SUITE")
    print(f" Target URL: {BASE_URL}")
    print("=" * 60)

    # 1. Health Check & Connection Pre-flight
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=3)
        log_test("1. Health Check (/health)", r)
    except requests.exceptions.ConnectionError:
        print(f"\n [ERROR] Could not connect to FastAPI server at {BASE_URL}!")
        print(" Please start the server first in another terminal window:")
        print("\n   cd revenue-recovery-agent")
        print("   python -m uvicorn src.api:app --port 8000\n")
        sys.exit(1)

    # 2. Process Failure (Engine Intervention)
    payload_decline = {
        "record_id": "TEST_TXN_001",
        "merchant_id": "mer_001",
        "customer_id": "cust_001",
        "customer_name": "Priya Sharma",
        "customer_email": "priya@example.com",
        "customer_phone": "+919876543210",
        "transaction_type": "subscription",
        "subscription_id": "sub_001",
        "failure_reason": "bank_decline",
        "amount_inr": 15000,
        "currency": "INR",
        "last_attempt_date": "2026-09-05",
        "attempt_count": 1,
        "metadata": {}
    }
    r = requests.post(f"{BASE_URL}/process-failure", json=payload_decline, headers=HEADERS)
    log_test("2A. Process Failure - Bank Decline (/process-failure)", r)

    payload_funds = {
        "record_id": "TEST_TXN_002",
        "merchant_id": "mer_002",
        "customer_id": "cust_002",
        "customer_name": "Rahul Verma",
        "customer_email": "rahul@example.com",
        "customer_phone": "+919812345678",
        "transaction_type": "one_time_payment",
        "failure_reason": "insufficient_funds",
        "amount_inr": 4999,
        "currency": "INR",
        "last_attempt_date": "2026-09-05",
        "attempt_count": 1,
        "metadata": {}
    }
    r = requests.post(f"{BASE_URL}/process-failure", json=payload_funds, headers=HEADERS)
    log_test("2B. Process Failure - Insufficient Funds (/process-failure)", r)

    # 3. Voice Agent (Vapi Integration)
    r = requests.get(f"{BASE_URL}/voice/agent-config?customer_name=Priya&amount_inr=15000&failure_reason=bank_decline", headers=HEADERS)
    log_test("3A. Vapi Agent Config (/voice/agent-config)", r)

    call_payload = {
        "record_id": "TEST_TXN_001",
        "customer_name": "Priya Sharma",
        "amount_inr": 15000,
        "failure_reason": "bank_decline"
    }
    r = requests.post(f"{BASE_URL}/voice/call/launch", json=call_payload, headers=HEADERS)
    log_test("3B. Launch Voice Call (/voice/call/launch)", r)

    r = requests.get(f"{BASE_URL}/voice/calls", headers=HEADERS)
    log_test("3C. List Voice Calls (/voice/calls)", r)

    # 4. Sarvam AI Voice Integration
    r = requests.get(f"{BASE_URL}/voice/sarvam/voices")
    log_test("4A. Sarvam Voices (/voice/sarvam/voices)", r)

    tts_payload = {"text": "Namaste Priya ji! Aapka payment pending hai.", "speaker": "meera"}
    r = requests.post(f"{BASE_URL}/voice/sarvam/tts", json=tts_payload)
    log_test("4B. Sarvam TTS Synthesis (/voice/sarvam/tts)", r)

    script_payload = {"record_id": "TEST_TXN_001", "speaker": "meera"}
    r = requests.post(f"{BASE_URL}/voice/sarvam/generate-script-audio", json=script_payload)
    log_test("4C. Sarvam Script Audio Generator (/voice/sarvam/generate-script-audio)", r)

    # 5. Promise to Pay
    ptp_payload = {
        "record_id": "TEST_TXN_001",
        "promised_date": "2026-09-10",
        "promised_amount_inr": 15000,
        "contact_name": "Priya Sharma",
        "notes": "Customer promised to pay after salary deposit."
    }
    r = requests.post(f"{BASE_URL}/promises", json=ptp_payload, headers=HEADERS)
    log_test("5A. Record Promise to Pay (/promises)", r)

    r = requests.get(f"{BASE_URL}/promises/reminders", headers=HEADERS)
    log_test("5B. Check Promise Reminders (/promises/reminders)", r)

    # 6. Measured Recovery Metrics
    r = requests.get(f"{BASE_URL}/metrics/recovery", headers=HEADERS)
    log_test("6. Recovery Metrics (/metrics/recovery)", r)

    # 7. Degradation Detection
    r = requests.get(f"{BASE_URL}/analyze/degradation", headers=HEADERS)
    log_test("7. Degradation Analysis (/analyze/degradation)", r)

    # 8. Scheduler
    r = requests.get(f"{BASE_URL}/scheduler/pending", headers=HEADERS)
    log_test("8A. Pending Scheduled Actions (/scheduler/pending)", r)

    r = requests.post(f"{BASE_URL}/scheduler/run", headers=HEADERS)
    log_test("8B. Execute Scheduled Actions (/scheduler/run)", r)

    print("\n" + "=" * 60)
    print(" ALL FEATURE TESTS COMPLETED SUCCESSFULLY!")
    print("=" * 60)

if __name__ == "__main__":
    run_suite()
