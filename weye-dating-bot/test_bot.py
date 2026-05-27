import os
import asyncio
import random
import re
import time
import logging
from datetime import datetime, timedelta
from datetime import time as dt_time
from dotenv import load_dotenv
import aiosqlite
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# ========== LOAD ENVIRONMENT VARIABLES FROM .env FILE ==========
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("No BOT_TOKEN found. Set BOT_TOKEN in .env file.")

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID"))
except (TypeError, ValueError):
    raise ValueError("ADMIN_ID must be a valid integer. Set ADMIN_ID in .env file.")

# Support multiple admins (comma-separated IDs)
ADMIN_IDS = {ADMIN_ID}
extra_admins = os.getenv("EXTRA_ADMIN_IDS")
if extra_admins:
    for aid in extra_admins.split(','):
        try:
            ADMIN_IDS.add(int(aid.strip()))
        except ValueError:
            pass

TELEBIRR_MERCHANT_ID = os.getenv("TELEBIRR_MERCHANT_ID", "0939999808")

# ========== LOGGING SETUP ==========
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== DATABASE PATH ==========
DB_PATH = "weye_dating.db"

# ========== URL PATTERN FOR ANTI-SPAM ==========
URL_PATTERN = re.compile(r'(https?://\S+|www\.\S+)', re.IGNORECASE)
REPEATED_MESSAGE_CACHE = {}

# ========== BUTTON PATTERNS ==========
button_patterns = r"^(🏠 Home|👤 Profile|🔍 Search|❤️ Likes|💕 Matches|⭐ Premium|🪙 Coins|🎁 Gifts|🎁 My Gifts|📢 Complaint|✏️ Edit Profile|🚫 Delete Account|❓ Help|🔔 Reminders)$"

# ========== COOLDOWN TRACKING ==========
last_action = {}


def check_cooldown(user_id, action_name, cooldown_seconds=3):
    key = f"{user_id}_{action_name}"
    now = time.time()
    if key in last_action:
        if now - last_action[key] < cooldown_seconds:
            return False
    last_action[key] = now
    return True


def check_repeated_message(user_id, message):
    now = time.time()
    if user_id in REPEATED_MESSAGE_CACHE:
        cached_msg, count, timestamp = REPEATED_MESSAGE_CACHE[user_id]
        if cached_msg == message and now - timestamp < 30:
            new_count = count + 1
            REPEATED_MESSAGE_CACHE[user_id] = (message, new_count, timestamp)
            if new_count >= 3:
                return False
        else:
            REPEATED_MESSAGE_CACHE[user_id] = (message, 1, now)
    else:
        REPEATED_MESSAGE_CACHE[user_id] = (message, 1, now)
    return True


# ========== GIFTS ==========
GIFTS = {
    "buna": {
        "name": "☕ Buna",
        "cost": 20,
        "meaning": "A warm Ethiopian coffee invitation ❤️"
    },
    "flower": {
        "name": "🌹 Flower",
        "cost": 15,
        "meaning": "You are beautiful 🌹"
    },
    "heart": {
        "name": "❤️ Heart",
        "cost": 25,
        "meaning": "Someone likes you a lot 💕"
    },
    "doro": {
        "name": "🍗 Doro Wat",
        "cost": 40,
        "meaning": "Serious Ethiopian love 😍"
    }
}


# ========== DATABASE INITIALIZATION ==========
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")

        await db.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            age INTEGER,
            gender TEXT,
            looking_for TEXT DEFAULT 'opposite',
            bio TEXT,
            photo TEXT,
            balance INTEGER DEFAULT 0,
            premium_expiry TEXT,
            reminder INTEGER DEFAULT 1,
            current_chat_target INTEGER DEFAULT 0,
            last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        await db.execute('''
        CREATE TABLE IF NOT EXISTS swipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user INTEGER,
            to_user INTEGER,
            action TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        await db.execute('''
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user1 INTEGER,
            user2 INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user1, user2)
        )
        ''')
        await db.execute('''
        CREATE TABLE IF NOT EXISTS private_chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user1 INTEGER,
            user2 INTEGER,
            last_message TEXT,
            last_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        await db.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            sender_id INTEGER,
            message TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        await db.execute('''
        CREATE TABLE IF NOT EXISTS gifts_received (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user INTEGER,
            to_user INTEGER,
            gift_type TEXT,
            coins_cost INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        await db.execute('''
        CREATE TABLE IF NOT EXISTS premium_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            days INTEGER,
            amount INTEGER,
            transaction_id TEXT,
            status TEXT DEFAULT 'pending',
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        await db.execute('''
        CREATE TABLE IF NOT EXISTS coin_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            coins INTEGER,
            amount INTEGER,
            transaction_id TEXT,
            status TEXT DEFAULT 'pending',
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_users_gender ON users(gender);')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_users_last_activity ON users(last_activity);')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_swipes_from_user ON swipes(from_user);')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_swipes_to_user ON swipes(to_user);')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_gender_activity ON users(gender, last_activity);')
        await db.execute('''
        CREATE TABLE IF NOT EXISTS blocks (
            blocker INTEGER,
            blocked INTEGER,
            PRIMARY KEY (blocker, blocked)
        )
        ''')
        await db.commit()


# Run DB initialization
asyncio.run(init_db())

# ========== KEYBOARD BUTTONS ==========
buttons = [
    [KeyboardButton("🏠 Home"), KeyboardButton("👤 Profile")],
    [KeyboardButton("🔍 Search"), KeyboardButton("❤️ Likes")],
    [KeyboardButton("💕 Matches"), KeyboardButton("⭐ Premium")],
    [KeyboardButton("🪙 Coins"), KeyboardButton("🎁 Gifts")],
    [KeyboardButton("🎁 My Gifts"), KeyboardButton("✏️ Edit Profile")],
    [KeyboardButton("📢 Complaint"), KeyboardButton("🚫 Delete Account")],
    [KeyboardButton("❓ Help"), KeyboardButton("🔔 Reminders")]
]
main_kb = ReplyKeyboardMarkup(buttons, resize_keyboard=True)

# ========== FUNNY MESSAGES ==========
funny_messages = [
    "😂 Did you know? 70% of Weye users find a match within 3 days!",
    "☕ Fun fact: Ethiopians invented coffee, and now we're inventing love!",
    "💘 Warning: Excessive swiping may cause butterflies in your stomach!",
    "🎁 Send a Buna gift – it's cheaper than buying real coffee!",
    "⭐ Premium users get 10x more matches! (Probably because they're rich? 😂)",
    "📢 Don't be shy! Your Weye is waiting for you!",
    "💕 Remember: Love is like Buna – best when shared!"
]

morning_reminders = [
    "🌅 Good morning! Time to find your Weye! ☕",
    "☀️ Rise and shine! Your perfect match might be waiting. /swipe now!",
    "🥐 Breakfast is important, but love is more important. Start swiping!",
    "🐦 Early bird gets the worm – and maybe a date! Good morning!",
    "🇪🇹 Buna and love – the best way to start the day! /search"
]

evening_reminders = [
    "🌙 Good evening! Perfect time to unwind and swipe for love. 💕",
    "✨ The stars are out – maybe your soulmate is too. Check /search!",
    "🍵 Evening Buna and a new match? Why not!",
    "🎬 Finished your series? Now find your Weye! /swipe",
    "😴 Don't go to bed alone – your match is just a swipe away!"
]


def get_funny_message():
    return random.choice(funny_messages)


def get_morning_reminder():
    return random.choice(morning_reminders)


def get_evening_reminder():
    return random.choice(evening_reminders)


def sanitize_text(text):
    if not text:
        return text
    return re.sub(r'[_*`\[\]()~>#+\-=|{}.!]', '', str(text))


def get_display_name(user):
    if user.username:
        return user.username
    return user.first_name or "Someone"


def is_admin(user_id):
    return user_id in ADMIN_IDS


def parse_datetime(date_str):
    if not date_str:
        return datetime.now()
    try:
        return datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return datetime.fromisoformat(date_str)
        except ValueError:
            return datetime.now()


# ========== DATABASE HELPERS ==========
async def get_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                await db.execute("UPDATE users SET last_activity = CURRENT_TIMESTAMP WHERE user_id=?", (user_id,))
                await db.commit()
                return row
    return None


async def update_user(user_id, **kwargs):
    async with aiosqlite.connect(DB_PATH) as db:
        for key, value in kwargs.items():
            await db.execute(f"UPDATE users SET {key} = ? WHERE user_id = ?", (value, user_id))
        await db.commit()


async def is_premium(user_id):
    user = await get_user(user_id)
    if user and user['premium_expiry']:
        expiry = parse_datetime(user['premium_expiry'])
        return expiry > datetime.now()
    return False


async def get_premium_days_left(user_id):
    user = await get_user(user_id)
    if user and user['premium_expiry']:
        expiry = parse_datetime(user['premium_expiry'])
        days = (expiry - datetime.now()).days
        return max(0, days)
    return 0


async def get_likes_count(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM swipes WHERE to_user=? AND action='like'", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def get_matches_count(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM matches WHERE user1=? OR user2=?", (user_id, user_id)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def deduct_coins(user_id, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row and row[0] >= amount:
                await db.execute("UPDATE users SET balance = balance - ? WHERE user_id=?", (amount, user_id))
                await db.commit()
                async with db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)) as cursor2:
                    new_row = await cursor2.fetchone()
                    return True, new_row[0] if new_row else 0
            return False, row[0] if row else 0


async def is_blocked(blocker, blocked):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM blocks WHERE blocker=? AND blocked=?", (blocker, blocked)) as cursor:
            return await cursor.fetchone() is not None


async def block_user(blocker, blocked):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO blocks (blocker, blocked) VALUES (?,?)", (blocker, blocked))
        await db.commit()


async def get_or_create_chat(user1, user2):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM private_chats WHERE (user1=? AND user2=?) OR (user1=? AND user2=?)",
                              (user1, user2, user2, user1)) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0]
            else:
                await db.execute("INSERT INTO private_chats (user1, user2) VALUES (?,?)", (user1, user2))
                await db.commit()
                return cursor.lastrowid


async def save_message(chat_id, sender_id, message):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO messages (chat_id, sender_id, message) VALUES (?,?,?)",
                         (chat_id, sender_id, message))
        await db.execute("UPDATE private_chats SET last_message=?, last_timestamp=CURRENT_TIMESTAMP WHERE id=?",
                         (message[:100], chat_id))
        await db.commit()


async def get_current_chat(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT current_chat_target FROM users WHERE user_id=?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def update_current_chat(user_id, target_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET current_chat_target = ? WHERE user_id=?", (target_id, user_id))
        await db.commit()


async def update_both_chats(user1, user2):
    await update_current_chat(user1, user2)
    await update_current_chat(user2, user1)


async def clear_both_chats(user1, user2):
    await update_current_chat(user1, 0)
    await update_current_chat(user2, 0)


async def is_matched(user1, user2):
    u1, u2 = sorted([user1, user2])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM matches WHERE user1=? AND user2=?", (u1, u2)) as cursor:
            return await cursor.fetchone() is not None


async def is_chat_timeout(user_id, timeout_minutes=15):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT last_activity FROM users WHERE user_id=?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                last = parse_datetime(row[0])
                return (datetime.now() - last).total_seconds() > timeout_minutes * 60
            return True


async def get_next_profile(user_id, user_gender, looking_for):
    if looking_for == "opposite":
        target_gender = "Female" if user_gender == "Male" else "Male"
    elif looking_for == "same":
        target_gender = user_gender
    else:
        target_gender = None

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if target_gender:
            async with db.execute('''
                SELECT user_id, name, age, bio, photo FROM users
                WHERE gender = ? 
                AND user_id != ?
                AND user_id NOT IN (SELECT to_user FROM swipes WHERE from_user=?)
                AND user_id NOT IN (SELECT blocked FROM blocks WHERE blocker=?)
                AND user_id NOT IN (SELECT blocker FROM blocks WHERE blocked=?)
                ORDER BY last_activity DESC
                LIMIT 50
            ''', (target_gender, user_id, user_id, user_id, user_id)) as cursor:
                candidates = await cursor.fetchall()
        else:
            async with db.execute('''
                SELECT user_id, name, age, bio, photo FROM users
                WHERE user_id != ?
                AND user_id NOT IN (SELECT to_user FROM swipes WHERE from_user=?)
                AND user_id NOT IN (SELECT blocked FROM blocks WHERE blocker=?)
                AND user_id NOT IN (SELECT blocker FROM blocks WHERE blocked=?)
                ORDER BY last_activity DESC
                LIMIT 50
            ''', (user_id, user_id, user_id, user_id)) as cursor:
                candidates = await cursor.fetchall()

    if candidates:
        return random.choice(candidates)
    return None


# ========== COMMAND HANDLERS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = await get_user(user_id)
    if not user:
        welcome_text = (
            "💘 WELCOME TO WEYE DATING! 💘\n\n"
            "Ever wanted to find love while drinking Buna? ☕\n"
            "Ever dreamed of a soulmate who loves Doro Wat as much as you? 🍗\n\n"
            "Why Weye Dating?\n"
            "✅ Ethiopian made, for Ethiopians 🇪🇹\n"
            "✅ Find genuine connections\n"
            "✅ Send virtual gifts (Buna, Flower, Heart, Doro Wat...)\n"
            "✅ Meet people who share your vibe\n\n"
            f"😂 Funny fact: {get_funny_message()}\n\n"
            "Let's find your Weye (ወዬ)! 💕\n\n"
            "What's your name?"
        )
        await update.message.reply_text(welcome_text)
        context.user_data['step'] = 'name'
    else:
        balance = user['balance']
        likes = await get_likes_count(user_id)
        matches = await get_matches_count(user_id)
        premium_days = await get_premium_days_left(user_id)

        pin_text = (
            f"🪙 {balance} coins | ❤️ {likes} likes | 💕 {matches} matches | ⭐ {premium_days} days premium\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🎉 Welcome back, {sanitize_text(user['name'])}!\n\n"
            f"😂 {get_funny_message()}\n\n"
            f"Ready to find your Weye? Use the buttons below! 💕"
        )
        await update.message.reply_text(pin_text, reply_markup=main_kb)


async def handle_registration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get('step')
    text = update.message.text
    user_id = update.effective_user.id

    if step == 'name':
        context.user_data['name'] = text
        await update.message.reply_text(f"Nice name, {sanitize_text(text)}! ✨\n\nHow old are you? (Send a number)")
        context.user_data['step'] = 'age'
    elif step == 'age':
        if not text.isdigit():
            await update.message.reply_text("Please send a number for your age 🧐")
            return
        context.user_data['age'] = int(text)
        gender_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("♂️ Male", callback_data="reg_gender_male")],
            [InlineKeyboardButton("♀️ Female", callback_data="reg_gender_female")]
        ])
        await update.message.reply_text("Select your gender:", reply_markup=gender_kb)
        context.user_data['step'] = 'gender'
    elif step == 'bio':
        context.user_data['bio'] = text
        await update.message.reply_text(
            "📸 Now send a photo of yourself!\n\nThis will be visible to other users.\n\nSend a clear photo so people can see your beautiful face! 📸")
        context.user_data['step'] = 'photo'


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get('step')
    user_id = update.effective_user.id

    if context.user_data.get('step') != 'photo' and context.user_data.get('editing') != 'photo':
        return

    if context.user_data.get("editing") == "photo":
        if not update.message.photo:
            await update.message.reply_text("❌ Please send a photo.")
            return
        photo_id = update.message.photo[-1].file_id
        await update_user(user_id, photo=photo_id)
        await update.message.reply_text("✅ Profile photo updated!", reply_markup=main_kb)
        context.user_data.pop("editing", None)
        return

    if step == 'photo':
        if 'name' not in context.user_data:
            await update.message.reply_text("Please restart registration with /start")
            return

        if not update.message.photo:
            await update.message.reply_text("📸 Please send a photo!")
            return

        photo_id = update.message.photo[-1].file_id
        gender = context.user_data.get('gender', 'Unknown')

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute('''
                INSERT INTO users (user_id, name, age, gender, bio, photo, balance, reminder)
                VALUES (?,?,?,?,?,?,?,?)
            ''', (user_id, context.user_data['name'], context.user_data['age'],
                  gender, context.user_data['bio'], photo_id, 0, 1))
            await db.commit()

        context.user_data.clear()

        await update.message.reply_text(
            "✅ 🎉 PROFILE CREATED SUCCESSFULLY! 🎉\n\n"
            "Now you can:\n"
            "🔍 Search for matches\n"
            "🎁 Send virtual gifts to impress\n"
            "⭐ Upgrade to Premium for more features\n"
            "✏️ Edit Profile anytime to update your info\n"
            "💕 Find your Weye (ወዬ)!\n\n"
            f"😂 {get_funny_message()}\n\n"
            "Use the buttons below to start your journey! 👇",
            reply_markup=main_kb
        )


async def registration_gender_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    gender = "Male" if query.data == "reg_gender_male" else "Female"
    context.user_data['gender'] = gender
    await query.edit_message_text(
        f"✅ Gender selected: {gender}\n\n📝 Now write a short bio about yourself (tell people who you are!):")
    context.user_data['step'] = 'bio'


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = await get_user(user_id)
    if not user:
        await update.message.reply_text("Please use /start to register first.")
        return
    premium = "✅ Active" if await is_premium(user_id) else "❌ Not active"

    if user['premium_expiry']:
        expiry = parse_datetime(user['premium_expiry'])
        if expiry > datetime.now():
            days_left = (expiry - datetime.now()).days
            premium = f"✅ Active ({days_left} days left)"

    text = (
        f"👤 {sanitize_text(user['name'])}\n"
        f"📅 Age: {user['age']}\n"
        f"⚥ Gender: {user['gender']}\n"
        f"📝 Bio: {sanitize_text(user['bio'])}\n"
        f"🪙 Coins: {user['balance']}\n"
        f"⭐ Premium: {premium}\n\n"
        f"💡 Tip: Use ✏️ Edit Profile to update your info!"
    )
    if user['photo']:
        await update.message.reply_photo(photo=user['photo'], caption=text, reply_markup=main_kb)
    else:
        await update.message.reply_text(text, reply_markup=main_kb)


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_cooldown(update.effective_user.id, "search", 2):
        await update.message.reply_text("⏳ Please wait a moment before searching again.")
        return

    user_id = update.effective_user.id
    user = await get_user(user_id)
    if not user:
        await update.message.reply_text("Please use /start to register first.")
        return

    user_gender = user['gender']
    looking_for = user['looking_for'] or "opposite"

    target = await get_next_profile(user_id, user_gender, looking_for)

    if not target:
        await update.message.reply_text(f"No more profiles to show. Come back later! 🌟\n\n😂 {get_funny_message()}",
                                        reply_markup=main_kb)
        return

    target_id = target['user_id']
    name = target['name']
    age = target['age']
    bio = target['bio']
    photo = target['photo']

    context.user_data['current_profile'] = target_id
    context.user_data['current_profile_name'] = name

    text = f"💖 {sanitize_text(name)}, {age}\n📝 {sanitize_text(bio)}"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👍 Like", callback_data=f"like_{target_id}"),
            InlineKeyboardButton("👎 Pass", callback_data=f"pass_{target_id}")
        ],
        [
            InlineKeyboardButton("💬 Message", callback_data=f"action_message_{target_id}"),
            InlineKeyboardButton("⭐ Super Like", callback_data=f"action_superlike_{target_id}")
        ],
        [
            InlineKeyboardButton("🎁 Send Gift", callback_data=f"action_gift_{target_id}"),
            InlineKeyboardButton("🚫 Block", callback_data=f"action_block_{target_id}"),
            InlineKeyboardButton("📢 Report", callback_data=f"action_report_{target_id}")
        ],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]
    ])

    if photo:
        await update.message.reply_photo(photo=photo, caption=text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, reply_markup=keyboard)


async def swipe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data.startswith("like_"):
        target_id = int(data.split("_")[1])
        action = "like"
    elif data.startswith("pass_"):
        target_id = int(data.split("_")[1])
        action = "pass"
    else:
        return

    if action == "like":
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO swipes (from_user, to_user, action) VALUES (?,?,?)",
                             (user_id, target_id, 'like'))
            await db.commit()
            u1, u2 = sorted([user_id, target_id])
            try:
                await db.execute("INSERT INTO matches (user1, user2) VALUES (?,?)", (u1, u2))
                await db.commit()
                await query.edit_message_text(
                    "🎉 IT'S A MATCH! 🎉\n\nYou can now chat via the Search button.\nUse 🔍 Search and press 💬 Message to start chatting.")
                other_user = await get_user(target_id)
                if other_user:
                    await context.bot.send_message(
                        chat_id=target_id,
                        text=f"🎉 MATCH! You matched with {sanitize_text(query.from_user.first_name)}!\nUse 🔍 Search and press 💬 Message to start chatting.",
                    )
            except Exception:
                pass
        await query.edit_message_text("👍 Liked! If they like you back, you'll match.")
    else:
        await query.edit_message_text("👎 Passed. Next profile might be your Weye!")
    context.user_data.pop('current_swipe', None)


async def action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    user = await get_user(user_id)

    if not user:
        await query.edit_message_text("Please use /start to register first.")
        return

    if data == "back_to_menu":
        await context.bot.send_message(
            chat_id=user_id,
            text="🔙 Returned to main menu.",
            reply_markup=main_kb
        )
        return

    if data.startswith("action_message_"):
        target_id = int(data.split('_')[2])
        target_user = await get_user(target_id)
        if not target_user:
            await query.edit_message_text("User not found.")
            return
        if not await is_matched(user_id, target_id):
            await query.edit_message_text(
                f"❌ You can only message users you have matched with first!\n\n"
                f"Swipe right on them and wait for them to swipe right on you!\n\n"
                f"😂 {get_funny_message()}"
            )
            return
        if await is_chat_timeout(user_id) or await is_chat_timeout(target_id):
            await clear_both_chats(user_id, target_id)
            await query.edit_message_text(
                f"⏳ Chat session expired due to inactivity. Please start a new chat from Search.\n\n😂 {get_funny_message()}"
            )
            return
        chat_id = await get_or_create_chat(user_id, target_id)
        await update_both_chats(user_id, target_id)
        await query.edit_message_text(
            f"💬 Chat with {sanitize_text(target_user['name'])} 💬\n\n"
            f"You can now send messages! Just type below.\n"
            f"Type /cancel to stop chatting.\n"
            f"Chat expires after 15 minutes of inactivity.\n\n"
            f"😂 {get_funny_message()}"
        )

    elif data.startswith("action_superlike_"):
        if not check_cooldown(user_id, "superlike", 10):
            await query.edit_message_text("⏳ Please wait a moment before sending another Super Like.")
            return
        target_id = int(data.split('_')[2])
        cost = 20 if not await is_premium(user_id) else 10
        if user['balance'] < cost:
            await query.edit_message_text(
                f"❌ Not enough coins!\n\nSuper Like costs {cost} coins.\nYour balance: {user['balance']} coins\n\n"
                f"Buy coins using 🪙 Coins button!\n\n😂 {get_funny_message()}"
            )
            return
        success, new_balance = await deduct_coins(user_id, cost)
        if success:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("INSERT INTO swipes (from_user, to_user, action) VALUES (?,?,?)",
                                 (user_id, target_id, 'superlike'))
                await db.commit()
            target_user = await get_user(target_id)
            if target_user:
                await context.bot.send_message(
                    chat_id=target_id,
                    text=f"⭐ SUPER LIKE! ⭐\n\n{get_display_name(update.effective_user)} sent you a Super Like!\n\n😂 {get_funny_message()}"
                )
            await query.edit_message_text(
                f"⭐ Super Like sent! ⭐\n\nYou spent {cost} coins.\nNew balance: {new_balance} coins\n\n😂 {get_funny_message()}"
            )
        else:
            await query.edit_message_text(
                f"❌ Failed to deduct coins. Please try again.\n\n😂 {get_funny_message()}"
            )

    elif data.startswith("action_gift_"):
        target_id = int(data.split('_')[2])
        context.user_data['gift_target'] = target_id
        context.user_data['gift_target_name'] = context.user_data.get('current_profile_name', 'this user')
        keyboard = InlineKeyboardMarkup([
                                            [InlineKeyboardButton(f"{g['name']} - {g['cost']} coins",
                                                                  callback_data=f"gift_{k}")]
                                            for k, g in GIFTS.items()
                                        ] + [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]])
        await query.edit_message_text(
            f"🎁 Send a gift to {sanitize_text(context.user_data['gift_target_name'])} 🎁\n\nChoose a gift:\n\n😂 {get_funny_message()}",
            reply_markup=keyboard
        )

    elif data.startswith("action_block_"):
        target_id = int(data.split('_')[2])
        await block_user(user_id, target_id)
        target_name = context.user_data.get('current_profile_name', 'Unknown')
        await query.edit_message_text(
            f"🚫 User blocked 🚫\n\nYou have blocked @{sanitize_text(target_name)}. They will no longer appear in your searches.\n\n😂 {get_funny_message()}"
        )
        await context.bot.send_message(
            chat_id=user_id,
            text="Return to main menu with /start",
            reply_markup=main_kb
        )

    elif data.startswith("action_report_"):
        target_id = int(data.split('_')[2])
        context.user_data['report_target'] = target_id
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚫 Fake User", callback_data="report_fake")],
            [InlineKeyboardButton("💢 Harassment", callback_data="report_harass")],
            [InlineKeyboardButton("📧 Spam", callback_data="report_spam")],
            [InlineKeyboardButton("❓ Other", callback_data="report_other")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]
        ])
        await query.edit_message_text(
            f"📢 Report User\n\nWhat is the issue with this user?\n\n😂 {get_funny_message()}",
            reply_markup=keyboard
        )

    elif data.startswith("report_"):
        report_type = data.split('_')[1]
        target_id = context.user_data.get('report_target')
        target_id_val = target_id if target_id else 0
        target_name = context.user_data.get('current_profile_name', 'Unknown')
        categories = {"fake": "Fake User / Impersonation", "harass": "Harassment / Bullying",
                      "spam": "Spam / Advertising", "other": "Other"}
        category = categories.get(report_type, "Unknown")
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"📢 NEW REPORT! 📢\n\nReporter: {get_display_name(update.effective_user)} (ID: {user_id})\nReported User ID: {target_id_val}\nReason: {category}\n\n😂 {get_funny_message()}"
        )
        await query.edit_message_text(
            f"✅ Report sent! ✅\n\nThank you for reporting. Admin will review within 24 hours.\n\n😂 {get_funny_message()}"
        )
        await context.bot.send_message(
            chat_id=user_id,
            text="Return to main menu with /start",
            reply_markup=main_kb
        )
        context.user_data.pop('report_target', None)


async def gift_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    gift_key = query.data.split('_')[1]
    gift = GIFTS[gift_key]

    user_id = query.from_user.id
    user = await get_user(user_id)
    if not user:
        await query.edit_message_text("Please use /start to register first.")
        return

    target_id = context.user_data.get('gift_target')
    target_name = context.user_data.get('gift_target_name', 'this user')
    if not target_id:
        await query.edit_message_text(f"Please use 🔍 Search first to find someone to gift!\n\n😂 {get_funny_message()}")
        await context.bot.send_message(
            chat_id=user_id,
            text="Return to main menu with /start",
            reply_markup=main_kb
        )
        return

    cost = gift['cost']
    if await is_premium(user_id):
        cost = int(cost * 0.8)
    if user['balance'] < cost:
        await query.edit_message_text(
            f"❌ Not enough coins!\n\n{gift['name']} costs {cost} coins.\nYour balance: {user['balance']} coins\n\nBuy coins using 🪙 Coins button!\n\n😂 {get_funny_message()}"
        )
        await context.bot.send_message(
            chat_id=user_id,
            text="Return to main menu with /start",
            reply_markup=main_kb
        )
        context.user_data.pop('gift_target', None)
        return

    success, new_balance = await deduct_coins(user_id, cost)
    if success:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO gifts_received (from_user, to_user, gift_type, coins_cost) VALUES (?,?,?,?)",
                             (user_id, target_id, gift_key, cost))
            await db.commit()
        target_user = await get_user(target_id)
        if target_user:
            await context.bot.send_message(
                chat_id=target_id,
                text=f"🎁 GIFT RECEIVED! 🎁\n\n{get_display_name(update.effective_user)} sent you {gift['name']}!\n📖 Meaning: {gift['meaning']}\n\n😂 {get_funny_message()}"
            )
        await query.edit_message_text(
            f"🎁 Gift sent to {sanitize_text(target_name)}! 🎁\n\nYou sent {gift['name']}!\n📖 Meaning: {gift['meaning']}\nCost: {cost} coins\nNew balance: {new_balance} coins\n\n😂 {get_funny_message()}"
        )
    else:
        await query.edit_message_text(
            f"❌ Failed to deduct coins. Please try again.\n\n😂 {get_funny_message()}"
        )
    await context.bot.send_message(
        chat_id=user_id,
        text="Return to main menu with /start",
        reply_markup=main_kb
    )
    context.user_data.pop('gift_target', None)
    context.user_data.pop('gift_target_name', None)


async def handle_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_id = await get_current_chat(user_id)

    if target_id == 0:
        return

    if not check_repeated_message(user_id, update.message.text):
        await update.message.reply_text("❌ Please don't send the same message repeatedly.", reply_markup=main_kb)
        return

    if not await is_matched(user_id, target_id):
        await clear_both_chats(user_id, target_id)
        await update.message.reply_text("❌ Chat ended. You are no longer matched with this user.", reply_markup=main_kb)
        return

    if await is_chat_timeout(user_id) or await is_chat_timeout(target_id):
        await clear_both_chats(user_id, target_id)
        await update.message.reply_text("⏳ Chat session expired due to inactivity. Use Search to start a new chat.",
                                        reply_markup=main_kb)
        return

    target_user = await get_user(target_id)
    if not target_user:
        await clear_both_chats(user_id, target_id)
        await update.message.reply_text("❌ Chat ended. User not found.", reply_markup=main_kb)
        return

    message = update.message.text

    if len(message) < 1:
        return
    if URL_PATTERN.search(message):
        await update.message.reply_text("❌ Links are not allowed in chat for safety.")
        return
    if len(message) > 500:
        await update.message.reply_text("❌ Message too long! Max 500 characters.")
        return

    chat_id = await get_or_create_chat(user_id, target_id)
    await save_message(chat_id, user_id, message)

    await context.bot.send_chat_action(chat_id=target_id, action="typing")
    await context.bot.send_message(
        chat_id=target_id,
        text=f"💬 New message from {sanitize_text(update.effective_user.first_name)} 💬\n\n\"{message}\"\n\nType /cancel to exit chat anytime.\n\n😂 {get_funny_message()}"
    )
    await update.message.reply_text(
        f"✅ Message sent to {sanitize_text(target_user['name'])}! ✅\n\nType /cancel to exit chat anytime.\n\n😂 {get_funny_message()}",
        reply_markup=main_kb
    )


async def my_gifts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('''
            SELECT u.name, g.gift_type, g.timestamp FROM gifts_received g
            JOIN users u ON g.from_user = u.user_id
            WHERE g.to_user = ?
            ORDER BY g.timestamp DESC
        ''', (user_id,)) as cursor:
            gifts_list = await cursor.fetchall()
    if not gifts_list:
        await update.message.reply_text(
            f"🎁 No gifts received yet 🎁\n\nSend gifts to others and they might send back!\n\n😂 {get_funny_message()}",
            reply_markup=main_kb)
        return
    msg = "🎁 Gifts You've Received 🎁\n\n"
    for gift in gifts_list:
        gift_name = GIFTS.get(gift['gift_type'], {}).get('name', gift['gift_type'])
        date = gift['timestamp'][:10] if gift['timestamp'] else "unknown"
        msg += f"✨ {gift_name} from {sanitize_text(gift['name'])} on {date}\n"
    msg += f"\n😂 {get_funny_message()}"
    await update.message.reply_text(msg, reply_markup=main_kb)


async def edit_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Edit Bio", callback_data="edit_bio")],
        [InlineKeyboardButton("📅 Edit Age", callback_data="edit_age")],
        [InlineKeyboardButton("⚥ Edit Gender", callback_data="edit_gender")],
        [InlineKeyboardButton("📸 Change Photo", callback_data="edit_photo")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]
    ])
    await update.message.reply_text(
        "✏️ Edit Profile\n\nChoose what you want to update:",
        reply_markup=keyboard
    )


async def edit_profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data
    if action == "back_to_menu":
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="🔙 Returned to main menu.",
            reply_markup=main_kb
        )
        return
    if action == "edit_bio":
        await query.edit_message_text("📝 Send your new bio (max 200 characters):")
        context.user_data['editing'] = 'bio'
    elif action == "edit_age":
        await query.edit_message_text("📅 Send your new age (number):")
        context.user_data['editing'] = 'age'
    elif action == "edit_gender":
        gender_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("♂️ Male", callback_data="edit_gender_male")],
            [InlineKeyboardButton("♀️ Female", callback_data="edit_gender_female")]
        ])
        await query.edit_message_text("⚥ Select your gender:", reply_markup=gender_kb)
    elif action == "edit_photo":
        await query.edit_message_text("📸 Send a new photo of yourself:")
        context.user_data['editing'] = 'photo'


async def edit_gender_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    gender = "Male" if query.data == "edit_gender_male" else "Female"
    user_id = query.from_user.id
    await update_user(user_id, gender=gender)
    await query.edit_message_text(f"✅ Gender updated to {gender}!\n\nUse /start to return to main menu.")


async def handle_edit_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    editing = context.user_data.get('editing')
    if not editing:
        return
    user_id = update.effective_user.id

    if editing == 'bio':
        text = update.message.text
        if len(text) > 200:
            await update.message.reply_text("❌ Bio too long! Max 200 characters.")
            return
        await update_user(user_id, bio=text)
        await update.message.reply_text(f"✅ Bio updated!\n\nNew bio: {text}", reply_markup=main_kb)
        context.user_data.pop('editing', None)
    elif editing == 'age':
        if not update.message.text.isdigit():
            await update.message.reply_text("❌ Please send a number.")
            return
        age = int(update.message.text)
        await update_user(user_id, age=age)
        await update.message.reply_text(f"✅ Age updated to {age}!", reply_markup=main_kb)
        context.user_data.pop('editing', None)
    elif editing == 'photo':
        pass


async def likes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_premium(user_id):
        await update.message.reply_text(
            f"🔒 Premium Feature 🔒\n\nUpgrade to Premium to see who liked you!\n\nClick ⭐ Premium to see plans.\n\n😂 {get_funny_message()}",
            reply_markup=main_kb
        )
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('''
            SELECT u.name, u.age FROM swipes s
            JOIN users u ON s.from_user = u.user_id
            WHERE s.to_user = ? AND s.action = 'like'
        ''', (user_id,)) as cursor:
            likes_list = await cursor.fetchall()
    if not likes_list:
        await update.message.reply_text(
            f"💔 No one liked you yet. Send gifts to get noticed! 🎁\n\n😂 {get_funny_message()}", reply_markup=main_kb)
    else:
        msg = "❤️ People who liked you: ❤️\n\n"
        for like in likes_list:
            msg += f"✨ {sanitize_text(like['name'])}, {like['age']}\n"
        msg += f"\n😂 {get_funny_message()}"
        await update.message.reply_text(msg, reply_markup=main_kb)


async def matches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('''
            SELECT u.name FROM matches m
            JOIN users u ON (u.user_id = m.user2) WHERE m.user1 = ?
            UNION
            SELECT u.name FROM matches m
            JOIN users u ON (u.user_id = m.user1) WHERE m.user2 = ?
        ''', (user_id, user_id)) as cursor:
            matches_list = await cursor.fetchall()
    if not matches_list:
        await update.message.reply_text(f"💕 No matches yet. Keep swiping to find your Weye!\n\n😂 {get_funny_message()}",
                                        reply_markup=main_kb)
    else:
        msg = "💕 Your Matches: 💕\n\n"
        for match in matches_list:
            msg += f"✨ {sanitize_text(match['name'])}\n"
        msg += f"\n😂 {get_funny_message()}"
        await update.message.reply_text(msg, reply_markup=main_kb)


async def coins_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 100 coins - 50 ETB", callback_data="buy_coins_100_50")],
        [InlineKeyboardButton("💰 500 coins - 300 ETB", callback_data="buy_coins_500_300")],
        [InlineKeyboardButton("💰 1000 coins - 600 ETB", callback_data="buy_coins_1000_600")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]
    ])
    await update.message.reply_text(
        f"🪙 Buy Coins 🪙\n\nChoose your package:\n\n😂 {get_funny_message()}",
        reply_markup=keyboard
    )


async def buy_coins_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "back_to_menu":
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="🔙 Returned to main menu.",
            reply_markup=main_kb
        )
        return
    if data.startswith("buy_coins_"):
        parts = data.split('_')
        coins = int(parts[2])
        amount = int(parts[3])
        user_id = query.from_user.id
        await query.edit_message_text(
            f"🪙 Coin Purchase Request 🪙\n\n📦 Package: {coins} coins\n💰 Amount: {amount} ETB\n\n"
            f"📞 Send payment to Telebirr: {TELEBIRR_MERCHANT_ID}\n\n"
            f"📱 After payment, send the transaction ID to the admin.\nAdmin will add coins.\n\n😂 {get_funny_message()}"
        )
        context.user_data['pending_coins'] = {'coins': coins, 'amount': amount}


async def premium_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📆 15 days - 150 ETB", callback_data="premium_15_150")],
        [InlineKeyboardButton("📆 1 month - 350 ETB", callback_data="premium_30_350")],
        [InlineKeyboardButton("📆 2 months - 700 ETB", callback_data="premium_60_700")],
        [InlineKeyboardButton("📆 6 months - 2500 ETB", callback_data="premium_180_2500")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]
    ])
    await update.message.reply_text(
        "⭐ Weye Premium ⭐\n\nBenefits:\n• Unlimited swipes 💕\n• See who liked you 👀\n• Send messages 💬\n• 50% off Super Likes ⭐\n• 500 monthly coins 🪙\n\nChoose your plan:",
        reply_markup=keyboard
    )


async def premium_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "back_to_menu":
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="🔙 Returned to main menu.",
            reply_markup=main_kb
        )
        return
    if data.startswith("premium_"):
        parts = data.split('_')
        days = int(parts[1])
        amount = int(parts[2])
        user_id = query.from_user.id
        await query.edit_message_text(
            f"⭐ Premium Request ⭐\n\n📆 Plan: {days} days\n💰 Amount: {amount} ETB\n\n"
            f"📞 Send payment to Telebirr: {TELEBIRR_MERCHANT_ID}\n\n"
            f"📱 After payment, send the transaction ID to the admin.\nAdmin will activate premium.\n\n😂 {get_funny_message()}"
        )
        context.user_data['pending_premium'] = {'days': days, 'amount': amount}


async def gifts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎁 Use 🔍 Search to send gifts!", reply_markup=main_kb)


async def reminder_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = await get_user(user_id)
    if not user:
        await update.message.reply_text("Please register first with /start")
        return
    current = user['reminder'] if user['reminder'] is not None else 1
    status = "🔔 ON" if current == 1 else "🔕 OFF"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 Turn ON", callback_data="reminder_on")],
        [InlineKeyboardButton("🔕 Turn OFF", callback_data="reminder_off")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]
    ])
    await update.message.reply_text(
        f"Reminder Settings\nCurrent: {status}\n\n"
        f"I'll send you funny reminders every morning and evening to help you find love.\n"
        f"😂 {get_funny_message()}",
        reply_markup=keyboard
    )


async def reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    if data == "back_to_menu":
        await context.bot.send_message(
            chat_id=user_id,
            text="🔙 Returned to main menu.",
            reply_markup=main_kb
        )
        return
    if data == "reminder_on":
        await update_user(user_id, reminder=1)
        await query.edit_message_text("✅ Reminders turned ON! You'll receive morning and evening reminders.")
    elif data == "reminder_off":
        await update_user(user_id, reminder=0)
        await query.edit_message_text("🔕 Reminders turned OFF. You can re-enable anytime from the menu.")


async def delete_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, delete my account", callback_data="confirm_delete")],
        [InlineKeyboardButton("❌ No, cancel", callback_data="cancel_delete")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]
    ])
    await update.message.reply_text(
        "⚠️ WARNING! ⚠️\n\n"
        "Are you sure you want to delete your account?\n\n"
        "This action is permanent and cannot be undone.\n\n"
        f"😂 {get_funny_message()}",
        reply_markup=keyboard
    )


async def delete_account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "back_to_menu":
        await context.bot.send_message(
            chat_id=user_id,
            text="🔙 Returned to main menu.",
            reply_markup=main_kb
        )
        return
    if data == "confirm_delete":
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM users WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM swipes WHERE from_user=? OR to_user=?", (user_id, user_id))
            await db.execute("DELETE FROM matches WHERE user1=? OR user2=?", (user_id, user_id))
            await db.execute("DELETE FROM gifts_received WHERE from_user=? OR to_user=?", (user_id, user_id))
            await db.execute("DELETE FROM private_chats WHERE user1=? OR user2=?", (user_id, user_id))
            await db.commit()
        await query.edit_message_text(
            f"✅ Account deleted successfully ✅\n\n"
            f"Your profile has been removed from Weye Dating.\n\n"
            f"You can always /start again to create a new profile.\n\n"
            f"😂 {get_funny_message()}"
        )
    elif data == "cancel_delete":
        await query.edit_message_text(
            f"✅ Account deletion cancelled ✅\n\n"
            f"We're glad you stayed! 💕\n\n"
            f"😂 {get_funny_message()}"
        )
        await context.bot.send_message(
            chat_id=user_id,
            text="Return to main menu with /start",
            reply_markup=main_kb
        )


async def help_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ Weye Dating Help ❓\n\n"
        "🏠 Home - Return to main menu\n"
        "👤 Profile - View your profile\n"
        "✏️ Edit Profile - Update your info\n"
        "🚫 Delete Account - Permanently delete your profile\n"
        "🔍 Search - Find matches (men see only women, women see only men)\n"
        "   • 👍 Like / 👎 Pass (free)\n"
        "   • 💬 Message (after match)\n"
        "   • ⭐ Super Like (costs coins)\n"
        "   • 🎁 Send Gift (costs coins)\n"
        "   • 🚫 Block user\n"
        "   • 📢 Report user\n"
        "❤️ Likes - See who liked you (Premium)\n"
        "💕 Matches - Your mutual matches\n"
        "⭐ Premium - Upgrade for more features\n"
        "🪙 Coins - Buy coins packages\n"
        "🎁 Gifts - Virtual Ethiopian gifts\n"
        "🎁 My Gifts - Gifts you received\n"
        "📢 Complaint - Report bugs or suggestions\n"
        "🔔 Reminders - Turn on/off daily funny reminders\n"
        "❌ /cancel - Cancel current operation\n\n"
        f"😂 {get_funny_message()}\n\nFind your Weye today! 💕",
        reply_markup=main_kb
    )


async def complaint_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🐛 Bug Report", callback_data="complaint_bug")],
        [InlineKeyboardButton("💡 Suggestion", callback_data="complaint_suggestion")],
        [InlineKeyboardButton("🚫 Fake User", callback_data="complaint_fake")],
        [InlineKeyboardButton("❓ Other", callback_data="complaint_other")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]
    ])
    await update.message.reply_text(
        f"📢 Choose complaint type 📢\n\nWhat would you like to report or suggest?\n\n😂 {get_funny_message()}",
        reply_markup=keyboard
    )


async def complaint_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "back_to_menu":
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="🔙 Returned to main menu.",
            reply_markup=main_kb
        )
        return
    complaint_type = data.split('_')[1]
    categories = {"bug": "Bug Report", "suggestion": "Suggestion", "fake": "Fake User Report", "other": "Other"}
    category = categories.get(complaint_type, "Unknown")
    await query.edit_message_text(
        f"📢 {category} 📢\n\nPlease describe your issue or suggestion in detail:\n\n😂 {get_funny_message()}"
    )
    context.user_data['complaint_type'] = complaint_type
    context.user_data['complaint_step'] = True


async def handle_complaint_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('complaint_step'):
        complaint_type = context.user_data.get('complaint_type', 'general')
        message = update.message.text
        categories = {"bug": "🐛 Bug Report", "suggestion": "💡 Suggestion", "fake": "🚫 Fake User Report",
                      "other": "❓ Other"}
        category = categories.get(complaint_type, "General")
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"📢 NEW {category.upper()}! 📢\n\nFrom: {get_display_name(update.effective_user)} (ID: {update.effective_user.id})\nType: {category}\nMessage: {message}\n\n😂 {get_funny_message()}"
        )
        await update.message.reply_text(
            f"✅ {category} sent! ✅\n\nThank you for your feedback! Admin will review it within 24 hours.\n\n😂 {get_funny_message()}",
            reply_markup=main_kb
        )
        context.user_data.pop('complaint_step', None)
        context.user_data.pop('complaint_type', None)


async def grant_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /grant_coins user_id amount")
        return
    try:
        user_id = int(context.args[0])
        amount = int(context.args[1])
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, user_id))
            await db.commit()
        await update.message.reply_text(f"✅ Added {amount} coins to user {user_id}.\n\n😂 {get_funny_message()}")
        logger.info(f"Admin {update.effective_user.id} granted {amount} coins to user {user_id}")
    except Exception as e:
        logger.error(f"Failed to grant coins: {e}")
        await update.message.reply_text("Invalid arguments.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user_id = update.effective_user.id
    target_id = await get_current_chat(user_id)
    if target_id:
        await clear_both_chats(user_id, target_id)
    await update.message.reply_text(
        "❌ Operation cancelled.",
        reply_markup=main_kb
    )


# ========== TEXT ROUTER ==========
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("step"):
        await handle_registration(update, context)
    elif context.user_data.get("editing"):
        await handle_edit_input(update, context)
    elif await get_current_chat(update.effective_user.id) != 0:
        await handle_chat_message(update, context)
    elif context.user_data.get("complaint_step"):
        await handle_complaint_input(update, context)


# ========== UNKNOWN COMMAND HANDLER ==========
async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❌ Unknown command or button.\nUse /start",
        reply_markup=main_kb
    )


# ========== ERROR HANDLER ==========
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception: {context.error}", exc_info=True)


# ========== REMINDER JOBS ==========
async def send_morning_reminders(context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users WHERE reminder=1") as cursor:
            users = await cursor.fetchall()
    for (uid,) in users:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=f"{get_morning_reminder()}\n\n{get_funny_message()}"
            )
        except Exception as e:
            logger.error(f"Failed to send morning reminder to {uid}: {e}")


async def send_evening_reminders(context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users WHERE reminder=1") as cursor:
            users = await cursor.fetchall()
    for (uid,) in users:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=f"{get_evening_reminder()}\n\n{get_funny_message()}"
            )
        except Exception as e:
            logger.error(f"Failed to send evening reminder to {uid}: {e}")


async def home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


# ========== MAIN ==========
def main():
    app = Application.builder().token(TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("grant_coins", grant_coins))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    # Message button handlers
    app.add_handler(MessageHandler(filters.Regex("^🏠 Home$"), home))
    app.add_handler(MessageHandler(filters.Regex("^👤 Profile$"), profile))
    app.add_handler(MessageHandler(filters.Regex("^🔍 Search$"), search))
    app.add_handler(MessageHandler(filters.Regex("^❤️ Likes$"), likes))
    app.add_handler(MessageHandler(filters.Regex("^💕 Matches$"), matches))
    app.add_handler(MessageHandler(filters.Regex("^⭐ Premium$"), premium_menu))
    app.add_handler(MessageHandler(filters.Regex("^🪙 Coins$"), coins_menu))
    app.add_handler(MessageHandler(filters.Regex("^🎁 Gifts$"), gifts_menu))
    app.add_handler(MessageHandler(filters.Regex("^🎁 My Gifts$"), my_gifts))
    app.add_handler(MessageHandler(filters.Regex("^📢 Complaint$"), complaint_menu))
    app.add_handler(MessageHandler(filters.Regex("^✏️ Edit Profile$"), edit_profile))
    app.add_handler(MessageHandler(filters.Regex("^🚫 Delete Account$"), delete_account))
    app.add_handler(MessageHandler(filters.Regex("^❓ Help$"), help_button))
    app.add_handler(MessageHandler(filters.Regex("^🔔 Reminders$"), reminder_settings))

    # Text router (excludes button texts)
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & ~filters.Regex(button_patterns),
            text_router
        )
    )
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Callback handlers
    app.add_handler(CallbackQueryHandler(registration_gender_callback, pattern="^reg_gender_"))
    app.add_handler(CallbackQueryHandler(edit_gender_callback, pattern="^edit_gender_"))
    app.add_handler(CallbackQueryHandler(swipe_callback, pattern="^(like_|pass_)"))
    app.add_handler(CallbackQueryHandler(gift_callback, pattern="^gift_"))
    app.add_handler(CallbackQueryHandler(complaint_callback, pattern="^complaint_"))
    app.add_handler(CallbackQueryHandler(edit_profile_callback, pattern="^(edit_bio|edit_age|edit_gender|edit_photo)$"))
    app.add_handler(CallbackQueryHandler(premium_callback, pattern="^premium_"))
    app.add_handler(CallbackQueryHandler(buy_coins_callback, pattern="^buy_coins_"))
    app.add_handler(CallbackQueryHandler(delete_account_callback, pattern="^(confirm_delete|cancel_delete)$"))
    app.add_handler(CallbackQueryHandler(reminder_callback, pattern="^reminder_"))
    app.add_handler(CallbackQueryHandler(action_callback, pattern="^(action_|report_|back_to_menu)"))

    # Daily reminder jobs
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_daily(
            send_morning_reminders,
            time=dt_time(hour=9, minute=0),
            days=tuple(range(7))
        )
        job_queue.run_daily(
            send_evening_reminders,
            time=dt_time(hour=20, minute=0),
            days=tuple(range(7))
        )
    else:
        logger.warning(
            "JobQueue not available. Reminders disabled. Install with: pip install 'python-telegram-bot[job-queue]'")

    # Error handler
    app.add_error_handler(error_handler)

    logger.info("✅ Weye Dating Bot started successfully!")
    app.run_polling()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"Fatal error: {e}")