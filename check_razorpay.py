import os
import json
import razorpay
try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(usecwd=True), override=True)
except ImportError:
    pass

key_id = os.environ.get("RAZORPAY_KEY_ID")
key_secret = os.environ.get("RAZORPAY_KEY_SECRET")

if not key_id or not key_secret:
    print("[ERROR] RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are not set in .env or environment!")
    print("Please add the following lines to your .env file:")
    print("  RAZORPAY_KEY_ID=rzp_test_YOUR_KEY_ID")
    print("  RAZORPAY_KEY_SECRET=YOUR_KEY_SECRET")
    print("  MOCK_MODE=false")
    exit(1)

print(f"Connecting to Razorpay API with Key ID: {key_id[:15]}...")
try:
    c = razorpay.Client(auth=(key_id, key_secret))
    links = c.payment_link.all({"count": 5})
    total = links.get("count", len(links.get("items", [])))
    print(f"[OK] Authenticated successfully! Total payment links found: {total}")

    items = links.get("items", [])
    for i, l in enumerate(items[:5]):
        lid = l.get("id", "?")
        amt = l.get("amount", 0) // 100
        desc = (l.get("description", "-") or "-")[:60]
        status = l.get("status", "unknown")
        short_url = l.get("short_url", "")
        print(f"  {i+1}. {lid} [{status}] | INR {amt} | {desc} | {short_url}")
except Exception as e:
    print(f"[ERROR] Razorpay API call failed: {e}")
    exit(1)

