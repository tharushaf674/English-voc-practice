import os
import json
import re
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google import genai
import schedule
import time
import asyncio
from threading import Thread
from flask import Flask

# ─────────────────────────────────────────
#  CONFIGURATION  (replace your keys here)
# ─────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '8429363146:AAGxxiMGwnjfFS3EdOA9cn9p-ic35R9fhAM')
GEMINI_API_KEY     = os.getenv('GEMINI_API_KEY',     '')
PORT               = int(os.getenv('PORT', 8000))  # Koyeb uses PORT env variable

CHAT_ID_FILE = 'user_chat_id.txt'
WORDS_FILE   = 'english_words.json'

# Gemini client
client       = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_MODEL = 'models/gemini-2.5-flash'

# Telegram max chars per message
TELEGRAM_MAX_CHARS = 4000

# Flask app - keeps Koyeb web service alive
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    words = load_words()
    return f"✅ Vocab Bot is running! Words saved: {len(words)}"

@flask_app.route('/health')
def health():
    return "OK", 200

print("✅ Vocab Bot started")
print(f"✅ Model  : {GEMINI_MODEL}")
print("✅ Mode   : Web Service (Koyeb free tier compatible)")


# ─────────────────────────────────────────
#  FILE HELPERS
# ─────────────────────────────────────────
def load_words():
    if os.path.exists(WORDS_FILE):
        with open(WORDS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_words(words):
    with open(WORDS_FILE, 'w', encoding='utf-8') as f:
        json.dump(words, f, ensure_ascii=False, indent=2)

def save_chat_id(chat_id):
    with open(CHAT_ID_FILE, 'w') as f:
        f.write(str(chat_id))

def load_chat_id():
    if os.path.exists(CHAT_ID_FILE):
        with open(CHAT_ID_FILE, 'r') as f:
            return int(f.read().strip())
    return None


# ─────────────────────────────────────────
#  CORE: ONE BULK GEMINI REQUEST
#  Send ALL words → ONE API call
#  Returns list of formatted strings (one per word)
# ─────────────────────────────────────────
def bulk_generate(word_list: list) -> list:
    if not word_list:
        return []

    numbered = "\n".join(f"{i+1}. {w}" for i, w in enumerate(word_list))

    prompt = f"""You are an expert English-Sinhala language teacher.

For EACH of the following {len(word_list)} English words, provide:
- An English example sentence
- The Sinhala meaning (in Sinhala unicode script, NOT romanized)
- The Sinhala translation of the sentence (in Sinhala unicode script)

Word list:
{numbered}

Format your response EXACTLY like this for EVERY word:

[1]. WORD: example
Sentence: This is an example sentence.
Sinhala Meaning: නිදසුන
Sinhala Sentence: මෙය නිදසුන් වාක්‍යයකි.

[2]. WORD: next word
...and so on for all {len(word_list)} words.

RULES:
- Do NOT skip any word
- Do NOT add extra text or commentary
- Sinhala MUST be in Sinhala unicode script (not English letters)
- Keep sentences simple and clear"""

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )
        raw = response.text.strip()

        # Split into per-word blocks (each starts with [NUMBER].)
        blocks = re.split(r'\n(?=\[\d+\]\.)', raw)
        results = [b.strip() for b in blocks if b.strip()]

        # Pad if any words are missing
        while len(results) < len(word_list):
            results.append(f"⚠️ Missing result for: {word_list[len(results)]}")

        return results

    except Exception as e:
        err = str(e)
        print(f"❌ Gemini error: {err}")
        return [f"⚠️ API error: {err[:100]}"] * len(word_list)


def chunk_messages(entries: list, max_chars: int = TELEGRAM_MAX_CHARS) -> list:
    """Join word entries and split into Telegram-safe chunks."""
    chunks = []
    current = ""
    separator = "\n" + "━" * 25 + "\n\n"

    for entry in entries:
        block = entry + separator
        if len(current) + len(block) > max_chars:
            if current:
                chunks.append(current.strip())
            current = block
        else:
            current += block

    if current.strip():
        chunks.append(current.strip())

    return chunks


# ─────────────────────────────────────────
#  BOT COMMANDS
# ─────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_chat_id(chat_id)
    words = load_words()

    msg = f"""🌟 English Vocabulary Practice Bot 🌟

📚 Commands:
• Send any word   → adds it + instant example
• /practice       → ALL words, ONE API call!
• /list           → see your word list
• /remove <word>  → delete a word
• /stats          → your progress

⏰ Auto daily practice at 9:00 AM every day!

💡 How it works:
ALL your words are sent to Gemini in ONE request.
No more rate limit errors!
50 words = 1 API call = a few messages!

📊 Words saved: {len(words)}

Send me a word to get started! 📝"""

    await update.message.reply_text(msg)


async def add_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    word = update.message.text.strip().lower()

    if word.startswith('/'):
        return

    if len(word) < 2 or not word.replace('-', '').replace(' ', '').isalpha():
        await update.message.reply_text("❌ Please send a valid English word.")
        return

    words = load_words()

    if any(w['word'] == word for w in words):
        await update.message.reply_text(f"📌 '{word}' is already in your list!")
        return

    words.append({
        'word': word,
        'added_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'practice_count': 0
    })
    save_words(words)

    await update.message.reply_text(f"✅ Added '{word}'!\n🔄 Generating example...")

    # Single word = 1 request
    results = bulk_generate([word])
    await update.message.reply_text(results[0] if results else "⚠️ Could not generate example.")


async def practice_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    words = load_words()

    if not words:
        await update.message.reply_text("📭 Your list is empty. Send me some words first!")
        return

    import random
    word_names = [w['word'] for w in words]
    random.shuffle(word_names)  # 🔀 Random order every time!
    total = len(word_names)

    await update.message.reply_text(
        f"📖 Generating examples for ALL {total} words...\n"
        f"⚡ ONE Gemini request — please wait 15-30 seconds..."
    )

    # ── ONE API CALL FOR ALL WORDS ──
    results = bulk_generate(word_names)

    # Update practice counts
    for w in words:
        w['practice_count'] += 1
    save_words(words)

    # Split into Telegram-safe chunks and send
    date_str = datetime.now().strftime('%Y-%m-%d')
    header   = f"📚 Practice — {total} words — {date_str}\n\n"
    chunks   = chunk_messages(results)

    for i, chunk in enumerate(chunks):
        text = (header + chunk) if i == 0 else chunk
        await update.message.reply_text(text)
        await asyncio.sleep(1)  # tiny pause between Telegram messages only

    await update.message.reply_text(
        f"✅ Done! {total} words practised with just 1 API call! 🎉"
    )


async def list_words(update: Update, context: ContextTypes.DEFAULT_TYPE):
    words = load_words()

    if not words:
        await update.message.reply_text("📭 Your vocabulary list is empty.")
        return

    msg = "📚 Your Vocabulary List:\n\n"
    for i, w in enumerate(words, 1):
        msg += f"{i}. {w['word']}  (practiced {w['practice_count']}×)\n"
    msg += f"\n📊 Total: {len(words)} words"

    await update.message.reply_text(msg)


async def remove_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: /remove <word>\nExample: /remove apple")
        return

    word_to_remove = ' '.join(context.args).lower()
    words = load_words()
    new_words = [w for w in words if w['word'] != word_to_remove]

    if len(new_words) < len(words):
        save_words(new_words)
        await update.message.reply_text(f"✅ Removed '{word_to_remove}'.")
    else:
        await update.message.reply_text(f"❌ '{word_to_remove}' not found in your list.")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    words = load_words()

    if not words:
        await update.message.reply_text("📭 No stats yet. Add some words first!")
        return

    total_words    = len(words)
    total_practice = sum(w['practice_count'] for w in words)
    avg            = total_practice / total_words if total_words > 0 else 0
    most           = max(words, key=lambda x: x['practice_count'])
    least          = min(words, key=lambda x: x['practice_count'])

    msg = f"""📊 Your Statistics:

📚 Total Words     : {total_words}
🔄 Total Practices : {total_practice}
📈 Average / Word  : {avg:.1f}

🌟 Most practiced  : {most['word']} ({most['practice_count']}×)
📝 Least practiced : {least['word']} ({least['practice_count']}×)

Keep going! 💪"""

    await update.message.reply_text(msg)


# ─────────────────────────────────────────
#  DAILY PRACTICE SCHEDULER
# ─────────────────────────────────────────
async def send_daily_practice(application):
    chat_id = load_chat_id()
    if not chat_id:
        return

    words = load_words()
    if not words:
        return

    try:
        import random
        word_names = [w['word'] for w in words]
        random.shuffle(word_names)  # 🔀 Random order every day!
        total = len(word_names)

        await application.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🌅 Good morning! Daily Vocabulary Practice\n"
                f"📚 {total} words → 1 Gemini request → sending now..."
            )
        )

        # ── ONE API CALL FOR ALL WORDS ──
        results = bulk_generate(word_names)

        # Update practice counts
        for w in words:
            w['practice_count'] += 1
        save_words(words)

        # Send in chunks
        header = f"📖 Daily Practice — {datetime.now().strftime('%Y-%m-%d')}\n\n"
        chunks = chunk_messages(results)

        for i, chunk in enumerate(chunks):
            text = (header + chunk) if i == 0 else chunk
            await application.bot.send_message(chat_id=chat_id, text=text)
            await asyncio.sleep(1)

        await application.bot.send_message(
            chat_id=chat_id,
            text=f"✅ Daily practice done! {total} words, 1 API call! Have a great day! 🌟"
        )

    except Exception as e:
        print(f"❌ Daily practice error: {e}")


def schedule_daily_practice(application):
    async def job():
        await send_daily_practice(application)

    schedule.every().day.at("09:00").do(lambda: asyncio.create_task(job()))

    while True:
        schedule.run_pending()
        time.sleep(60)


# ─────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────
def run_flask():
    """Run Flask web server - required for Koyeb Web Service"""
    flask_app.run(host='0.0.0.0', port=PORT)

def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start",    start))
    application.add_handler(CommandHandler("list",     list_words))
    application.add_handler(CommandHandler("practice", practice_all))
    application.add_handler(CommandHandler("remove",   remove_word))
    application.add_handler(CommandHandler("stats",    stats))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_word))

    # Run Flask in background thread (keeps Koyeb web service alive)
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Run scheduler in background thread
    scheduler_thread = Thread(
        target=schedule_daily_practice,
        args=(application,),
        daemon=True
    )
    scheduler_thread.start()

    print("🤖 Bot is running...")
    application.run_polling()


if __name__ == '__main__':
    main()
