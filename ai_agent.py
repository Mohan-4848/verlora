import os
from dotenv import load_dotenv

load_dotenv()

# System prompt defining the persona of the AI WhatsApp Local Store Agent
STORE_AGENT_SYSTEM_PROMPT = """You are "KiranaBuddy" 🛒, an intelligent, polite, and helpful AI assistant for "Sri Krishna Quick Mart" (a local neighborhood grocery & daily essentials store) on WhatsApp.

Your Store Profile:
- Name: Sri Krishna Quick Mart
- Timings: Open 24x7 (Open 24 hours, all days)
- Delivery: 30-45 mins quick delivery to nearby apartments/local area. Free delivery on orders above ₹200 (otherwise ₹25 delivery fee).
- Payment: Cash on Delivery (COD) or instant UPI (GooglePay, PhonePe, Paytm).

Common Store Items Available:
- Dairy: Amul Taaza (500ml ₹27, 1L ₹54), Amul Gold (500ml ₹33, 1L ₹66), Nandini Milk, Fresh Paneer (200g ₹90), Amul Curd/Dahi (400g ₹40), Amul Butter.
- Bakery: Britannia Milk Bread (₹45), Brown Bread (₹55), Pav pack (₹30), Eggs (Tray of 6: ₹45, Tray of 30: ₹210).
- Staples: Aashirvaad Shudh Chakki Atta (5kg ₹245, 10kg ₹475), India Gate Basmati Rice (1kg ₹130, 5kg ₹590), Toor Dal (1kg ₹160), Tata Salt (1kg ₹28), Fortune Sunflower Oil (1L ₹145).
- Fresh Veggies: Potatoes (₹35/kg), Onions (₹40/kg), Tomatoes (₹30/kg), Green Chillies, Coriander, Ginger.
- Snacks & Beverages: Maggi 2-Minute Noodles (4-pack ₹56), Parle-G, Good Day biscuits, Coca Cola / Thums Up (750ml ₹45), Tata Tea Gold (500g ₹310).

Your Interaction Rules:
1. Greet customers warmly and address them by their name when provided.
2. When customers ask what you have or inquire about specific items, answer with specific availability, package sizes, and prices in a concise, organized WhatsApp bullet format.
3. Be conversational, natural, and WhatsApp-friendly. Use emojis appropriately (🥛, 🍞, 🛒, ⚡, etc.). Keep messages compact and clear.
4. Support multilingual chat smoothly: If the customer writes in Telugu, Hindi, Tenglish, or Hinglish (e.g. "paalu unnaya?", "1 packet bread pampandi", "bhaiya doodh hai kya?", "1 packet bread aur doodh bhej do"), reply naturally in their preferred language (Telugu, Hindi, Tenglish, or Hinglish).
5. If the customer lists items to order:
   - Acknowledge their items politely.
   - Clarify any missing details (brand, size, quantity).
   - Let them know you've noted their request and can confirm their delivery address.
"""

# In-memory conversation history per phone number: { phone: [ {"role": "user"|"assistant", "content": "..."} ] }
conversation_sessions = {}

def call_groq_llm(messages: list) -> str:
    """Ultra-fast inference via Groq (30 RPM free tier, ~200ms latency)"""
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    if not groq_key:
        return None
    
    from groq import Groq
    client = Groq(api_key=groq_key)

    # Models: llama-3.3-70b-versatile or llama-3.1-8b-instant
    for model_name in ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]:
        try:
            completion = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=0.7,
                max_tokens=800
            )
            return completion.choices[0].message.content.strip()
        except Exception as e:
            print(f"⚠️ Groq model {model_name} error: {e}")
            continue
    return None

def call_gemini_llm(user_prompt: str, recent_turns: list) -> str:
    """Fallback inference via Google Gemini"""
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        return None
    
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=gemini_key)

    contents = []
    for turn in recent_turns:
        role = "user" if turn["role"] == "user" else "model"
        contents.append(
            types.Content(
                role=role,
                parts=[types.Part.from_text(text=turn["content"])]
            )
        )
    contents.append(
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=user_prompt)]
        )
    )

    generate_config = types.GenerateContentConfig(
        system_instruction=STORE_AGENT_SYSTEM_PROMPT,
        temperature=0.7,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        max_output_tokens=800
    )

    models_to_try = ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash"]
    for model_name in models_to_try:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=generate_config
            )
            if response.text:
                return response.text.strip()
        except Exception as e:
            print(f"⚠️ Gemini {model_name} failed: {e}")
            continue
    return None

async def generate_agent_reply(sender_phone: str, sender_name: str, incoming_text: str) -> str:
    """
    Generates high-speed, high-RPM conversational AI response.
    Primary: Groq (30 RPM, 200ms)
    Secondary fallback: Google Gemini
    """
    # Initialize history for new users
    if sender_phone not in conversation_sessions:
        conversation_sessions[sender_phone] = []

    history = conversation_sessions[sender_phone]

    # Append customer identification context
    user_prompt = f"Customer Name: {sender_name} | Phone: +{sender_phone}\nMessage: {incoming_text}"
    recent_history = history[-8:]

    # Prepare OpenAI/Groq standard message format
    messages = [{"role": "system", "content": STORE_AGENT_SYSTEM_PROMPT}]
    for turn in recent_history:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": user_prompt})

    reply_text = None

    # 1. Try Groq first for blazing speed and 30+ RPM
    if os.getenv("GROQ_API_KEY", "").strip():
        reply_text = call_groq_llm(messages)

    # 2. Fallback to Gemini if Groq is not configured or failed
    if not reply_text:
        reply_text = call_gemini_llm(user_prompt, recent_history)

    # 3. Default fallback if all fail or keys missing
    if not reply_text:
        reply_text = (
            f"Hello {sender_name}! 👋 Welcome to Sri Krishna Quick Mart.\n\n"
            f"I have noted your request: \"{incoming_text}\".\n"
            f"We are ready to take your order! Please share your Groq or Gemini API key to activate full AI assistance."
        )

    # Store turn in history
    history.append({"role": "user", "content": incoming_text})
    history.append({"role": "assistant", "content": reply_text})

    return reply_text
