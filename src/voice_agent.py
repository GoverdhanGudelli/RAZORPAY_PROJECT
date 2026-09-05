"""
voice_agent.py -- Vapi assistant configuration for Hinglish Voice Recovery.

Blueprint section 2 (Architecture) + section 3 (Tools) + section 4 (Guardrails).

Builds the full Vapi assistant JSON:
  - Model: Gemini 1.5 Flash (free tier, fast)
  - Voice: Sarvam AI (Indian accent, Hinglish TTS) with ElevenLabs multilingual fallback
  - System prompt: polite Hinglish persona + strict compliance guardrails
  - 4 function-call tools -> FastAPI /voice/tools/* endpoints
  - firstMessage: natural Hinglish greeting with customer name + amount injected

Usage:
  from voice_agent import get_assistant_config, create_web_call

  config = get_assistant_config(backend_url="http://127.0.0.1:8000")
  call   = create_web_call(record_id="TXN001", amount_inr=15000,
                           customer_name="Priya", failure_reason="bank_decline")
"""

from __future__ import annotations
import os
import requests
from typing import Any, Optional

try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(usecwd=True), override=True)
except ImportError:
    pass

VAPI_API_KEY = os.environ.get("VAPI_API_KEY", "")
VAPI_BASE_URL = "https://api.vapi.ai"

# Backend URL where Vapi sends tool-call webhooks.
# For local demos: set BACKEND_BASE_URL=https://<ngrok-id>.ngrok.io
BACKEND_BASE_URL = os.environ.get("BACKEND_BASE_URL", "http://127.0.0.1:8000")


# ---------------------------------------------------------------------------
# System prompt (blueprint section 4)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are Riya, a friendly and professional payment recovery assistant for a fintech company. \
You speak natural Hinglish -- a natural mix of Hindi and English as spoken by urban Indians. \
Your job is to help customers resolve failed or pending payments in a warm, helpful, non-pushy way.

PERSONA:
- Speak in short, conversational Hinglish sentences (1-2 sentences per turn maximum).
- Be warm, empathetic, and never aggressive or threatening.
- Address the customer by their first name.
- Example style: "Namaste Priya ji! Aapka Rs.15,000 ka payment process nahi ho paya tha. \
Kya main aapko abhi ek secure payment link WhatsApp par bhej doon?"

COMPLIANCE -- STRICT RULES (never break these):
1. NEVER ask for OTP, card CVV, card number, UPI PIN, or bank password. \
If customer offers these, say "Nahi nahi, aap yeh share mat karein -- \
humein inki zaroorat nahi. Main sirf ek link bhejta hoon."
2. If the customer says ANYTHING like "call mat karo", "not interested", "band karo", \
"DND", "dobara mat call karna" -- immediately call mark_opt_out, apologize once, \
and end the call politely. Do not retry persuasion.
3. Maximum 2 outbound call attempts to the same customer in any 24-hour window.
4. Do not make specific guarantees about bank processing times.
5. Do not discuss competitor payment platforms.

CONVERSATION FLOW:
1. Greet the customer, mention the pending amount, ask if they would like a payment link.
2. If YES -> call send_payment_link immediately, confirm the link is sent, end the call politely.
3. If NOT NOW / BUSY -> ask when is a good time, call schedule_ptp_retry with their preferred date.
4. If there is a DISPUTE / COMPLAINT -> call escalate_to_human, tell them a team member will call back.
5. If OPT OUT -> call mark_opt_out, apologize once, end the call.

LANGUAGE:
- Default to Hinglish. If customer responds in pure Hindi, respond in pure Hindi.
- If customer responds in pure English, respond in English.
- Always be polite: "ji", "please", "shukriya / thank you" are always appropriate.
"""

FIRST_MESSAGE_TEMPLATE = (
    "Namaste {customer_name} ji! Main Riya bol rahi hoon -- "
    "aapka Rs.{amount_inr} ka payment process nahi ho paya tha. "
    "Kya main aapko abhi ek secure payment link bhej sakti hoon?"
)

END_CALL_MESSAGE = (
    "Aapka bahut shukriya! Agar koi aur help chahiye to hum available hain. "
    "Have a great day! Namaste!"
)


# ---------------------------------------------------------------------------
# Tool definitions (blueprint section 3)
# ---------------------------------------------------------------------------

def _tool_send_payment_link(server_url: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "send_payment_link",
            "description": (
                "Send a secure Razorpay payment link to the customer via WhatsApp/SMS "
                "so they can complete their pending payment immediately. "
                "Call this when the customer agrees to pay."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "record_id": {
                        "type": "string",
                        "description": "The transaction record ID (provided in your context).",
                    },
                    "method": {
                        "type": "string",
                        "enum": ["whatsapp", "sms", "link_only"],
                        "description": "Delivery channel. Default: link_only (URL returned to agent).",
                    },
                },
                "required": ["record_id"],
            },
        },
        "server": {"url": f"{server_url}/voice/tools/send_payment_link"},
    }


def _tool_schedule_ptp(server_url: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "schedule_ptp_retry",
            "description": (
                "Record a Promise-to-Pay when the customer says they will pay later "
                "(e.g., 'kal subah karunga'). Schedule a follow-up reminder for that date."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "record_id": {
                        "type": "string",
                        "description": "The transaction record ID.",
                    },
                    "agreed_date": {
                        "type": "string",
                        "description": "ISO date string (YYYY-MM-DD) when customer will pay.",
                    },
                    "contact_name": {
                        "type": "string",
                        "description": "Customer name as spoken during the call.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Brief note from the conversation (e.g., 'salary credited kal').",
                    },
                },
                "required": ["record_id", "agreed_date"],
            },
        },
        "server": {"url": f"{server_url}/voice/tools/schedule_ptp_retry"},
    }


def _tool_mark_opt_out(server_url: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "mark_opt_out",
            "description": (
                "Flag the customer as Do-Not-Disturb and stop all future automated outreach. "
                "Call this immediately if the customer says 'call mat karo', "
                "'not interested', 'DND', or any clear opt-out expression."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "record_id": {
                        "type": "string",
                        "description": "The transaction record ID.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Brief reason for opt-out as expressed by customer.",
                    },
                },
                "required": ["record_id"],
            },
        },
        "server": {"url": f"{server_url}/voice/tools/mark_opt_out"},
    }


def _tool_escalate(server_url: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "escalate_to_human",
            "description": (
                "Route the customer to a human support agent for disputes, "
                "refund requests, or complex issues the voice agent cannot resolve."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "record_id": {
                        "type": "string",
                        "description": "The transaction record ID.",
                    },
                    "issue_type": {
                        "type": "string",
                        "enum": ["dispute", "refund_request", "bank_issue",
                                 "duplicate_charge", "other"],
                        "description": "Nature of the issue.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Brief summary of what the customer said.",
                    },
                },
                "required": ["record_id", "issue_type"],
            },
        },
        "server": {"url": f"{server_url}/voice/tools/escalate_to_human"},
    }


# ---------------------------------------------------------------------------
# Assistant config builder
# ---------------------------------------------------------------------------

def get_assistant_config(
    backend_url: Optional[str] = None,
    customer_name: str = "Customer",
    amount_inr: int = 0,
    record_id: str = "",
    failure_reason: str = "",
) -> dict[str, Any]:
    """
    Build the full Vapi assistant config dict.

    Args:
        backend_url   : Base URL of the FastAPI server (tool webhook receiver).
        customer_name : Injected into firstMessage.
        amount_inr    : Injected into firstMessage.
        record_id     : Injected as assistant metadata for tools.
        failure_reason: Context for the agent understanding.

    Returns:
        Dict ready to POST to https://api.vapi.ai/assistant or
        used as assistant param in POST /call.
    """
    server_url = (backend_url or BACKEND_BASE_URL).rstrip("/")

    first_name = customer_name.split()[0] if customer_name else "ji"
    first_message = FIRST_MESSAGE_TEMPLATE.format(
        customer_name=first_name,
        amount_inr=f"{amount_inr:,}",
    )

    return {
        "name": "Riya -- Hinglish Recovery Agent",
        "model": {
            "provider": "google",
            "model": "gemini-1.5-flash",
            "systemPrompt": SYSTEM_PROMPT + (
                f"\n\nCURRENT CALL CONTEXT:\n"
                f"  record_id     : {record_id}\n"
                f"  customer_name : {customer_name}\n"
                f"  amount_inr    : Rs.{amount_inr:,}\n"
                f"  failure_reason: {failure_reason}\n"
                f"\nAlways use record_id='{record_id}' when calling any tool."
            ),
            "temperature": 0.4,
        },
        "voice": {
            "provider": "sarvam",
            "voiceId": "meera",
            "language": "hi-IN",
        },
        "firstMessage": first_message,
        "endCallMessage": END_CALL_MESSAGE,
        "endCallPhrases": [
            "shukriya", "dhanyawad", "thank you so much", "goodbye", "bye bye",
            "call khatam", "rakhta hoon", "rakhti hoon",
        ],
        "tools": [
            _tool_send_payment_link(server_url),
            _tool_schedule_ptp(server_url),
            _tool_mark_opt_out(server_url),
            _tool_escalate(server_url),
        ],
        "silenceTimeoutSeconds": 20,
        "maxDurationSeconds": 300,
        "metadata": {
            "record_id": record_id,
            "amount_inr": amount_inr,
            "failure_reason": failure_reason,
        },
    }


# ---------------------------------------------------------------------------
# Vapi API helpers
# ---------------------------------------------------------------------------

def _vapi_headers() -> dict:
    return {
        "Authorization": f"Bearer {VAPI_API_KEY}",
        "Content-Type": "application/json",
    }


def create_web_call(
    record_id: str,
    amount_inr: int,
    customer_name: str,
    failure_reason: str,
    backend_url: Optional[str] = None,
) -> dict[str, Any]:
    """
    Create a Vapi Web Call (browser WebRTC -- no phone number needed).
    Perfect for demos/buildathon: judge opens the URL in their browser.

    Returns dict with:
      call_id      : Vapi call ID
      web_call_url : URL the customer/judge opens to start the voice session
      status       : queued | error | mock
    """
    if not VAPI_API_KEY:
        import random, string
        fake_id = "call_mock_" + "".join(random.choices(string.ascii_lowercase, k=8))
        return {
            "call_id": fake_id,
            "web_call_url": f"https://vapi.ai/demo?callId={fake_id}",
            "status": "mock",
            "mock": True,
            "note": "VAPI_API_KEY not set -- set it in .env to make real calls.",
        }

    assistant_config = get_assistant_config(
        backend_url=backend_url,
        customer_name=customer_name,
        amount_inr=amount_inr,
        record_id=record_id,
        failure_reason=failure_reason,
    )

    payload = {
        "type": "webCall",
        "assistant": assistant_config,
    }

    try:
        resp = requests.post(
            f"{VAPI_BASE_URL}/call",
            headers=_vapi_headers(),
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "call_id": data.get("id"),
            "web_call_url": data.get("webCallUrl") or data.get("url"),
            "status": data.get("status", "queued"),
            "mock": False,
            "raw": data,
        }
    except requests.HTTPError as e:
        return {"call_id": None, "web_call_url": None, "status": "error",
                "error": str(e), "mock": False}
    except Exception as e:
        return {"call_id": None, "web_call_url": None, "status": "error",
                "error": str(e), "mock": False}


def get_call_status(call_id: str) -> dict[str, Any]:
    """Fetch live call status and transcript from Vapi."""
    if not VAPI_API_KEY or call_id.startswith("call_mock_"):
        return {"id": call_id, "status": "mock", "transcript": []}
    try:
        resp = requests.get(
            f"{VAPI_BASE_URL}/call/{call_id}",
            headers=_vapi_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"id": call_id, "status": "error", "error": str(e)}
