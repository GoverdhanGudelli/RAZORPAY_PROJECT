# Complete Feature Testing Guide

This guide contains all PowerShell commands to start the server and test every single feature of the **Razorpay Revenue Recovery Agent** in both **Mock Mode (`MOCK_MODE=true`)** and **Live Mode (`MOCK_MODE=false`)**.

---

## 🚀 Step 1: Starting the Server

### Mode A: Mock Mode (`MOCK_MODE=true`) — Default / Demo Mode
*No external API keys required. Everything works locally with simulated Razorpay & Voice responses.*

```powershell
# Set Environment Variables
$env:MOCK_MODE = "true"
$env:API_SECRET_KEY = "0XMmx1cU502aRFk75iq0Me3e"
$env:RAZORPAY_KEY_ID = "rzp_test_TTxoPHD9iuVmov"
$env:RAZORPAY_KEY_SECRET = "0XMmx1cU502aRFk75iq0Me3e"

# Run FastAPI Server
python -m uvicorn src.api:app --host 127.0.0.1 --port 8000 --reload
```
clear
---

### Mode B: Live Mode (`MOCK_MODE=false`) — Real Integration
*Requires valid Razorpay Test/Live API keys and optional Sarvam/Vapi keys.*

```powershell
# Set Real Credentials
$env:MOCK_MODE = "false"
$env:RAZORPAY_KEY_ID = "rzp_test_YOUR_KEY_ID"
$env:RAZORPAY_KEY_SECRET = "YOUR_KEY_SECRET"
$env:API_SECRET_KEY = "YOUR_KEY_SECRET"
$env:SARVAM_API_KEY = "YOUR_SARVAM_KEY"       # Optional: for real Sarvam TTS
$env:VAPI_API_KEY = "YOUR_VAPI_KEY"           # Optional: for real Vapi WebRTC calls

# Run FastAPI Server
python -m uvicorn src.api:app --host 127.0.0.1 --port 8000 --reload
```

---

## 🧪 Step 2: Testing All Features via PowerShell

Run these commands in a **separate terminal window** (navigate to `revenue-recovery-agent` folder):

### 1. Server Liveness & Health Check
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/health"
```

---

### 2. Transaction Processing (Engine Intervention Router)

#### A. Soft Decline (Bank Downtime / Temporary Issue -> Schedule Smart Retry)
```powershell
$body = @{
    record_id = "TXN_DEMO_001"
    transaction_type = "one_time"
    failure_reason = "bank_decline"
    amount_inr = 15000
    customer_name = "Priya Sharma"
    customer_email = "priya@example.com"
    customer_phone = "+919876543210"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8000/process-failure" -Method Post -Body $body -ContentType "application/json"
```

#### B. Insufficient Funds (Smart Incentive + Payment Link)
```powershell
$body = @{
    record_id = "TXN_DEMO_002"
    transaction_type = "subscription"
    failure_reason = "insufficient_funds"
    amount_inr = 4999
    customer_name = "Rahul Verma"
    customer_phone = "+919812345678"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8000/process-failure" -Method Post -Body $body -ContentType "application/json"
```

#### C. Expired Card (Alternative Payment Method Link)
```powershell
$body = @{
    record_id = "TXN_DEMO_003"
    transaction_type = "one_time"
    failure_reason = "card_expired"
    amount_inr = 12000
    customer_name = "Ananya Das"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8000/process-failure" -Method Post -Body $body -ContentType "application/json"
```

---

### 3. Hinglish Voice Recovery Agent (Vapi Integration)

#### A. Fetch Vapi Agent Configuration
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/voice/agent-config?customer_name=Priya&amount_inr=15000&failure_reason=bank_decline"
```

#### B. Launch Voice Call (WebRTC Session)
```powershell
$body = @{
    record_id = "TXN_DEMO_001"
    customer_name = "Priya Sharma"
    amount_inr = 15000
    failure_reason = "bank_decline"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8000/voice/call/launch" -Method Post -Body $body -ContentType "application/json"
```

#### C. List All Voice Call Records
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/voice/calls"
```

---

### 4. Sarvam AI Voice Engine (Indian Hinglish TTS)

#### A. List Sarvam AI Voices & Styles
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/voice/sarvam/voices"
```

#### B. Generate Custom Text-to-Speech (Meera Voice)
```powershell
$body = @{
    text = "Namaste Priya ji! Aapka 15,000 rupees ka payment bank se decline ho gaya tha."
    speaker = "meera"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8000/voice/sarvam/tts" -Method Post -Body $body -ContentType "application/json"
```

#### C. Generate Recovery Call Script & Audio for Transaction
```powershell
$body = @{
    record_id = "TXN_DEMO_001"
    speaker = "meera"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8000/voice/sarvam/generate-script-audio" -Method Post -Body $body -ContentType "application/json"
```

---

### 5. Promise to Pay (PTP Tracker)

#### A. Record Promise-to-Pay
```powershell
$body = @{
    record_id = "TXN_DEMO_001"
    promised_date = "2026-09-10"
    promised_amount_inr = 15000
    contact_name = "Priya Sharma"
    notes = "Customer promised to pay after salary deposit."
} | ConvertTo-Json

$headers = @{ "X-Api-Key" = "0XMmx1cU502aRFk75iq0Me3e" }
Invoke-RestMethod -Uri "http://127.0.0.1:8000/promises" -Method Post -Headers $headers -Body $body -ContentType "application/json"
```

#### B. Check Overdue & Reminders
```powershell
$headers = @{ "X-Api-Key" = "0XMmx1cU502aRFk75iq0Me3e" }
Invoke-RestMethod -Uri "http://127.0.0.1:8000/promises/reminders" -Headers $headers
```

---

### 6. Measured Recovery Metrics (The Bar)
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/metrics/recovery"
```

---

### 7. Degradation & Anomaly Cluster Detection
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/analyze/degradation"
```

---

### 8. Automated Scheduler & Dunning Sweep

#### A. List Pending Scheduled Actions
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/scheduler/pending"
```

#### B. Execute Pending Retry Actions
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/scheduler/run" -Method Post
```

---

## ⚡ Automated 1-Click Test Script

You can also run our automated test suite in one command:

```powershell
python test_all_features.py
```
