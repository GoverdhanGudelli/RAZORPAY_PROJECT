"""
sarvam_tts.py -- Sarvam AI Text-to-Speech + Translation integration.

API: POST https://api.sarvam.ai/text-to-speech
Auth: api-subscription-key header
Response: { "audios": ["<base64-wav>", ...] }

Models:
  bulbul:v1  -- original Indian TTS
  bulbul:v2  -- improved prosody
  bulbul:v3  -- latest, most natural (recommended)

Speakers (female): meera, pavithra, maitreyi, arvind(m), amol(m), amartya(m)
Languages: hi-IN, en-IN, ta-IN, te-IN, kn-IN, ml-IN, mr-IN, gu-IN, bn-IN, pa-IN

Usage:
  from sarvam_tts import generate_hinglish_audio, translate_to_hindi

  result = generate_hinglish_audio(
      text="Namaste Priya ji! Aapka 15,000 rupees ka payment pending hai.",
      speaker="meera",
  )
  # result = {"audio_b64": "...", "audio_bytes": b"...", "wav_path": "data/audio/xxx.wav"}
"""

from __future__ import annotations
import os
import base64
import hashlib
import json
import requests
import struct
import math
import wave
import io
from pathlib import Path
from typing import Optional

def _load_env_file():
    for base in [Path.cwd(), Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent, Path(__file__).resolve().parent.parent.parent]:
        ef = base / ".env"
        if ef.exists():
            try:
                for line in ef.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k and v and not os.environ.get(k):
                            os.environ[k] = v
            except Exception:
                pass

_load_env_file()

def _get_sarvam_api_key() -> str:
    key = os.environ.get("SARVAM_API_KEY", "")
    if not key:
        _load_env_file()
        key = os.environ.get("SARVAM_API_KEY", "")
    return key.strip()

SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"
SARVAM_TRANSLATE_URL = "https://api.sarvam.ai/translate"

# Cache directory for generated audio files (avoid re-generating same scripts)
AUDIO_CACHE_DIR = Path(os.environ.get("AUDIO_CACHE_DIR", "data/audio"))

# Available Sarvam TTS voices (bulbul:v3)
SARVAM_VOICES = {
    "priya":     {"gender": "female", "lang": "hi-IN", "style": "warm"},
    "ritu":      {"gender": "female", "lang": "hi-IN", "style": "professional"},
    "neha":      {"gender": "female", "lang": "hi-IN", "style": "expressive"},
    "pooja":     {"gender": "female", "lang": "hi-IN", "style": "natural"},
    "simran":    {"gender": "female", "lang": "hi-IN", "style": "friendly"},
    "kavya":     {"gender": "female", "lang": "hi-IN", "style": "polite"},
    "rahul":     {"gender": "male",   "lang": "hi-IN", "style": "neutral"},
    "rohan":     {"gender": "male",   "lang": "hi-IN", "style": "friendly"},
    "aditya":    {"gender": "male",   "lang": "hi-IN", "style": "formal"},
    "ashutosh":  {"gender": "male",   "lang": "hi-IN", "style": "professional"},
    "amit":      {"gender": "male",   "lang": "hi-IN", "style": "calm"},
    "dev":       {"gender": "male",   "lang": "hi-IN", "style": "direct"},
}

# Speaker alias mapping for backward compatibility
SPEAKER_ALIASES = {
    "meera": "priya",
    "anushka": "priya",
    "pavithra": "ritu",
    "vidya": "ritu",
    "maitreyi": "neha",
    "manisha": "neha",
    "arvind": "rahul",
    "abhilash": "rahul",
    "amol": "rohan",
    "karun": "rohan",
    "amartya": "aditya",
    "hitesh": "aditya",
}

DEFAULT_SPEAKER   = "priya"
DEFAULT_MODEL     = "bulbul:v3"
DEFAULT_LANGUAGE  = "hi-IN"
PHONE_SAMPLE_RATE = 8000   # 8kHz for telephony


def _sarvam_headers() -> dict:
    return {
        "api-subscription-key": _get_sarvam_api_key(),
        "Content-Type": "application/json",
    }


def _cache_path(text: str, speaker: str, model: str) -> Path:
    """Generate a deterministic file path for a given text+voice combo."""
    key = hashlib.md5(f"{model}:{speaker}:{text}".encode()).hexdigest()[:12]
    AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return AUDIO_CACHE_DIR / f"sarvam_{key}.wav"


def generate_hinglish_audio(
    text: str,
    speaker: str = DEFAULT_SPEAKER,
    model: str = DEFAULT_MODEL,
    language_code: str = DEFAULT_LANGUAGE,
    target_sample_rate: int = PHONE_SAMPLE_RATE,
    enable_preprocessing: bool = True,
    use_cache: bool = True,
) -> dict:
    """
    Convert Hinglish text to speech using Sarvam Bulbul TTS.

    Args:
        text              : Hinglish/Hindi text to synthesize.
        speaker           : Voice ID (meera, pavithra, maitreyi, arvind, amol, amartya).
        model             : Sarvam TTS model (bulbul:v2, bulbul:v3).
        language_code     : BCP-47 language tag (hi-IN for Hinglish).
        target_sample_rate: 8000 for telephony, 22050 for web playback.
        enable_preprocessing: Let Sarvam normalize numbers/dates to spoken form.
        use_cache         : Skip API call if same text+voice was already synthesized.

    Returns:
        {
          "audio_b64"   : base64-encoded WAV string (ready for API response),
          "audio_bytes" : raw WAV bytes,
          "wav_path"    : absolute path to saved WAV file (or None if mock),
          "duration_ms" : estimated duration in milliseconds,
          "speaker"     : speaker used,
          "mock"        : True if SARVAM_API_KEY not set,
          "error"       : error string if something went wrong (else None),
        }
    """
    if model == "bulbul:v2":
        model = "bulbul:v3"
    speaker = SPEAKER_ALIASES.get(speaker.lower(), speaker.lower())
    if speaker not in SARVAM_VOICES:
        speaker = DEFAULT_SPEAKER
    cache_file = _cache_path(text, speaker, model)

    # Serve from cache if available
    if use_cache and cache_file.exists():
        audio_bytes = cache_file.read_bytes()
        return {
            "audio_b64": base64.b64encode(audio_bytes).decode(),
            "wav_path": str(cache_file.absolute()),
            "duration_ms": _estimate_duration_ms(text),
            "speaker": speaker,
            "mock": False,
            "cached": True,
            "error": None,
        }

    api_key = _get_sarvam_api_key()
    # No API key -- return playable demo audio in mock mode so audio player works locally
    if not api_key:
        mock_b64, mock_bytes, mock_path = _generate_mock_wav(text, speaker, model)
        return {
            "audio_b64": mock_b64,
            "wav_path": mock_path,
            "duration_ms": _estimate_duration_ms(text),
            "speaker": speaker,
            "mock": True,
            "cached": True,
            "error": "SARVAM_API_KEY not set (playing demo audio). Get a free key at apps.sarvam.ai",
        }

    payload = {
        "inputs": [text],
        "target_language_code": language_code,
        "speaker": speaker,
        "model": model,
        "pitch": 0,
        "pace": 1.0,
        "loudness": 1.5,
        "speech_sample_rate": target_sample_rate,
        "enable_preprocessing": enable_preprocessing,
    }

    try:
        resp = requests.post(
            SARVAM_TTS_URL,
            headers=_sarvam_headers(),
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()

        audios = data.get("audios") or []
        if not audios:
            raise ValueError("Sarvam TTS returned empty audios list")

        audio_b64 = audios[0]
        audio_bytes = base64.b64decode(audio_b64)

        # Save to cache
        cache_file.write_bytes(audio_bytes)

        return {
            "audio_b64": audio_b64,
            "wav_path": str(cache_file.absolute()),
            "duration_ms": _estimate_duration_ms(text),
            "speaker": speaker,
            "mock": False,
            "cached": False,
            "error": None,
        }

    except requests.HTTPError as e:
        err = f"Sarvam TTS HTTP error: {e.response.status_code} {e.response.text[:200]}"
        return {"audio_b64": None, "wav_path": None,
                "duration_ms": 0, "speaker": speaker, "mock": False,
                "cached": False, "error": err}
    except Exception as e:
        return {"audio_b64": None, "wav_path": None,
                "duration_ms": 0, "speaker": speaker, "mock": False,
                "cached": False, "error": str(e)}


def generate_recovery_audio(
    customer_name: str,
    amount_inr: int,
    failure_reason: str,
    speaker: str = DEFAULT_SPEAKER,
    model: str = DEFAULT_MODEL,
) -> dict:
    """
    Generate the complete Hinglish recovery call opening script as audio.
    This is the first thing Riya says when a customer picks up.

    Returns the same dict as generate_hinglish_audio plus 'script' key.
    """
    first_name = customer_name.split()[0] if customer_name else "ji"
    amount_str = f"{amount_inr:,}"

    # Reason-specific empathy phrase
    reason_phrases = {
        "bank_decline":        "bank se payment nahi ho paya",
        "insufficient_funds":  "account mein balance kam tha",
        "card_expired":        "card ki expiry aa gayi thi",
        "3ds_auth_fail":       "payment authentication mein dikkat aayi",
        "hard_decline":        "payment decline ho gayi",
        "bank_downtime":       "bank temporarily unavailable tha",
        "mandate_revoked":     "auto-pay mandate cancel ho gaya tha",
        "invoice_overdue":     "payment pending hai",
    }
    reason_phrase = reason_phrases.get(failure_reason, "payment process nahi ho paya")

    script = (
        f"Namaste {first_name} ji! Main Riya bol rahi hoon. "
        f"Aapka {amount_str} rupees ka {reason_phrase}. "
        f"Kya main aapko abhi ek secure payment link bhej sakti hoon "
        f"taki aap easily pay kar sakein?"
    )

    result = generate_hinglish_audio(
        text=script,
        speaker=speaker,
        model=model,
        language_code="hi-IN",
        target_sample_rate=PHONE_SAMPLE_RATE,
    )
    result["script"] = script
    result["customer_name"] = customer_name
    result["amount_inr"] = amount_inr
    return result


def translate_to_hindi(
    text: str,
    source_language: str = "en-IN",
    target_language: str = "hi-IN",
) -> dict:
    """
    Translate English text to Hindi using Sarvam Translate API.
    Useful for translating English error messages / system text before TTS.

    Returns: { "translated_text": str, "mock": bool, "error": str|None }
    """
    if not _get_sarvam_api_key():
        return {
            "translated_text": text,  # passthrough
            "mock": True,
            "error": "SARVAM_API_KEY not set",
        }

    payload = {
        "input": text,
        "source_language_code": source_language,
        "target_language_code": target_language,
        "model": "mayura:v1",
        "enable_preprocessing": True,
    }

    try:
        resp = requests.post(
            SARVAM_TRANSLATE_URL,
            headers=_sarvam_headers(),
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "translated_text": data.get("translated_text", text),
            "mock": False,
            "error": None,
        }
    except Exception as e:
        return {"translated_text": text, "mock": False, "error": str(e)}


def _estimate_duration_ms(text: str) -> int:
    """
    Rough estimate: average Hinglish TTS pace ~130 words/min.
    Used when actual audio is not available (mock mode).
    """
    word_count = len(text.split())
    return max(1000, int((word_count / 130) * 60 * 1000))


def list_voices() -> list[dict]:
    """Return metadata for all available Sarvam voices."""
    return [{"id": k, **v} for k, v in SARVAM_VOICES.items()]


def _generate_mock_wav(text: str, speaker: str, model: str) -> tuple[str, bytes, str]:
    """Generate a clean demo WAV audio file for mock mode without external dependencies."""
    cache_file = _cache_path(text, speaker, model)
    duration_ms = _estimate_duration_ms(text)
    if not cache_file.exists():
        sample_rate = 8000
        num_samples = int(sample_rate * (duration_ms / 1000.0))
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            for i in range(num_samples):
                t = i / sample_rate
                sample = int(8000 * math.sin(2 * math.pi * 440 * t) * math.exp(-t * 0.3) +
                             6000 * math.sin(2 * math.pi * 554.37 * t) * math.exp(-t * 0.3))
                sample = max(-32768, min(32767, sample))
                wav_file.writeframes(struct.pack('<h', sample))
        raw_bytes = buffer.getvalue()
        cache_file.write_bytes(raw_bytes)
    else:
        raw_bytes = cache_file.read_bytes()

    audio_b64 = base64.b64encode(raw_bytes).decode()
    return audio_b64, raw_bytes, str(cache_file.absolute())

