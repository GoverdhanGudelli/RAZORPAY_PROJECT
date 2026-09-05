"""
message_generator.py — Multilingual recovery message templates.

Generates customer-facing recovery messages in three language modes:
  "en"       — Standard English
  "hi"       — Hindi (Devanagari script)
  "hinglish" — Hindi-English code-switch (dominant mode in Indian SMS/WhatsApp)

The customer_language_pref field on TransactionFailure drives which template is
selected. This field already exists in the schema — no migration needed.

Generated messages are used as:
  1. The Razorpay payment link description (visible to customer on payment page)
  2. The reasoning suffix in AgentAction (for audit trail traceability)
  3. Future: WhatsApp/SMS template body (channel-agnostic generation)

Design rule: all templates are plain text, no HTML. Razorpay's description field
accepts plain text only. Keep messages under 200 chars for SMS compatibility.
"""

from __future__ import annotations
from typing import Optional


# ---------------------------------------------------------------------------
# Template bank — {lang: {action_type: template_string}}
# Placeholders: {name}, {amount}, {reason}, {pct}, {hours}, {days}, {date}
# ---------------------------------------------------------------------------

_TEMPLATES: dict[str, dict[str, str]] = {
    "en": {
        "RETRY_PAYMENT": (
            "Hi {name}, your payment of Rs.{amount} will be retried automatically. "
            "No action needed."
        ),
        "SEND_PAYMENT_LINK": (
            "Hi {name}, your payment of Rs.{amount} could not be processed. "
            "Please complete it here."
        ),
        "SEND_INCENTIVE_LINK": (
            "Hi {name}! Pay Rs.{amount} today and get {pct}% off. "
            "Limited-time offer — don't miss it."
        ),
        "SEND_CHECKOUT_RECOVERY": (
            "Hi {name}, you left Rs.{amount} in your cart {hours} ago. "
            "Complete your purchase here."
        ),
        "SEND_MANDATE_SETUP": (
            "Hi {name}, your auto-debit mandate needs to be re-set up. "
            "Please complete it in 2 minutes."
        ),
        "LOG_PROMISE_TO_PAY": (
            "Thank you {name}. Your payment commitment of Rs.{amount} by {date} "
            "has been recorded."
        ),
        "ESCALATE_TO_HUMAN": (
            "Your account Rs.{amount} requires attention. "
            "Our team will contact you shortly."
        ),
        "INITIATE_VOICE_CALL": (
            "Hi {name}, we are calling about your pending payment of Rs.{amount}. "
            "Please stay on the line or use the payment link we send after this call."
        ),
    },

    "hi": {
        "RETRY_PAYMENT": (
            "नमस्ते {name}, आपका Rs.{amount} का भुगतान स्वचालित रूप से दोबारा प्रयास किया जाएगा। "
            "कोई कार्रवाई आवश्यक नहीं।"
        ),
        "SEND_PAYMENT_LINK": (
            "नमस्ते {name}, आपका Rs.{amount} का भुगतान नहीं हो सका। "
            "कृपया यहाँ पूरा करें।"
        ),
        "SEND_INCENTIVE_LINK": (
            "नमस्ते {name}! आज Rs.{amount} का भुगतान करें और {pct}% की छूट पाएं। "
            "सीमित समय का ऑफर।"
        ),
        "SEND_CHECKOUT_RECOVERY": (
            "नमस्ते {name}, आपने {hours} घंटे पहले Rs.{amount} की खरीदारी अधूरी छोड़ी। "
            "अभी पूरा करें।"
        ),
        "SEND_MANDATE_SETUP": (
            "नमस्ते {name}, आपका ऑटो-डेबिट मैन्डेट दोबारा सेट करना होगा। "
            "2 मिनट में पूरा करें।"
        ),
        "LOG_PROMISE_TO_PAY": (
            "धन्यवाद {name}। {date} तक Rs.{amount} के भुगतान की आपकी प्रतिबद्धता दर्ज की गई।"
        ),
        "ESCALATE_TO_HUMAN": (
            "आपके खाते पर Rs.{amount} बकाया है। हमारी टीम जल्द आपसे संपर्क करेगी।"
        ),
        "INITIATE_VOICE_CALL": (
            "नमस्ते {name}, हम आपके Rs.{amount} के लंबित भुगतान के बारे में कॉल कर रहे हैं।"
        ),
    },

    "hinglish": {
        "RETRY_PAYMENT": (
            "Hi {name}! Aapka Rs.{amount} ka payment dobara try kiya jayega. "
            "Aapko kuch karne ki zaroorat nahi."
        ),
        "SEND_PAYMENT_LINK": (
            "Hi {name}! Aapka Rs.{amount} ka payment process nahi hua. "
            "Please is link se complete karein."
        ),
        "SEND_INCENTIVE_LINK": (
            "Hi {name}! Aaj Rs.{amount} pay karein aur {pct}% discount paayen. "
            "Limited time offer — jaldi karein!"
        ),
        "SEND_CHECKOUT_RECOVERY": (
            "Hi {name}! Aapne {hours} ghante pehle Rs.{amount} ki shopping "
            "adhoori chodi. Abhi complete karein!"
        ),
        "SEND_MANDATE_SETUP": (
            "Hi {name}! Aapka auto-debit mandate update karna hai. "
            "Sirf 2 minute mein yahan se setup karein."
        ),
        "LOG_PROMISE_TO_PAY": (
            "Thank you {name}! Aapki Rs.{amount} payment commitment {date} tak "
            "record ho gayi hai."
        ),
        "ESCALATE_TO_HUMAN": (
            "Hi {name}, aapke account mein Rs.{amount} pending hai. "
            "Hamaari team aapse jald contact karegi."
        ),
        "INITIATE_VOICE_CALL": (
            "Hi {name}! Hum aapke Rs.{amount} ke pending payment ke baare mein call kar rahe hain. "
            "Please line pe rahiye — payment link call ke baad bhejenge."
        ),
    },
}

# Fallback: if language not found, use English
_FALLBACK_LANG = "en"


def generate_message(
    action_type: str,
    language: str,
    customer_name: str,
    amount_inr: int,
    incentive_pct: Optional[int] = None,
    hours_since_event: Optional[int] = None,
    follow_up_date: Optional[str] = None,
) -> str:
    """
    Generate a customer-facing recovery message.

    Args:
        action_type       : ActionType enum value (e.g. "SEND_PAYMENT_LINK")
        language          : "en" | "hi" | "hinglish" (falls back to "en")
        customer_name     : first name or full name of the customer
        amount_inr        : transaction amount in INR (int)
        incentive_pct     : discount percentage (for SEND_INCENTIVE_LINK)
        hours_since_event : hours since cart was abandoned / payment failed
        follow_up_date    : ISO date string for promise-to-pay deadline

    Returns:
        Formatted message string, ready for use as Razorpay payment link
        description or SMS/WhatsApp body.
    """
    # Normalize language aliases (e.g. "hi-en", "te-en" -> "hinglish")
    lang_alias_map = {
        "hi-en": "hinglish",
        "te-en": "hinglish",
        "hi_en": "hinglish",
        "te_en": "hinglish",
        "hindi": "hi",
    }
    lang = language.lower() if language else _FALLBACK_LANG
    lang = lang_alias_map.get(lang, lang)
    if lang not in _TEMPLATES:
        lang = _FALLBACK_LANG

    templates = _TEMPLATES[lang]
    template = templates.get(action_type, templates.get("SEND_PAYMENT_LINK", ""))

    # First name only for informal tone
    first_name = customer_name.split()[0] if customer_name else "there"

    return template.format(
        name   = first_name,
        amount = f"{amount_inr:,}",
        pct    = incentive_pct or 0,
        hours  = hours_since_event or 0,
        days   = hours_since_event or 0,
        date   = follow_up_date or "the agreed date",
        reason = "",  # available for future per-reason customisation
    ).strip()


def get_available_languages() -> list[str]:
    return list(_TEMPLATES.keys())

# Alias for backwards compatibility
get_message = generate_message


# ---------------------------------------------------------------------------
# Hinglish / multilingual VOICE scripts (Track 03 direction 6)
# Spoken cadence — shorter sentences, natural pauses marked with "..."
# ---------------------------------------------------------------------------

_VOICE_SCRIPTS: dict[str, str] = {
    "en": (
        "Hello {name}. This is an automated call about your pending payment "
        "of {amount} rupees. ... Please press 1 to receive a secure payment link "
        "on WhatsApp, or stay on the line to speak with an agent."
    ),
    "hi": (
        "नमस्ते {name}। यह आपके {amount} रुपये के लंबित भुगतान के बारे में एक स्वचालित कॉल है। ... "
        "पेमेंट लिंक के लिए 1 दबाएँ, या एजेंट से बात करने के लिए लाइन पर रहें।"
    ),
    "hinglish": (
        "Hello {name}! Main aapke pending payment ke baare mein call kar raha hoon — "
        "amount hai {amount} rupees. ... Payment link WhatsApp pe chahiye to 1 dabaiye, "
        "ya agent se baat karne ke liye line pe rahiye. Dhanyavaad!"
    ),
}


def generate_voice_script(
    language: str,
    customer_name: str,
    amount_inr: int,
    reason: Optional[str] = None,
) -> dict:
    """
    Generate a spoken IVR / outbound-call script for Hinglish voice recovery.

    Returns dict with script text, estimated duration seconds, and language.
    Mock mode logs this; real mode can feed Exotel/Twilio TTS.
    """
    lang_alias_map = {
        "hi-en": "hinglish", "te-en": "hinglish",
        "hi_en": "hinglish", "te_en": "hinglish", "hindi": "hi",
    }
    lang = (language or "hinglish").lower()
    lang = lang_alias_map.get(lang, lang)
    if lang not in _VOICE_SCRIPTS:
        lang = "hinglish"  # Track 03 default for India voice recovery

    first_name = customer_name.split()[0] if customer_name else "ji"
    script = _VOICE_SCRIPTS[lang].format(
        name=first_name,
        amount=f"{amount_inr:,}",
    )
    # ~150 words/min spoken → rough duration
    word_count = len(script.replace("...", " ").split())
    duration_sec = max(12, int(word_count / 2.5))

    return {
        "language": lang,
        "script": script,
        "estimated_duration_sec": duration_sec,
        "reason": reason,
        "channel": "voice_ivr",
    }



# ---------------------------------------------------------------------------
# quick self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    test_cases = [
        ("SEND_PAYMENT_LINK",   "en",       "Priya Sharma",   4999,  None, None,  None),
        ("SEND_INCENTIVE_LINK", "hinglish", "Rahul Kumar",   12000,   10,  None,  None),
        ("SEND_CHECKOUT_RECOVERY","hinglish","Ananya Singh",  2499,  None,    2,   None),
        ("SEND_MANDATE_SETUP",  "hi",       "Vikram Mehta",  1499,  None, None,  None),
        ("LOG_PROMISE_TO_PAY",  "en",       "Sunita Rao",   25000,  None, None, "2026-09-01"),
        ("RETRY_PAYMENT",       "hinglish", "Arjun Kapoor",   999,  None, None,  None),
    ]
    print("=== Message Generator Self-Test ===\n")
    for action, lang, name, amount, pct, hours, fdate in test_cases:
        msg = generate_message(action, lang, name, amount, pct, hours, fdate)
        print(f"[{lang:8s}] {action}")
        print(f"  -> {msg}")
        print()
