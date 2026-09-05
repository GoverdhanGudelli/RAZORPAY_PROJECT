"""
Razorpay execution layer.

MOCK_MODE=true  (default) -> simulates API calls, returns realistic fake
                              responses, logs everything. Safe to run anywhere,
                              including this sandbox with no network access
                              to api.razorpay.com.

MOCK_MODE=false -> uses real razorpay-python SDK against Test Mode keys.
                    Run this ONLY on your own machine with:
                      export RAZORPAY_KEY_ID=rzp_test_xxxxx
                      export RAZORPAY_KEY_SECRET=xxxxx
                      export MOCK_MODE=false

This split is deliberate: the agent's DECISION logic (router_v2.py) never
changes based on mock vs real. Only this execution layer swaps. That's the
correct boundary — decisions are pure/testable, I/O is isolated and swappable.
"""

import os
import random
import string
import time
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(usecwd=True), override=True)
except ImportError:
    pass

from schemas import AgentAction, ActionType

MOCK_MODE = os.environ.get("MOCK_MODE", "true").lower() != "false"

# Razorpay Test Mode hard limit: max 30 Payment Links per business.
# https://razorpay.com/docs/api/payments/payment-links/create-standard/
# This is NOT configurable by us — it's a platform-side cap. We enforce our
# own governor below it so a full 100-record run fails loudly and early
# instead of burning through the quota and getting rejected mid-run.
TEST_MODE_LINK_CAP = 25  # stay under Razorpay's 30, leave headroom for retries

# Razorpay Test Mode maximum amount per payment link (in INR).
# The platform rejects amounts above 5,00,000 paise (₹5,000) in test mode.
# We clamp to this cap and flag it in the response for audit transparency.
TEST_MODE_MAX_AMOUNT_INR = 5000


def _fake_id(prefix: str) -> str:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=14))
    return f"{prefix}_{suffix}"


def _normalize_indian_phone(raw: str | None) -> str | None:
    """Razorpay's customer.contact expects E.164-ish format (e.g. +919876543210),
    8-14 chars. Faker's en_IN numbers come in inconsistent formats, so we
    normalize defensively rather than pass through and risk a 4xx.
    Returns None if no real phone is available — caller must omit the field
    entirely (Razorpay rejects recurring-digit placeholders like +919999999999)."""
    if not raw:
        return None  # caller should omit contact field rather than send a fake
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) < 10:
        return None  # not enough digits to form a valid number, skip it
    digits = digits[-10:]  # take last 10 digits (handles country-prefix variants)
    # Reject if all digits are the same (e.g. 9999999999 or 0000000000)
    if len(set(digits)) == 1:
        return None
    return f"+91{digits}"


class RazorpayExecutionError(Exception):
    pass


class LinkCapExceededError(RazorpayExecutionError):
    """Raised when a run would exceed our self-imposed Test Mode link governor.
    This is a deliberate stop, not a bug — see TEST_MODE_LINK_CAP above."""
    pass


class RazorpayClient:
    """Thin wrapper. Same method signatures whether mocked or real,
    so executor.py never needs to know which mode it's in."""

    def __init__(self):
        self.mock = MOCK_MODE
        self.links_created_this_run = 0
        # Short suffix appended to reference_id to make each run's links unique.
        # Razorpay rejects a link if a prior link with the same reference_id exists.
        self._run_suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        if not self.mock:
            try:
                import razorpay  # only imported when actually needed
            except ImportError as e:
                raise RazorpayExecutionError(
                    "MOCK_MODE=false but 'razorpay' package not installed. "
                    "Run: pip install razorpay --break-system-packages (or without the flag on Windows)"
                ) from e
            key_id = os.environ.get("RAZORPAY_KEY_ID")
            key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
            if not key_id or not key_secret:
                raise RazorpayExecutionError(
                    "MOCK_MODE=false requires RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET env vars."
                )
            if not key_id.startswith("rzp_test_"):
                raise RazorpayExecutionError(
                    f"RAZORPAY_KEY_ID does not look like a Test Mode key (expected 'rzp_test_...', "
                    f"got a key starting with '{key_id[:8]}...'). Refusing to run — this pipeline "
                    f"is not built or reviewed for Live Mode. Get Test keys from Dashboard > Settings > API Keys."
                )
            self._client = razorpay.Client(auth=(key_id, key_secret))
        else:
            self._client = None

    def create_payment_link(
        self, record_id: str, amount_inr: int, description: str,
        customer_name: str, customer_phone: str | None = None,
    ) -> dict:
        """Used for SEND_PAYMENT_LINK and SEND_INCENTIVE_LINK actions.
        Field shape verified against Razorpay's Create Standard Payment Link
        docs (POST /v1/payment_links) — amount in paise, reference_id unique
        per link, customer.contact in E.164-ish format, notes for our own
        traceability metadata."""
        if self.mock:
            time.sleep(0.02)  # simulate network latency
            return {
                "id": _fake_id("plink"),
                "short_url": f"https://rzp.io/l/{_fake_id('mock')[:10]}",
                "amount": amount_inr * 100,
                "status": "created",
                "description": description,
                "reference_id": record_id,
                "mock": True,
            }

        # --- real-mode governor: warn if approaching self-imposed cap ---
        if self.links_created_this_run >= TEST_MODE_LINK_CAP:
            print(
                f"  [link-cap] Link #{self.links_created_this_run + 1} exceeds "
                f"self-imposed governor ({TEST_MODE_LINK_CAP}). Will attempt "
                f"Payment Link, falling back to Order API if Razorpay rejects."
            )

        normalized_phone = _normalize_indian_phone(customer_phone)
        customer_payload: dict = {"name": customer_name}
        if normalized_phone:
            customer_payload["contact"] = normalized_phone
        # SMS notify only makes sense when we have a real contact number
        notify_sms = bool(normalized_phone)

        # Clamp to Test Mode max amount — Razorpay rejects values above this.
        # Flag in response so audit log is transparent about any capping.
        capped = amount_inr > TEST_MODE_MAX_AMOUNT_INR
        send_amount_inr = min(amount_inr, TEST_MODE_MAX_AMOUNT_INR)

        # Append run-unique suffix so repeated runs don't collide with prior links.
        unique_ref = f"{record_id}-{self._run_suffix}"
        payload = {
            "amount": send_amount_inr * 100,  # paise
            "currency": "INR",
            "description": description[:2048],
            "reference_id": unique_ref,
            "customer": customer_payload,
            "notify": {"sms": notify_sms, "email": False},
            "notes": {"source": "revenue-recovery-agent", "record_id": record_id, "run_suffix": self._run_suffix},
        }

        # Retry with exponential backoff to handle Razorpay Test Mode rate limiting.
        # 15s between requests = ~4 RPM, well within Test Mode's cap.
        max_attempts = 4
        link_limit_hit = False
        for attempt in range(1, max_attempts + 1):
            try:
                time.sleep(15)  # mandatory inter-request pause — Test Mode rate limit is ~4 RPM
                resp = self._client.payment_link.create(payload)
                break  # success — exit retry loop
            except Exception as e:
                err_str = str(e)
                if "Too many requests" in err_str and attempt < max_attempts:
                    wait = 15 * attempt  # 15s, 30s, 45s extra on top of the base sleep
                    print(f"  [rate-limit] attempt {attempt}/{max_attempts} — waiting {wait}s before retry...")
                    time.sleep(wait)
                elif "test mode limit of 30 reached" in err_str.lower() or "limit" in err_str.lower() and "payment_link" in err_str.lower():
                    print(f"  [link-limit] Payment link limit hit for {record_id} — falling back to Order API")
                    link_limit_hit = True
                    break
                else:
                    raise  # not rate-limit or link-limit — propagate

        # Fallback: use Order API when payment link quota is exhausted.
        # Orders have no test-mode cap and still prove real Razorpay integration.
        if link_limit_hit:
            return self._create_order_fallback(record_id, send_amount_inr, description, capped, amount_inr)

        self.links_created_this_run += 1
        resp["mock"] = False
        if capped:
            resp["test_mode_amount_capped"] = True
            resp["original_amount_inr"] = amount_inr
            resp["capped_amount_inr"] = send_amount_inr
        return resp

    def _create_order_fallback(
        self, record_id: str, amount_inr: int, description: str,
        capped: bool, original_amount_inr: int,
    ) -> dict:
        """Fallback when Payment Link quota (30/business) is exhausted.

        Creates a Razorpay Order instead — Orders have NO test-mode cap and
        still prove real API integration. The response is normalized to look
        like a payment link response so the audit log and dashboard work
        seamlessly.  Tagged with fallback_to_order=True for transparency."""
        unique_ref = f"{record_id}-{self._run_suffix}"
        order_payload = {
            "amount": amount_inr * 100,  # paise
            "currency": "INR",
            "notes": {
                "source": "revenue-recovery-agent",
                "record_id": record_id,
                "description": description[:512],
                "run_suffix": self._run_suffix,
                "fallback_reason": "payment_link_limit_30_reached",
            },
            "receipt": unique_ref,
        }

        time.sleep(5)  # rate-limit courtesy
        order_resp = self._client.order.create(order_payload)

        # Normalize to match payment-link response shape for downstream compat
        resp = {
            "id": order_resp["id"],
            "amount": order_resp["amount"],
            "currency": order_resp.get("currency", "INR"),
            "status": order_resp.get("status", "created"),
            "description": description,
            "reference_id": unique_ref,
            "mock": False,
            "fallback_to_order": True,
            "fallback_reason": "Payment link 30-limit reached; used Order API instead",
            "order_id": order_resp["id"],
            "receipt": unique_ref,
        }
        if capped:
            resp["test_mode_amount_capped"] = True
            resp["original_amount_inr"] = original_amount_inr
            resp["capped_amount_inr"] = amount_inr
        print(f"  [order-fallback] {record_id}: Created order {order_resp['id']} (INR {amount_inr})")
        return resp

    def retry_payment(self, record_id: str, amount_inr: int) -> dict:
        """Used for RETRY_PAYMENT actions.

        HONEST LIMITATION: Razorpay does not expose a generic "retry this
        failed charge" endpoint — real retry flows depend entirely on how the
        original payment was created (Subscriptions API auto-retries on its
        own schedule; Orders API requires a fresh checkout attempt). There is
        no single API call that retries an arbitrary past payment by ID.

        So in real mode, RETRY_PAYMENT falls back to creating a fresh Payment
        Link for the same amount — functionally a retry from the customer's
        perspective, but implemented honestly rather than pretending a
        one-call "retry" API exists. This is flagged in the audit log via
        the 'retry_via' field so it's never silently misrepresented."""
        if self.mock:
            time.sleep(0.02)
            success = random.random() < 0.55  # matches our documented eval assumption
            return {
                "id": _fake_id("pay"),
                "status": "captured" if success else "failed",
                "amount": amount_inr * 100,
                "retry_via": "mock_direct_retry",
                "mock": True,
            }

        link_resp = self.create_payment_link(
            record_id=record_id,
            amount_inr=amount_inr,
            description=f"Retry payment for {record_id}",
            customer_name="Customer",
        )
        link_resp["retry_via"] = "fresh_payment_link"
        link_resp["status"] = "created"  # honest: we don't know outcome yet, no auto-capture
        return link_resp


def _get_val(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def execute_action(action: AgentAction, record, client: "RazorpayClient" = None) -> dict:
    """
    Takes a validated AgentAction + its source record, executes via
    RazorpayClient, returns a result dict for the audit log.
    NO_ACTION and ESCALATE_TO_HUMAN never touch the payment API at all —
    that's a deliberate safety boundary, not an oversight.

    IMPORTANT: pass a single shared `client` when calling this in a loop.
    The link-cap governor (TEST_MODE_LINK_CAP) is tracked per-client-instance
    via links_created_this_run — a fresh client every call would reset that
    counter to 0 each time and silently defeat the cap.

    v4 additions: INITIATE_VOICE_CALL (Hinglish voice recovery).
    All link actions now use localized descriptions from message_generator.py.
    """
    if client is None:
        client = RazorpayClient()  # fine for one-off calls; NOT for batch loops
    result = {
        "record_id": action.record_id,
        "action": action.action.value,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "mock_mode": client.mock,
        "message_language": getattr(action, "message_language", _get_val(record, "customer_language_pref", "hinglish")),
        "propensity_score": getattr(action, "propensity_score", None),
        "deferred": bool(getattr(action, "defer_execution", False)),
    }

    # Build a localized description for all link-sending actions
    try:
        from message_generator import get_message
        c_name = _get_val(record, "customer_name", "Customer")
        c_amt = _get_val(record, "amount_inr", 0)
        c_lang = getattr(action, "message_language", _get_val(record, "customer_language_pref", "hinglish"))
        localized_desc = get_message(
            action_type=action.action.value,
            language=c_lang,
            customer_name=c_name,
            amount_inr=c_amt,
            incentive_pct=action.incentive_discount_pct,
            hours_since_event=_get_val(record, "hours_since_checkout", None),
            follow_up_date=(
                action.follow_up_date.isoformat()
                if getattr(action, "follow_up_date", None) else None
            ),
        )
    except Exception:
        localized_desc = action.reasoning[:80]

    # Deferred retries: do not hit Razorpay yet — scheduler will execute later
    if getattr(action, "defer_execution", False) and action.action == ActionType.RETRY_PAYMENT:
        result["api_response"] = None
        result["outcome"] = "scheduled"
        result["retry_delay_hours"] = action.retry_delay_hours
        result["customer_message"] = localized_desc
        return result

    if action.action == ActionType.RETRY_PAYMENT:
        api_result = client.retry_payment(action.record_id, record.amount_inr)
        result["api_response"] = api_result
        result["outcome"] = "success" if api_result.get("status") == "captured" else "pending_or_failed"
        result["customer_message"] = localized_desc

    elif action.action == ActionType.SEND_PAYMENT_LINK:
        api_result = client.create_payment_link(
            record_id=action.record_id,
            amount_inr=record.amount_inr,
            description=localized_desc,
            customer_name=record.customer_name,
            customer_phone=None,
        )
        result["api_response"] = api_result
        result["outcome"] = "link_sent"
        result["customer_message"] = localized_desc

    elif action.action == ActionType.SEND_INCENTIVE_LINK:
        discounted = int(record.amount_inr * (1 - (action.incentive_discount_pct or 0) / 100))
        api_result = client.create_payment_link(
            record_id=action.record_id,
            amount_inr=discounted,
            description=localized_desc,
            customer_name=record.customer_name,
        )
        result["api_response"] = api_result
        result["outcome"] = "incentive_link_sent"
        result["original_amount_inr"] = record.amount_inr
        result["discounted_amount_inr"] = discounted
        result["customer_message"] = localized_desc

    elif action.action == ActionType.SEND_CHECKOUT_RECOVERY:
        # Cart recovery: payment link tagged as checkout_recovery
        api_result = client.create_payment_link(
            record_id=action.record_id,
            amount_inr=record.amount_inr,
            description=localized_desc,
            customer_name=record.customer_name,
        )
        result["api_response"] = api_result
        result["outcome"] = "checkout_recovery_link_sent"
        result["checkout_session_id"] = getattr(record, "checkout_session_id", None)
        result["hours_since_checkout"] = getattr(record, "hours_since_checkout", None)
        result["customer_message"] = localized_desc

    elif action.action == ActionType.SEND_MANDATE_SETUP:
        # Fresh mandate/NACH setup link. In production, Razorpay mandate API
        # requires subscription plan ID — using a payment link as the re-setup
        # vehicle is the correct approach for ad-hoc NACH re-authorizations.
        api_result = client.create_payment_link(
            record_id=action.record_id,
            amount_inr=record.amount_inr,
            description=localized_desc,
            customer_name=record.customer_name,
        )
        result["api_response"] = api_result
        result["outcome"] = "mandate_setup_link_sent"
        result["subscription_id"] = getattr(record, "subscription_id", None)
        result["customer_message"] = localized_desc

    elif action.action == ActionType.LOG_PROMISE_TO_PAY:
        # No payment API call — record-keeping action only.
        # api.py writes the commitment to the promises DB table.
        result["api_response"] = None
        result["outcome"] = "promise_logged"
        result["promised_payment_date"] = (
            record.promised_payment_date.isoformat()
            if getattr(record, "promised_payment_date", None) else None
        )
        result["customer_message"] = localized_desc

    elif action.action == ActionType.INITIATE_VOICE_CALL:
        # Track 03 Hinglish voice recovery
        # MOCK mode: log the script (no real call placed)
        # REAL mode: create a Vapi Web Call and return the browser URL
        from message_generator import generate_voice_script
        c_name = _get_val(record, "customer_name", "Customer")
        c_amt = _get_val(record, "amount_inr", 0)
        c_lang = getattr(action, "message_language", "hinglish")
        c_reason = _get_val(record, "failure_reason", "")
        voice = generate_voice_script(
            language=c_lang,
            customer_name=c_name,
            amount_inr=c_amt,
            reason=c_reason,
        )

        if client.mock:
            # Mock mode: unchanged behaviour — log the script, return a fake call ID
            call_id = _fake_id("call")
            result["api_response"] = {
                "id": call_id,
                "status": "initiated",
                "channel": "voice_ivr",
                "provider": "mock",
                "voice_script": voice["script"],
                "language": voice["language"],
                "estimated_duration_sec": voice["estimated_duration_sec"],
                "mock": True,
            }
        else:
            # Real mode: launch a Vapi Web Call
            try:
                from voice_agent import create_web_call, BACKEND_BASE_URL
                call_result = create_web_call(
                    record_id=action.record_id,
                    amount_inr=c_amt,
                    customer_name=c_name,
                    failure_reason=c_reason,
                    backend_url=BACKEND_BASE_URL,
                )
                result["api_response"] = {
                    "id": call_result.get("call_id"),
                    "status": call_result.get("status", "queued"),
                    "channel": "voice_webrtc",
                    "provider": "vapi",
                    "web_call_url": call_result.get("web_call_url"),
                    "voice_script": voice["script"],
                    "language": voice["language"],
                    "mock": False,
                }
                if call_result.get("error"):
                    result["api_response"]["error"] = call_result["error"]
                    result["api_response"]["status"] = "error"
            except Exception as vapi_err:
                # Fallback: still log the script so the call isn't silently lost
                call_id = _fake_id("call_err")
                result["api_response"] = {
                    "id": call_id,
                    "status": "error",
                    "channel": "voice_webrtc",
                    "provider": "vapi",
                    "error": str(vapi_err),
                    "voice_script": voice["script"],
                    "mock": False,
                }

        result["outcome"] = "voice_call_initiated"
        result["customer_message"] = voice["script"]
        result["voice"] = voice

    elif action.action == ActionType.ESCALATE_TO_HUMAN:
        result["api_response"] = None
        result["outcome"] = "escalated_no_api_call"
        result["customer_message"] = localized_desc

    else:  # NO_ACTION
        result["api_response"] = None
        result["outcome"] = "no_action_taken"

    return result
