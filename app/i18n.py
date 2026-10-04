"""Multilingual support (English, Telugu, Hindi) for WhatsApp messages and flows.

Provides language detection, normalization, translations for all interactive buttons,
menus, cart, checkout, confirmation, status updates, and keyword dictionaries.
"""
import re

LANG_EN = "en"
LANG_TE = "te"
LANG_HI = "hi"

SUPPORTED_LANGUAGES = {LANG_EN, LANG_TE, LANG_HI}

# Regex to detect Telugu script (\u0C00-\u0C7F) and Devanagari/Hindi script (\u0900-\u097F)
RE_TELUGU_SCRIPT = re.compile(r"[\u0C00-\u0C7F]")
RE_HINDI_SCRIPT = re.compile(r"[\u0900-\u097F]")

# Romanized keyword cues for Telugu (Tenglish)
RE_TELUGU_ROMAN = re.compile(
    r"\b(paalu|palu|biyyam|kavali|kaavali|kavaali|ivvandi|pampandi|avunu|vaddu|oddu|namaskaram|"
    r"inka|mariyu|meeru|cheyandi|enti|ekkada|kooda|kuda|chudandi|kavala|unnaya|unnada|ledha|"
    r"dhanyavadalu|andhariki|baga|ekkada|repu|ivvala|eppudu)\b",
    re.I
)

# Romanized keyword cues for Hindi (Hinglish)
RE_HINDI_ROMAN = re.compile(
    r"\b(doodh|dudh|chawal|aata|atta|chahiye|chaiye|bhejo|bhej|bhaiya|namaste|shukriya|haan|"
    r"nahi|nahin|kripya|kaha|kahan|kaise|karo|batao|dena|kya|kyun|kab|kitna|kitne|chota|bada|"
    r"kuch|peene|khana|sabji|pani|daal|dhanyawad|theek|thik)\b",
    re.I
)


def normalize_language(lang: str | None) -> str:
    """Normalize any language string to 'en', 'te', or 'hi'."""
    if not lang:
        return LANG_EN
    l = lang.strip().lower()
    if l in ("te", "telugu", "tenglish", "తెలుగు"):
        return LANG_TE
    if l in ("hi", "hindi", "hinglish", "हिंदी", "हिन्दी"):
        return LANG_HI
    return LANG_EN


def detect_language(text: str) -> str | None:
    """Auto-detect language from incoming customer text. Returns 'te', 'hi', 'en', or None if neutral."""
    if not text:
        return None
    raw = text.strip()
    norm = raw.lower()

    # Explicit language switch commands
    if norm in ("telugu", "తెలుగు", "te"):
        return LANG_TE
    if norm in ("hindi", "हिंदी", "हिन्दी", "hi"):
        return LANG_HI
    if norm in ("english", "en", "angrezi"):
        return LANG_EN

    # Native script check
    if RE_TELUGU_SCRIPT.search(raw):
        return LANG_TE
    if RE_HINDI_SCRIPT.search(raw):
        return LANG_HI

    # Romanized keyword checks
    if RE_TELUGU_ROMAN.search(norm):
        return LANG_TE
    if RE_HINDI_ROMAN.search(norm):
        return LANG_HI

    return None


# ---------------- Multi-lingual Vocabulary for Keyword Matching ----------------

GREETINGS_I18N = {
    # English
    "hi", "hii", "hiii", "hlo", "hloo", "hlw", "hello", "helo", "hello there", "hey", "hai", "hy", "yo",
    "good morning", "good evening", "gm", "menu", "start", "home", "main menu", "0", "restart", "back",
    # Telugu (Script + Romanized)
    "నమస్కారం", "నమస్తే", "హలో", "హాయ్", "బాగున్నారా", "namaste", "namaskar", "namaskaram",
    # Hindi (Script + Romanized)
    "नमस्ते", "नमस्कार", "प्रणाम", "हेलो", "हाय", "kem cho", "ram ram", "pranam", "pranaam"
}

YES_I18N = {
    # English
    "yes", "y", "ok", "okay", "confirm", "confirm order", "sure", "done", "place order",
    # Telugu
    "అవును", "సరే", "ఆర్డర్ చేయండి", "కన్ఫర్మ్", "హాం", "హా", "avunu", "sare", "ha", "avunandi",
    # Hindi
    "हाँ", "हां", "ठीक है", "कन्फर्म", "ऑर्डर करो", "सत्यापित करें", "haan", "han", "thik hai", "theek hai", "sahi hai"
}

NO_I18N = {
    # English
    "no", "n", "cancel", "stop",
    # Telugu
    "వద్దు", "రద్దు", "కాదు", "రద్దు చేయండి", "వద్దండి", "vaddu", "oddu", "kadu",
    # Hindi
    "नहीं", "रद्द", "रद्द करें", "कैंसिल", "nahi", "nahin", "mat karo"
}

CART_WORDS_I18N = {
    # English
    "cart", "my cart", "basket", "view cart",
    # Telugu
    "కార్ట్", "బుట్ట", "నా కార్ట్", "kart", "butta",
    # Hindi
    "कार्ट", "मेरी कार्ट", "टोकरी", "झोला", "tokri", "jhola"
}

CHECKOUT_WORDS_I18N = {
    # English
    "checkout", "order now", "buy", "place order",
    # Telugu
    "చెక్అవుట్", "ఆర్డర్", "కొనండి", "ఆర్డర్ పెట్టు", "konandi",
    # Hindi
    "चेकआउट", "खरीदना", "ऑर्डर करो", "खरीदो"
}

ORDERS_WORDS_I18N = {
    # English
    "orders", "my orders", "track", "track order", "status", "order status", "where is my order",
    # Telugu
    "ఆర్డర్లు", "నా ఆర్డర్", "ట్రాక్", "ఆర్డర్ ఎక్కడ ఉంది", "నా ఆర్డర్లు",
    # Hindi
    "ऑर्डर", "मेरे ऑर्डर", "ट्रैक", "ऑर्डर कहाँ है", "कहाँ पहुँचा", "ऑर्डर स्थिति"
}

LANG_WORDS_I18N = {
    "language", "lang", "bhasha", "భాష", "భాష మార్చండి", "తెలుగు", "హిందీ", "भाषा", "भाषा बदलें", "हिंदी", "english"
}

STATUS_LABELS = {
    LANG_EN: {
        "pending": "⏳ Waiting for store",
        "accepted": "✅ Accepted",
        "preparing": "👨‍🍳 Preparing",
        "packed": "📦 Packed",
        "out_for_delivery": "🛵 Out for delivery",
        "delivered": "🎉 Delivered",
        "rejected": "❌ Rejected",
        "cancelled": "🛑 Cancelled",
    },
    LANG_TE: {
        "pending": "⏳ స్టోర్ కోసం వేచి ఉంది",
        "accepted": "✅ ఆమోదించబడింది",
        "preparing": "👨‍🍳 సిద్ధం చేస్తున్నారు",
        "packed": "📦 ప్యాక్ చేయబడింది",
        "out_for_delivery": "🛵 డెలివరీలో ఉంది",
        "delivered": "🎉 డెలివరీ చేయబడింది",
        "rejected": "❌ తిరస్కరించబడింది",
        "cancelled": "🛑 రద్దు చేయబడింది",
    },
    LANG_HI: {
        "pending": "⏳ दुकान की स्वीकृति प्रतीक्षित",
        "accepted": "✅ स्वीकृत",
        "preparing": "👨‍🍳 तैयार हो रहा है",
        "packed": "📦 पैक हो गया",
        "out_for_delivery": "🛵 डिलीवरी पर निकला",
        "delivered": "🎉 डिलीवर हो गया",
        "rejected": "❌ अस्वीकृत",
        "cancelled": "🛑 रद्द किया गया",
    },
}

# ---------------- UI Strings & Templates ----------------

STRINGS = {
    # Menu rows (Title <= 24 chars, Desc <= 72 chars)
    "menu_browse_title": {
        LANG_EN: "📂 Browse categories",
        LANG_TE: "📂 కేటగిరీలు",
        LANG_HI: "📂 श्रेणियां देखें",
    },
    "menu_browse_desc": {
        LANG_EN: "See everything we sell",
        LANG_TE: "అన్ని వస్తువులను చూడండి",
        LANG_HI: "दुकान का सारा सामान देखें",
    },
    "menu_cart_title": {
        LANG_EN: "🧺 My cart",
        LANG_TE: "🧺 నా కార్ట్",
        LANG_HI: "🧺 मेरी कार्ट",
    },
    "menu_cart_empty": {
        LANG_EN: "Empty",
        LANG_TE: "ఖాళీగా ఉంది",
        LANG_HI: "खाली है",
    },
    "menu_cart_filled": {
        LANG_EN: "{count} items · {total}",
        LANG_TE: "{count} వస్తువులు · {total}",
        LANG_HI: "{count} सामान · {total}",
    },
    "menu_orders_title": {
        LANG_EN: "📦 My orders",
        LANG_TE: "📦 నా ఆర్డర్లు",
        LANG_HI: "📦 मेरे ऑर्डर",
    },
    "menu_orders_desc": {
        LANG_EN: "Track, cancel or reorder",
        LANG_TE: "ట్రాక్ చేయండి లేదా రీఆర్డర్",
        LANG_HI: "ट्रैक, कैंसिल या दोबारा ऑर्डर",
    },
    "menu_store_title": {
        LANG_EN: "💬 Talk to the store",
        LANG_TE: "💬 స్టోర్‌తో చాట్",
        LANG_HI: "💬 दुकान से बात करें",
    },
    "menu_store_desc": {
        LANG_EN: "Chat with our staff",
        LANG_TE: "మా సిబ్బందితో మాట్లాడండి",
        LANG_HI: "स्टाफ से सहायता लें",
    },
    "menu_lang_title": {
        LANG_EN: "🌐 Language",
        LANG_TE: "🌐 భాష మార్చండి",
        LANG_HI: "🌐 भाषा बदलें",
    },
    "menu_lang_desc": {
        LANG_EN: "English · తెలుగు · हिंदी",
        LANG_TE: "English · తెలుగు · हिंदी",
        LANG_HI: "English · తెలుగు · हिंदी",
    },
    "menu_switch_title": {
        LANG_EN: "🏪 Switch shop",
        LANG_TE: "🏪 స్టోర్ మార్చండి",
        LANG_HI: "🏪 दुकान बदलें",
    },
    "menu_switch_desc": {
        LANG_EN: "Order from another shop",
        LANG_TE: "వేరే స్టోర్ నుండి ఆర్డర్ చేయండి",
        LANG_HI: "दूसरी दुकान से ऑर्डर करें",
    },
    "btn_menu": {
        LANG_EN: "Menu",
        LANG_TE: "మెనూ",
        LANG_HI: "मेनू",
    },
    "section_main_menu": {
        LANG_EN: "Main menu",
        LANG_TE: "ప్రధాన మెనూ",
        LANG_HI: "मुख्य मेनू",
    },
    "greeting_hint_examples": {
        LANG_EN: "Just type what you need — e.g. {examples} — or open the menu 👇",
        LANG_TE: "మీకు కావాల్సిన వస్తువు పేరు టైప్ చేయండి — ఉదా: {examples} — లేదా మెనూ తెరవండి 👇",
        LANG_HI: "आपको जो चाहिए उसका नाम लिखें — जैसे {examples} — या मेनू खोलें 👇",
    },
    "prompt_what_to_buy": {
        LANG_EN: "*What would you like to buy?*\nJust type a product name — e.g. {examples} — or open the menu 👇",
        LANG_TE: "*మీరు ఏమి కొనాలనుకుంటున్నారు?*\nవస్తువు పేరు టైప్ చేయండి — ఉదా: {examples} — లేదా మెనూ తెరవండి 👇",
        LANG_HI: "*आप क्या खरीदना चाहते हैं?*\nसामान का नाम लिखें — जैसे {examples} — या मेनू खोलें 👇",
    },
    "greeting_no_products": {
        LANG_EN: "🛍️ We're still adding our products here — please check back soon!\nYou can track past orders or message us from the menu 👇",
        LANG_TE: "🛍️ మేము వస్తువులను జోడిస్తున్నాము — దయచేసి త్వరలో మళ్లీ చూడండి!\nమెనూ నుండి మీ ఆర్డర్‌లను ట్రాక్ చేయవచ్చు 👇",
        LANG_HI: "🛍️ हम अभी सामान जोड़ रहे हैं — कृपया जल्द ही फिर देखें!\nमेनू से आप पिछले ऑर्डर ट्रैक कर सकते हैं 👇",
    },
    # Categories
    "categories_prompt": {
        LANG_EN: "📂 *Pick a category*\n(or just type what you're looking for)",
        LANG_TE: "📂 *కేటగిరీని ఎంచుకోండి*\n(లేదా కావాల్సిన వస్తువు పేరు టైప్ చేయండి)",
        LANG_HI: "📂 *श्रेणी चुनें*\n(या जो चाहिए उसका नाम लिखें)",
    },
    "btn_categories": {
        LANG_EN: "Categories",
        LANG_TE: "కేటగిరీలు",
        LANG_HI: "श्रेणियां",
    },
    "categories_empty": {
        LANG_EN: "🛍️ Nothing is in stock right now — please check back soon!",
        LANG_TE: "🛍️ ప్రస్తుతం ఏ వస్తువులూ అందుబాటులో లేవు — దయచేసి త్వరలో చూడండి!",
        LANG_HI: "🛍️ अभी कोई सामान उपलब्ध नहीं है — कृपया जल्द देखें!",
    },
    # Products & Search
    "out_of_stock_msg": {
        LANG_EN: "{heading}\n\n😕 Sorry, {items} out of stock right now.\nTry another product name.",
        LANG_TE: "{heading}\n\n😕 క్షమించండి, {items} ప్రస్తుతం అందుబాటులో లేవు.\nమరో వస్తువు పేరు టైప్ చేయండి.",
        LANG_HI: "{heading}\n\n😕 क्षमा करें, {items} अभी उपलब्ध नहीं है।\nकृपया कोई अन्य सामान खोजें।",
    },
    "choose_items_tip": {
        LANG_EN: "👉 Tap *Choose items* and tick everything you want.",
        LANG_TE: "👉 *వస్తువులు ఎంచుకోండి* పై నొక్కి కావలసినవి టిక్ చేయండి.",
        LANG_HI: "👉 *सामान चुनें* दबाएं और जो चाहिए टिक करें।",
    },
    "btn_choose_items": {
        LANG_EN: "Choose items",
        LANG_TE: "వస్తువులు ఎంచుకోండి",
        LANG_HI: "सामान चुनें",
    },
    "btn_choose_item": {
        LANG_EN: "Choose item",
        LANG_TE: "వస్తువు ఎంచుకోండి",
        LANG_HI: "सामान चुनें",
    },
    "section_available_now": {
        LANG_EN: "Available now",
        LANG_TE: "ప్రస్తుతం ఉన్నవి",
        LANG_HI: "उपलब्ध सामान",
    },
    "search_not_found": {
        LANG_EN: "😕 Sorry, we couldn't find *{query}*.\nTry another name{examples} or browse our categories.",
        LANG_TE: "😕 క్షమించండి, *{query}* కనిపించలేదు.\nమరో పేరు ప్రయత్నించండి{examples} లేదా కేటగిరీలు చూడండి.",
        LANG_HI: "😕 क्षमा करें, हमें *{query}* नहीं मिला।\nकोई अन्य नाम खोजें{examples} या श्रेणियां देखें।",
    },
    "search_results_heading": {
        LANG_EN: "🔍 Results for *{query}*",
        LANG_TE: "🔍 *{query}* కోసం ఫలితాలు",
        LANG_HI: "🔍 *{query}* के परिणाम",
    },
    # Quantity
    "item_no_longer_available": {
        LANG_EN: "😕 That item is no longer available.",
        LANG_TE: "😕 ఆ వస్తువు ప్రస్తుతం అందుబాటులో లేదు.",
        LANG_HI: "😕 यह सामान अब उपलब्ध नहीं है।",
    },
    "ask_quantity_prompt": {
        LANG_EN: "How many would you like?",
        LANG_TE: "ఎన్ని కావాలో ఎంచుకోండి?",
        LANG_HI: "आपको कितने चाहिए?",
    },
    "btn_choose_quantity": {
        LANG_EN: "Choose quantity",
        LANG_TE: "పరిమాణం ఎంచుకోండి",
        LANG_HI: "मात्रा चुनें",
    },
    "section_quantity": {
        LANG_EN: "Quantity",
        LANG_TE: "పరిమాణం",
        LANG_HI: "मात्रा",
    },
    "only_n_left": {
        LANG_EN: "Only {n} left",
        LANG_TE: "{n} మాత్రమే మిగిలి ఉన్నాయి",
        LANG_HI: "केवल {n} बचे हैं",
    },
    # Cart
    "added_to_cart": {
        LANG_EN: "✅ Added *{qty} × {item}*\n🧺 Cart: {count} items · *{total}*",
        LANG_TE: "✅ జోడించబడింది: *{qty} × {item}*\n🧺 కార్ట్: {count} వస్తువులు · *{total}*",
        LANG_HI: "✅ कार्ट में जोड़ा गया: *{qty} × {item}*\n🧺 कार्ट: {count} सामान · *{total}*",
    },
    "added_many_heading": {
        LANG_EN: "✅ *Added to cart:*",
        LANG_TE: "✅ *కార్ట్‌కు జోడించబడ్డాయి:*",
        LANG_HI: "✅ *कार्ट में जोड़े गए:*",
    },
    "nothing_added": {
        LANG_EN: "😕 Nothing was added.",
        LANG_TE: "😕 ఏమీ జోడించబడలేదు.",
        LANG_HI: "😕 कुछ भी नहीं जोड़ा गया।",
    },
    "cart_empty_prompt": {
        LANG_EN: "🧺 Your cart is empty.\nType a product name to start shopping 🔍",
        LANG_TE: "🧺 మీ కార్ట్ ఖాళీగా ఉంది.\nషాపింగ్ ప్రారంభించడానికి వస్తువు పేరు టైప్ చేయండి 🔍",
        LANG_HI: "🧺 आपकी कार्ट खाली है।\nखरीदारी शुरू करने के लिए सामान का नाम लिखें 🔍",
    },
    "cart_heading": {
        LANG_EN: "🧺 *Your cart*",
        LANG_TE: "🧺 *మీ కార్ట్*",
        LANG_HI: "🧺 *आपकी कार्ट*",
    },
    "subtotal": {
        LANG_EN: "Subtotal",
        LANG_TE: "ఉపమొత్తం",
        LANG_HI: "उप-कुल",
    },
    "delivery": {
        LANG_EN: "Delivery",
        LANG_TE: "డెలివరీ",
        LANG_HI: "डिलीवरी",
    },
    "total": {
        LANG_EN: "Total",
        LANG_TE: "మొత్తం",
        LANG_HI: "कुल",
    },
    "free": {
        LANG_EN: "FREE",
        LANG_TE: "ఉచితం",
        LANG_HI: "मुफ़्त",
    },
    "btn_add_more": {
        LANG_EN: "➕ Add more",
        LANG_TE: "➕ మరిన్ని జోడించు",
        LANG_HI: "➕ और जोड़ें",
    },
    "btn_view_cart": {
        LANG_EN: "🧺 View cart",
        LANG_TE: "🧺 కార్ట్ చూడండి",
        LANG_HI: "🧺 कार्ट देखें",
    },
    "btn_checkout": {
        LANG_EN: "✅ Checkout",
        LANG_TE: "✅ చెక్అవుట్",
        LANG_HI: "✅ चेकआउट",
    },
    "btn_edit_cart": {
        LANG_EN: "✏️ Edit cart",
        LANG_TE: "✏️ కార్ట్ మార్చండి",
        LANG_HI: "✏️ कार्ट बदलें",
    },
    "btn_change_qty": {
        LANG_EN: "✏️ Change qty",
        LANG_TE: "✏️ మార్చండి",
        LANG_HI: "✏️ मात्रा बदलें",
    },
    "change_qty_hint": {
        LANG_EN: "Need more than 1 of something? Tap *✏️ Change qty*.",
        LANG_TE: "ఒకటి కంటే ఎక్కువ కావాలా? *✏️ మార్చండి* నొక్కండి.",
        LANG_HI: "एक से ज़्यादा चाहिए? *✏️ मात्रा बदलें* दबाएं।",
    },
    "btn_talk_store": {
        LANG_EN: "💬 Talk to store",
        LANG_TE: "💬 స్టోర్‌తో చాట్",
        LANG_HI: "💬 दुकान से बात करें",
    },
    "btn_home_menu": {
        LANG_EN: "🏠 Menu",
        LANG_TE: "🏠 మెనూ",
        LANG_HI: "🏠 मेनू",
    },
    # Edit Cart
    "edit_cart_prompt": {
        LANG_EN: "✏️ *Edit cart*\nPick an item to change its quantity or remove it.",
        LANG_TE: "✏️ *కార్ట్ మార్చండి*\nపరిమాణం మార్చడానికి లేదా తీసివేయడానికి వస్తువును ఎంచుకోండి.",
        LANG_HI: "✏️ *कार्ट बदलें*\nमात्रा बदलने या हटाने के लिए सामान चुनें।",
    },
    "btn_edit_items": {
        LANG_EN: "Edit items",
        LANG_TE: "మార్చండి",
        LANG_HI: "बदलें",
    },
    "row_clear_cart": {
        LANG_EN: "🗑️ Clear whole cart",
        LANG_TE: "🗑️ కార్ట్ ఖాళీ చేయండి",
        LANG_HI: "🗑️ पूरी कार्ट खाली करें",
    },
    "row_remove_item": {
        LANG_EN: "❌ Remove from cart",
        LANG_TE: "❌ కార్ట్ నుండి తీసివేయి",
        LANG_HI: "❌ कार्ट से हटाएं",
    },
    # Checkout & Address
    "address_choice_prompt": {
        LANG_EN: "🚚 *Delivery address*\n{addr}\n\nDeliver here?",
        LANG_TE: "🚚 *డెలివరీ చిరునామా*\n{addr}\n\nఇక్కడికే డెలివరీ చేయాలా?",
        LANG_HI: "🚚 *डिलीवरी पता*\n{addr}\n\nक्या इसी पते पर मंगाना है?",
    },
    "btn_deliver_here": {
        LANG_EN: "📍 Yes, deliver here",
        LANG_TE: "📍 అవును, ఇక్కడికే",
        LANG_HI: "📍 हां, इसी पते पर",
    },
    "btn_new_address": {
        LANG_EN: "✏️ New address",
        LANG_TE: "✏️ కొత్త చిరునామా",
        LANG_HI: "✏️ नया पता",
    },
    "ask_address_prompt": {
        LANG_EN: "📝 Please type your *delivery address*\n(house/flat no., street, area, landmark)\n\nor share your 📍 *location* (📎 → Location).",
        LANG_TE: "📝 దయచేసి మీ *డెలివరీ చిరునామా* టైప్ చేయండి\n(ఇంటి నెం., వీధి, ప్రాంతం, ల్యాండ్‌మార్క్)\n\nలేదా 📍 *లొకేషన్* షేర్ చేయండి (📎 → Location).",
        LANG_HI: "📝 कृपया अपना *डिलीवरी पता* लिखें\n(मकान/फ्लैट नं., गली, इलाका, लैंडमार्क)\n\nया अपना 📍 *लोकेशन* शेयर करें (📎 → Location)।",
    },
    "address_too_short": {
        LANG_EN: "🙏 That address looks too short. Please include house/flat no., street and area.",
        LANG_TE: "🙏 చిరునామా అసంపూర్తిగా ఉంది. దయచేసి ఇంటి నెం., వీధి మరియు ప్రాంతం వివరాలు ఇవ్వండి.",
        LANG_HI: "🙏 यह पता बहुत छोटा लग रहा है। कृपया मकान नं., गली और इलाके का नाम शामिल करें।",
    },
    "address_saved": {
        LANG_EN: "✅ Address saved.",
        LANG_TE: "✅ చిరునామా సేవ్ చేయబడింది.",
        LANG_HI: "✅ पता सेव हो गया।",
    },
    # Payment
    "ask_payment_prompt": {
        LANG_EN: "💳 *How would you like to pay?*",
        LANG_TE: "💳 *మీరు ఎలా చెల్లించాలనుకుంటున్నారు?*",
        LANG_HI: "💳 *आप भुगतान कैसे करना चाहेंगे?*",
    },
    "btn_pay_cod": {
        LANG_EN: "💵 Cash on Delivery",
        LANG_TE: "💵 క్యాష్ ఆన్ డెలివరీ",
        LANG_HI: "💵 कैश ऑन डिलीवरी",
    },
    "btn_pay_upi": {
        LANG_EN: "📲 UPI / GPay",
        LANG_TE: "📲 యూపీఐ / GPay",
        LANG_HI: "📲 यूपीआई / GPay",
    },
    # Review & Confirmation
    "order_summary_heading": {
        LANG_EN: "🧾 *Order summary*",
        LANG_TE: "🧾 *ఆర్డర్ సారాంశం*",
        LANG_HI: "🧾 *ऑर्डर का विवरण*",
    },
    "confirm_order_ask": {
        LANG_EN: "*Confirm your order?*",
        LANG_TE: "*ఆర్డర్ నిర్ధారించాలా?*",
        LANG_HI: "*क्या ऑर्डर कन्फर्म करें?*",
    },
    "btn_confirm_order": {
        LANG_EN: "✅ Confirm order",
        LANG_TE: "✅ ఆర్డర్ కన్ఫర్మ్",
        LANG_HI: "✅ ऑर्डर कन्फर्म",
    },
    "btn_cancel": {
        LANG_EN: "❌ Cancel",
        LANG_TE: "❌ రద్దు చేయండి",
        LANG_HI: "❌ रद्द करें",
    },
    "order_not_placed": {
        LANG_EN: "👍 Okay, order not placed. Your cart is saved.",
        LANG_TE: "👍 సరే, ఆర్డర్ చేయలేదు. మీ కార్ట్ అలాగే భద్రంగా ఉంది.",
        LANG_HI: "👍 ठीक है, ऑर्डर नहीं किया गया। आपकी कार्ट सुरक्षित है।",
    },
    # Order Placed
    "order_placed_body": {
        LANG_EN: "🎉 *Order placed!*\n\nOrder ID: *{code}*\nTotal: *{total}* ({pay})\n\n"
                 "⏳ Waiting for the store to accept. You'll get updates here:\n"
                 "Accepted → Packed → Out for delivery → Delivered",
        LANG_TE: "🎉 *ఆర్డర్ నమోదైంది!*\n\nఆర్డర్ ID: *{code}*\nమొత్తం: *{total}* ({pay})\n\n"
                 "⏳ స్టోర్ ఆమోదం కోసం వేచి ఉంది. అప్‌డేట్‌లు ఇక్కడే అందుతాయి:\n"
                 "ఆమోదించబడింది → ప్యాక్ చేయబడింది → డెలివరీలో ఉంది → చేరింది",
        LANG_HI: "🎉 *ऑर्डर दर्ज हो गया!*\n\nऑर्डर आईडी: *{code}*\nकुल राशि: *{total}* ({pay})\n\n"
                 "⏳ दुकान द्वारा स्वीकार किए जाने की प्रतीक्षा है। अपडेट यहाँ मिलेंगे:\n"
                 "स्वीकृत → पैक → डिलीवरी पर → डिलीवर",
    },
    "btn_track_order": {
        LANG_EN: "📦 Track order",
        LANG_TE: "📦 ట్రాక్ చేయండి",
        LANG_HI: "📦 ऑर्डर ट्रैक करें",
    },
    "pay_link_text": {
        LANG_EN: "💳 *Pay now:* {url}",
        LANG_TE: "💳 *ఇప్పుడే చెల్లించండి:* {url}",
        LANG_HI: "💳 *अभी भुगतान करें:* {url}",
    },
    "pay_cod_note": {
        LANG_EN: "💵 Please pay cash or UPI at delivery.",
        LANG_TE: "💵 డెలివరీ సమయంలో నగదు లేదా యూపీఐ ద్వారా చెల్లించండి.",
        LANG_HI: "💵 कृपया डिलीवरी पर नकद या यूपीआई से भुगतान करें।",
    },
    # Order Tracking & History
    "no_orders_placed": {
        LANG_EN: "📦 You haven't placed any orders yet.\nType a product name to start shopping 🔍",
        LANG_TE: "📦 మీరు ఇంకా ఎలాంటి ఆర్డర్‌లు చేయలేదు.\nషాపింగ్ ప్రారంభించడానికి వస్తువు పేరు టైప్ చేయండి 🔍",
        LANG_HI: "📦 आपने अभी तक कोई ऑर्डर नहीं दिया है।\nखरीदारी शुरू करने के लिए सामान का नाम लिखें 🔍",
    },
    "orders_list_prompt": {
        LANG_EN: "📦 *Your orders*\nPick one to track, cancel or reorder.",
        LANG_TE: "📦 *మీ ఆర్డర్లు*\nట్రాక్ చేయడానికి, రద్దు చేయడానికి లేదా మళ్లీ ఆర్డర్ చేయడానికి ఎంచుకోండి.",
        LANG_HI: "📦 *आपके ऑर्डर*\nट्रैक, कैंसिल या दोबारा ऑर्डर के लिए चुनें।",
    },
    "btn_view_orders": {
        LANG_EN: "View orders",
        LANG_TE: "ఆర్డర్లు",
        LANG_HI: "ऑर्डर देखें",
    },
    "section_recent_orders": {
        LANG_EN: "Recent orders",
        LANG_TE: "ఇటీవలి ఆర్డర్లు",
        LANG_HI: "हाल के ऑर्डर",
    },
    "btn_cancel_order": {
        LANG_EN: "❌ Cancel order",
        LANG_TE: "❌ ఆర్డర్ రద్దు చేయి",
        LANG_HI: "❌ ऑर्डर रद्द करें",
    },
    "btn_reorder": {
        LANG_EN: "🔁 Reorder",
        LANG_TE: "🔁 మళ్లీ ఆర్డర్",
        LANG_HI: "🔁 दोबारा ऑर्डर",
    },
    "ask_cancel_prompt": {
        LANG_EN: "Cancel order *{code}*?",
        LANG_TE: "ఆర్డర్ *{code}* రద్దు చేయాలా?",
        LANG_HI: "क्या ऑर्डर *{code}* रद्द करना है?",
    },
    "btn_yes_cancel": {
        LANG_EN: "Yes, cancel it",
        LANG_TE: "అవును, రద్దు చేయి",
        LANG_HI: "हां, रद्द करें",
    },
    "btn_no_keep": {
        LANG_EN: "No, keep it",
        LANG_TE: "వద్దు, ఉంచండి",
        LANG_HI: "नहीं, रहने दें",
    },
    "order_cancelled_done": {
        LANG_EN: "🛑 Order *{code}* has been cancelled.",
        LANG_TE: "🛑 ఆర్డర్ *{code}* రద్దు చేయబడింది.",
        LANG_HI: "🛑 ऑर्डर *{code}* रद्द कर दिया गया है।",
    },
    "cant_cancel_anymore": {
        LANG_EN: "⚠️ This order can't be cancelled any more — it's already on its way. Use *Talk to the store*.",
        LANG_TE: "⚠️ ఈ ఆర్డర్ ఇప్పటికే బయలుదేరింది, రద్దు చేయలేము. దయచేసి *స్టోర్‌తో చాట్* చేయండి.",
        LANG_HI: "⚠️ यह ऑर्डर अब रद्द नहीं हो सकता — यह निकल चुका है। सहायता के लिए *दुकान से बात करें*।",
    },
    # Store Chat
    "chat_store_welcome": {
        LANG_EN: "💬 You're now chatting with our store team. Type your message and they'll reply here.\nType *menu* anytime to go back.",
        LANG_TE: "💬 మీరు ఇప్పుడు మా స్టోర్ బృందంతో చాట్ చేస్తున్నారు. మీ సందేశాన్ని టైప్ చేయండి, వారు త్వరలో సమాధానం ఇస్తారు.\nవెనక్కి వెళ్ళడానికి *menu* అని టైప్ చేయండి.",
        LANG_HI: "💬 आप अब हमारी दुकान टीम से बात कर रहे हैं। अपना संदेश लिखें, वे जल्द ही जवाब देंगे।\nवापस जाने के लिए *menu* लिखें।",
    },
    "chat_store_sent": {
        LANG_EN: "✅ Sent to the store team. They'll reply shortly.",
        LANG_TE: "✅ స్టోర్ బృందానికి పంపబడింది. త్వరలో సమాధానం ఇస్తారు.",
        LANG_HI: "✅ दुकान टीम को भेज दिया गया है। वे जल्द ही जवाब देंगे।",
    },
    # Store Closed
    "closed_notice": {
        LANG_EN: "🔴 *{store_name}* isn't taking orders right now.\nReason: {reason}\n\nWe'll be back soon 🙏 You can still track your orders or message us.",
        LANG_TE: "🔴 *{store_name}* ప్రస్తుతం ఆర్డర్లు స్వీకరించడం లేదు.\nకారణం: {reason}\n\nత్వరలో మళ్లీ అందుబాటులోకి వస్తాము 🙏 మీరు ఇప్పటికీ మీ ఆర్డర్‌లను ట్రాక్ చేయవచ్చు లేదా మాకు మెసేజ్ చేయవచ్చు.",
        LANG_HI: "🔴 *{store_name}* अभी ऑर्डर नहीं ले रहा है।\nकारण: {reason}\n\nहम जल्द ही वापस आएंगे 🙏 आप अपने ऑर्डर ट्रैक कर सकते हैं या संदेश भेज सकते हैं।",
    },
    # Location
    "location_saved": {
        LANG_EN: "📍 Location saved for delivery.",
        LANG_TE: "📍 డెలివరీ కోసం లొకేషన్ సేవ్ చేయబడింది.",
        LANG_HI: "📍 डिलीवरी के लिए लोकेशन सेव हो गया।",
    },
    "location_saved_tip": {
        LANG_EN: "✅ Location saved. Tip: you can also type your flat/house number if it's an apartment.",
        LANG_TE: "✅ లొకేషన్ సేవ్ చేయబడింది. సూచన: అపార్ట్‌మెంట్ అయితే మీ ఫ్లాట్/ఇంటి నంబర్‌ను కూడా టైప్ చేయవచ్చు.",
        LANG_HI: "✅ लोकेशन सेव हो गया। सुझाव: यदि फ्लैट/अपार्टमेंट है तो आप मकान नंबर भी लिख सकते हैं।",
    },
    # Language Switcher
    "choose_language_prompt": {
        LANG_EN: "🌐 *Choose your language / భాషను ఎంచుకోండి / भाषा चुनें:*",
        LANG_TE: "🌐 *భాషను ఎంచుకోండి / Choose your language / भाषा चुनें:*",
        LANG_HI: "🌐 *भाषा चुनें / Choose your language / భాషను ఎంచుకోండి:*",
    },
    "lang_changed": {
        LANG_EN: "✅ Language set to English.",
        LANG_TE: "✅ భాష తెలుగుగా మార్చబడింది. (Language set to Telugu)",
        LANG_HI: "✅ भाषा बदलकर हिंदी कर दी गई है। (Language set to Hindi)",
    },
}


def t(key: str, lang: str | None = None, **kwargs) -> str:
    """Retrieve translated template string, with formatting kwargs."""
    l = normalize_language(lang)
    entry = STRINGS.get(key)
    if not entry:
        return key
    template = entry.get(l) or entry.get(LANG_EN, key)
    if kwargs:
        try:
            return template.format(**kwargs)
        except KeyError:
            return template
    return template


def status_label(status: str, lang: str | None = None) -> str:
    l = normalize_language(lang)
    labels = STATUS_LABELS.get(l, STATUS_LABELS[LANG_EN])
    return labels.get(status, status)


def language_picker_buttons() -> list[tuple[str, str]]:
    """Buttons to switch language (WhatsApp reply buttons max 20 chars)."""
    return [
        ("lang:en", "🇬🇧 English"),
        ("lang:te", "🇮🇳 తెలుగు (Telugu)"),
        ("lang:hi", "🇮🇳 हिंदी (Hindi)"),
    ]


def status_notification_message(o: dict, status: str, note: str | None, lang: str | None = None,
                                store_name: str = "the store", total_str: str = "") -> str | None:
    """Localized order notification message for dashboard-driven status updates."""
    l = normalize_language(lang)
    code = o["code"]
    reason = f"\nReason: {note}" if note else ""
    if l == LANG_TE:
        reason = f"\nకారణం: {note}" if note else ""
        if status == "accepted":
            msg = f"✅ మీ ఆర్డర్ *{code}* స్వీకరించబడింది! ప్యాక్ చేస్తున్నాము.\n🕒 డెలివరీ సమయం: {o['eta']}"
            return msg
        if status == "rejected":
            return (f"❌ క్షమించండి, మీ ఆర్డర్ *{code}* ఆమోదించలేకపోయాము.{reason}\n"
                    + ("మీ చెల్లింపు రీఫండ్ చేయబడుతుంది. " if o.get("payment_status") in ("paid", "refund_due") else "")
                    + "వేరే ఏదైనా ఆర్డర్ చేయాలనుకుంటే ఇక్కడ రిప్లై ఇవ్వండి.")
        if status == "preparing":
            return f"👨‍🍳 మీ ఆర్డర్ *{code}* సిద్ధం చేస్తున్నాము."
        if status == "packed":
            return f"📦 మీ ఆర్డర్ *{code}* ప్యాక్ చేయబడింది, త్వరలో బయలుదేరుతుంది."
        if status == "out_for_delivery":
            cash = f"\nదయచేసి {total_str} సిద్ధంగా ఉంచుకోండి (నగదు లేదా యూపీఐ)." if o.get("payment_status") != "paid" else ""
            return f"🛵 మీ ఆర్డర్ *{code}* డెలివరీ కోసం బయలుదేరింది!{cash}"
        if status == "delivered":
            return (f"🎉 మీ ఆర్డర్ *{code}* డెలివరీ చేయబడింది. {store_name} వద్ద కొనుగోలు చేసినందుకు ధన్యవాదాలు!\n"
                    "మళ్లీ ఆర్డర్ చేయడానికి *reorder* అని రిప్లై ఇవ్వండి.")
        if status == "cancelled":
            return f"🛑 మీ ఆర్డర్ *{code}* రద్దు చేయబడింది.{reason}"
        if status == "payment_paid":
            return f"💰 ఆర్డర్ *{code}* కోసం {total_str} చెల్లింపు అందింది. ధన్యవాదాలు!"
        if status == "payment_refunded":
            return f"↩️ ఆర్డర్ *{code}* కోసం {total_str} రీఫండ్ ప్రాసెస్ చేయబడింది."

    elif l == LANG_HI:
        reason = f"\nकारण: {note}" if note else ""
        if status == "accepted":
            msg = f"✅ आपका ऑर्डर *{code}* स्वीकार कर लिया गया है! हम इसे पैक कर रहे हैं।\n🕒 संभावित समय: {o['eta']}"
            return msg
        if status == "rejected":
            return (f"❌ क्षमा करें, हम आपका ऑर्डर *{code}* स्वीकार नहीं कर सके।{reason}\n"
                    + ("आपका भुगतान वापस (रिफंड) कर दिया जाएगा। " if o.get("payment_status") in ("paid", "refund_due") else "")
                    + "यदि आप कुछ और मंगाना चाहते हैं तो यहाँ लिखें।")
        if status == "preparing":
            return f"👨‍🍳 आपका ऑर्डर *{code}* तैयार किया जा रहा है।"
        if status == "packed":
            return f"📦 आपका ऑर्डर *{code}* पैक हो गया है और जल्द निकलेगा।"
        if status == "out_for_delivery":
            cash = f"\nकृपया {total_str} तैयार रखें (नकद या यूपीआई)।" if o.get("payment_status") != "paid" else ""
            return f"🛵 आपका ऑर्डर *{code}* डिलीवरी के लिए निकल चुका है!{cash}"
        if status == "delivered":
            return (f"🎉 आपका ऑर्डर *{code}* डिलीवर हो गया है। {store_name} से खरीदारी के लिए धन्यवाद!\n"
                    "दोबारा ऑर्डर करने के लिए कभी भी *reorder* लिखें।")
        if status == "cancelled":
            return f"🛑 आपका ऑर्डर *{code}* रद्द कर दिया गया है।{reason}"
        if status == "payment_paid":
            return f"💰 ऑर्डर *{code}* के लिए {total_str} का भुगतान प्राप्त हुआ। धन्यवाद!"
        if status == "payment_refunded":
            return f"↩️ ऑर्डर *{code}* के लिए {total_str} का रिफंड प्रोसेस हो गया है।"

    # Default / English
    if status == "accepted":
        msg = f"✅ Your order *{code}* has been accepted! We're packing it now.\n🕒 Expected delivery: {o['eta']}"
        return msg
    if status == "rejected":
        return (f"❌ Sorry, we couldn't accept order *{code}*.{reason}\n"
                + ("Your payment will be refunded. " if o.get("payment_status") in ("paid", "refund_due") else "")
                + "Reply here if you'd like to order something else.")
    if status == "preparing":
        return f"👨‍🍳 We're preparing your order *{code}* now."
    if status == "packed":
        return f"📦 Order *{code}* is packed and will leave the store shortly."
    if status == "out_for_delivery":
        cash = f"\nPlease keep {total_str} ready (cash or UPI)." if o.get("payment_status") != "paid" else ""
        return f"🛵 Order *{code}* is out for delivery!{cash}"
    if status == "delivered":
        return (f"🎉 Order *{code}* delivered. Thank you for shopping with {store_name}!\n"
                "Reply *reorder* anytime to get the same items again.")
    if status == "cancelled":
        return f"🛑 Order *{code}* has been cancelled.{reason}"
    if status == "payment_paid":
        return f"💰 Payment of {total_str} received for order *{code}*. Thank you!"
    if status == "payment_refunded":
        return f"↩️ Refund of {total_str} for order *{code}* has been processed."
    return None
