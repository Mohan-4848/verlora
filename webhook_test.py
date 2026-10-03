import os
from fastapi import FastAPI, Request, Response, BackgroundTasks
import uvicorn
import httpx
from dotenv import load_dotenv

from ai_agent import generate_agent_reply

load_dotenv()

app = FastAPI(title="AI WhatsApp Order Agent")

# WhatsApp Cloud API Credentials from .env
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "hackathon_token_123")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "1314695395065596")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")

async def send_whatsapp_message(to_phone: str, text: str):
    """Sends a free-form text message via Meta WhatsApp Cloud API"""
    if not ACCESS_TOKEN:
        print("⚠️ ACCESS_TOKEN is missing. Cannot send WhatsApp message.")
        return

    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone,
        "type": "text",
        "text": {"body": text}
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, headers=headers, json=payload, timeout=10.0)
            if response.status_code == 200:
                print(f"📤 Sent WhatsApp reply to +{to_phone}: '{text[:60]}...'")
            else:
                print(f"❌ Failed to send message: {response.status_code} - {response.text}")
        except Exception as err:
            print(f"❌ Network error sending WhatsApp message: {err}")

async def process_and_reply(sender_phone: str, sender_name: str, incoming_text: str):
    """Generates AI response and sends it to user on WhatsApp"""
    print(f"\n🤖 Processing AI response for {sender_name} (+{sender_phone}): '{incoming_text}'")
    ai_reply = await generate_agent_reply(sender_phone, sender_name, incoming_text)
    print(f"💬 AI Reply: {ai_reply}\n")
    await send_whatsapp_message(sender_phone, ai_reply)

# 1. Verification Handshake (Meta calls this once on setup)
@app.get("/webhook")
def verify_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("[SETUP] Webhook verified successfully with Meta!")
        return Response(content=challenge, media_type="text/plain")
    return Response(content="Verification failed", status_code=403)

# 2. Inbound Message Handler
@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()

    try:
        entry = payload.get("entry", [])[0]
        changes = entry.get("changes", [])[0]
        value = changes.get("value", {})

        # Ensure event contains incoming messages (skip read/delivered receipts)
        if "messages" in value:
            message_data = value["messages"][0]
            sender_phone = message_data.get("from")
            msg_type = message_data.get("type")

            # Extract user's WhatsApp display profile name
            sender_name = "there"
            contacts = value.get("contacts", [])
            if contacts and "profile" in contacts[0]:
                sender_name = contacts[0]["profile"].get("name", "there")

            if msg_type == "text":
                incoming_text = message_data.get("text", {}).get("body", "")
                print(f"\n📩 Received from {sender_name} (+{sender_phone}): {incoming_text}")

                # Offload AI generation & WhatsApp reply to background tasks to prevent webhook timeout
                background_tasks.add_task(process_and_reply, sender_phone, sender_name, incoming_text)

    except Exception as e:
        print(f"Error parsing payload: {e}")

    # Immediately acknowledge Meta with 200 OK
    return {"status": "success"}

if __name__ == "__main__":
    uvicorn.run("webhook_test:app", host="0.0.0.0", port=8000, reload=True)