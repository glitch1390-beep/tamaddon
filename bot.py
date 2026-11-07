import os
import time
import random
import sqlite3
import logging
from datetime import date
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# توکن را فقط در Environment Variables هاست با نام TELEGRAM_BOT_TOKEN قرار بده.
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "tamaddon.db")

COLLECT_INTERVAL = 30 * 60       # هر نیم ساعت
REMINDER_INTERVAL = 24 * 60 * 60 # یادآوری ۲۴ ساعته
MAX_BUILDING_LEVEL = 20
PRICE_DISCOUNT = 0.95            # تخفیف ۵ درصدی تجهیزات
STARTING_COINS = 1500

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("tamaddon")

# ۱۲ ساختمان مستقل؛ درآمد هر سطح در هر بازه ۳۰ دقیقه‌ای
BUILDINGS = {
    "🏛 ساختمان مرکزی": {"price": 500, "income": 0},
    "🌾 مزرعه": {"price": 180, "income": 18},
    "⛏ معدن آهن": {"price": 240, "income": 23},
    "🪵 چوب‌بری": {"price": 210, "income": 20},
    "🪨 معدن سنگ": {"price": 220, "income": 21},
    "🏭 کارخانه": {"price": 420, "income": 38},
    "🏦 بانک": {"price": 380, "income": 0},
    "🛡 پادگان": {"price": 320, "income": 0},
    "✈️ فرودگاه": {"price": 460, "income": 0},
    "⚓ بندر": {"price": 440, "income": 0},
    "🔬 آزمایشگاه": {"price": 520, "income": 0},
    "🛰 مرکز فرماندهی": {"price": 620, "income": 0},
}

# دسته‌بندی تجهیزات: نام -> (قیمت پایه، قدرت)
UNITS = {
    "زمینی": {
        "پیاده‌نظام": (80, 5), "تانک سبک": (260, 12),
        "تانک سنگین": (520, 23), "توپخانه": (390, 17),
    },
    "هوایی": {
        "جنگنده": (700, 30), "بمب‌افکن": (900, 38),
        "بالگرد": (480, 19),
    },
    "دریایی": {
        "قایق گشتی": (350, 14), "ناوشکن": (850, 34),
        "زیردریایی": (950, 39),
    },
    "پدافند": {
        "ضدهوایی": (420, 19), "سامانه موشکی": (760, 31),
    },
    "موشکی": {
        "موشک کوتاه‌برد": (500, 24), "موشک دوربرد": (1100, 50),
    },
}

MENU = ReplyKeyboardMarkup([
    ["🏙 شهر من", "🏗 ساختمان‌ها"],
    ["💰 جمع‌آوری درآمد", "🛒 فروشگاه نظامی"],
    ["⚔️ ارتش من", "🎯 مأموریت روزانه"],
    ["🎲 حمله تصادفی", "🤝 حمله به بازیکن"],
    ["🏆 رتبه‌بندی", "ℹ️ راهنما"],
], resize_keyboard=True)

def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS players(
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT 'فرمانده',
            coins INTEGER NOT NULL DEFAULT 1500,
            points INTEGER NOT NULL DEFAULT 0,
            last_collect REAL NOT NULL,
            last_reminder REAL NOT NULL DEFAULT 0,
            created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS buildings(
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            level INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id,name)
        );
        CREATE TABLE IF NOT EXISTS army(
            user_id INTEGER NOT NULL,
            unit TEXT NOT NULL,
            amount INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id,unit)
        );
        CREATE TABLE IF NOT EXISTS missions(
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id,day)
        );
        CREATE TABLE IF NOT EXISTS battles(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attacker INTEGER NOT NULL,
            defender INTEGER NOT NULL,
            winner INTEGER NOT NULL,
            created REAL NOT NULL,
            mode TEXT NOT NULL
        );
        """)
        existing = con.execute("SELECT user_id FROM players").fetchall()
        for p in existing:
            for name in BUILDINGS:
                con.execute(
                    "INSERT OR IGNORE INTO buildings(user_id,name,level) VALUES(?,?,0)",
                    (p["user_id"], name)
                )

def register(user):
    now = time.time()
    with db() as con:
        con.execute("""
            INSERT OR IGNORE INTO players
            (user_id,username,name,coins,points,last_collect,last_reminder,created)
            VALUES(?,?,?,?,?,?,?,?)
        """, (user.id, user.username or "", user.first_name or "فرمانده",
              STARTING_COINS, 0, now, now, now))
        con.execute(
            "UPDATE players SET username=?,name=? WHERE user_id=?",
            (user.username or "", user.first_name or "فرمانده", user.id)
        )
        for name in BUILDINGS:
            con.execute(
                "INSERT OR IGNORE INTO buildings(user_id,name,level) VALUES(?,?,0)",
                (user.id, name)
            )

def get_player(uid):
    with db() as con:
        return con.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone()

def income_per_period(uid):
    with db() as con:
        rows = con.execute(
            "SELECT name,level FROM buildings WHERE user_id=?", (uid,)
        ).fetchall()
    return sum(BUILDINGS[r["name"]]["income"] * r["level"] for r in rows)

def collect_income(uid):
    now = time.time()
    with db() as con:
        p = con.execute("SELECT last_collect FROM players WHERE user_id=?", (uid,)).fetchone()
        if not p:
            return 0, COLLECT_INTERVAL
        elapsed = max(0, now - p["last_collect"])
        periods = int(elapsed // COLLECT_INTERVAL)
        if periods == 0:
            return 0, int(COLLECT_INTERVAL - elapsed)
        amount = income_per_period(uid) * periods
        # فقط بازه‌های کامل مصرف می‌شوند؛ ثانیه‌های باقی‌مانده حفظ می‌شوند.
        con.execute(
            "UPDATE players SET coins=coins+?,last_collect=last_collect+? WHERE user_id=?",
            (amount, periods * COLLECT_INTERVAL, uid)
        )
        return amount, 0

def unit_power(uid):
    with db() as con:
        rows = con.execute(
            "SELECT unit,amount FROM army WHERE user_id=? AND amount>0", (uid,)
        ).fetchall()
    power = 0
    for row in rows:
        for category in UNITS.values():
            if row["unit"] in category:
                power += category[row["unit"]][1] * row["amount"]
                break
    return power

def home_text(uid):
    p = get_player(uid)
    income = income_per_period(uid)
    with db() as con:
        levels = con.execute(
            "SELECT COALESCE(SUM(level),0) n FROM buildings WHERE user_id=?", (uid,)
        ).fetchone()["n"]
        count = con.execute(
            "SELECT COALESCE(SUM(amount),0) n FROM army WHERE user_id=?", (uid,)
        ).fetchone()["n"]
    return (
        f"🏰 امپراتوری {p['name']}\\n\\n"
        f"💰 موجودی: {p['coins']:,} سکه\\n"
        f"⭐ امتیاز: {p['points']:,}\\n"
        f"📈 درآمد هر ۳۰ دقیقه: {income:,} سکه\\n"
        f"🏗 مجموع سطح ساختمان‌ها: {levels}\\n"
        f"⚔️ تعداد تجهیزات ارتش: {count}\\n"
        f"🔥 قدرت ارتش: {unit_power(uid):,}"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register(update.effective_user)
    await update.message.reply_text(
        "به تمدن خوش آمدی، فرمانده! 🏰\\nامپراتوری‌ات را بساز، درآمد جمع کن و بجنگ.",
        reply_markup=MENU
    )
    await update.message.reply_text(home_text(update.effective_user.id))

async def show_buildings(update, context):
    uid = update.effective_user.id
    with db() as con:
        rows = con.execute(
            "SELECT name,level FROM buildings WHERE user_id=? ORDER BY rowid", (uid,)
        ).fetchall()
    lines = ["🏗 ساختمان‌ها | برای ارتقا بنویس: ارتقا نام ساختمان", ""]
    for r in rows:
        level = r["level"]
        cost = int(BUILDINGS[r["name"]]["price"] * (1.5 ** level))
        lines.append(
            f"{r['name']}\\nسطح: {level}/{MAX_BUILDING_LEVEL} | "
            f"هزینه ارتقای بعدی: {cost:,} سکه"
        )
    await update.message.reply_text("\\n\\n".join(lines))

async def upgrade(update, name):
    uid = update.effective_user.id
    if name not in BUILDINGS:
        await update.message.reply_text("نام ساختمان دقیق نیست. از بخش 🏗 ساختمان‌ها فهرست را ببین.")
        return
    with db() as con:
        row = con.execute(
            "SELECT level FROM buildings WHERE user_id=? AND name=?", (uid, name)
        ).fetchone()
        level = row["level"]
        if level >= MAX_BUILDING_LEVEL:
            await update.message.reply_text("این ساختمان به سطح ۲۰ رسیده است.")
            return
        cost = int(BUILDINGS[name]["price"] * (1.5 ** level))
        p = con.execute("SELECT coins FROM players WHERE user_id=?", (uid,)).fetchone()
        if p["coins"] < cost:
            await update.message.reply_text(f"سکه کافی نیست. قیمت ارتقا: {cost:,} سکه")
            return
        con.execute("UPDATE players SET coins=coins-?,points=points+5 WHERE user_id=?",
                    (cost, uid))
        con.execute("UPDATE buildings SET level=level+1 WHERE user_id=? AND name=?",
                    (uid, name))
    await update.message.reply_text(
        f"✅ {name} به سطح {level+1} رسید.\\nهزینه پرداخت‌شده: {cost:,} سکه"
    )

async def show_shop(update, context):
    lines = ["🛒 فروشگاه نظامی | قیمت نهایی با ۵٪ تخفیف", ""]
    for category, items in UNITS.items():
        lines.append(f"【{category}】")
        for name, (base, power) in items.items():
            price = int(base * PRICE_DISCOUNT)
            lines.append(f"• {name}: {price:,} سکه | قدرت: {power}")
        lines.append("")
    lines.append("خرید: خرید نام تجهیزات تعداد")
    lines.append("مثال: خرید تانک سبک ۲")
    await update.message.reply_text("\\n".join(lines))

async def buy_unit(update, text):
    parts = text.split()
    if len(parts) < 2:
        await update.message.reply_text("مثال: خرید تانک سبک ۲")
        return
    try:
        amount = int(parts[-1]) if len(parts) >= 3 and parts[-1].isdigit() else 1
        name = " ".join(parts[1:-1]) if len(parts) >= 3 and parts[-1].isdigit() else " ".join(parts[1:])
        if not 1 <= amount <= 100:
            raise ValueError
    except ValueError:
        await update.message.reply_text("تعداد خرید باید بین ۱ تا ۱۰۰ باشد.")
        return
    details = next((cat[name] for cat in UNITS.values() if name in cat), None)
    if details is None:
        await update.message.reply_text("این وسیله در فروشگاه نیست؛ نام را دقیق وارد کن.")
        return
    price = int(details[0] * PRICE_DISCOUNT) * amount
    uid = update.effective_user.id
    with db() as con:
        p = con.execute("SELECT coins FROM players WHERE user_id=?", (uid,)).fetchone()
        if p["coins"] < price:
            await update.message.reply_text(f"سکه کافی نیست. قیمت کل: {price:,} سکه")
            return
        con.execute("UPDATE players SET coins=coins-? WHERE user_id=?", (price, uid))
        con.execute("""
            INSERT INTO army(user_id,unit,amount) VALUES(?,?,?)
            ON CONFLICT(user_id,unit) DO UPDATE SET amount=amount+excluded.amount
        """, (uid, name, amount))
    await update.message.reply_text(
        f"✅ خرید انجام شد: {name} × {amount}\\nمبلغ: {price:,} سکه"
    )

async def show_army(update, context):
    uid = update.effective_user.id
    with db() as con:
        rows = con.execute(
            "SELECT unit,amount FROM army WHERE user_id=? AND amount>0 ORDER BY unit", (uid,)
        ).fetchall()
    if not rows:
        await update.message.reply_text("ارتشت خالی است؛ از فروشگاه نظامی نیرو بخر.")
        return
    lines = ["⚔️ ارتش تو:"]
    lines.extend(f"• {r['unit']}: {r['amount']}" for r in rows)
    lines.append(f"\\nقدرت کل: {unit_power(uid):,}")
    await update.message.reply_text("\\n".join(lines))

async def daily_mission(update, context):
    uid = update.effective_user.id
    today = date.today().isoformat()
    with db() as con:
        row = con.execute(
            "SELECT done FROM missions WHERE user_id=? AND day=?", (uid, today)
        ).fetchone()
        if row and row["done"]:
            await update.message.reply_text("مأموریت امروز را انجام داده‌ای؛ فردا دوباره بیا.")
            return
        con.execute(
            "INSERT OR IGNORE INTO missions(user_id,day,done) VALUES(?,?,0)", (uid, today)
        )
        con.execute("UPDATE missions SET done=1 WHERE user_id=? AND day=?", (uid, today))
        con.execute(
            "UPDATE players SET coins=coins+250,points=points+20 WHERE user_id=?", (uid,)
        )
    await update.message.reply_text("🎯 مأموریت روزانه تکمیل شد! پاداش: ۲۵۰ سکه و ۲۰ امتیاز.")

async def leaderboard(update, context):
    with db() as con:
        rows = con.execute(
            "SELECT name,points,coins FROM players ORDER BY points DESC,coins DESC LIMIT 10"
        ).fetchall()
    lines = ["🏆 ۱۰ امپراتوری برتر"]
    lines.extend(
        f"{i}. {r['name']} — {r['points']:,} امتیاز | {r['coins']:,} سکه"
        for i, r in enumerate(rows, 1)
    )
    await update.message.reply_text("\\n".join(lines))

async def do_battle(update, opponent_id, mode):
    uid = update.effective_user.id
    if opponent_id == uid:
        await update.message.reply_text("نمی‌توانی با خودت بجنگی!")
        return
    my_power = unit_power(uid)
    their_power = unit_power(opponent_id)
    if my_power <= 0:
        await update.message.reply_text("ارتش نداری! اول از فروشگاه نیرو بخر.")
        return
    # شانس محدود برای جلوگیری از قطعی بودن نتیجه؛ قدرت ارتش همچنان عامل اصلی است.
    my_score = my_power * random.uniform(0.85, 1.15)
    their_score = their_power * random.uniform(0.85, 1.15)
    winner = uid if my_score >= their_score else opponent_id
    with db() as con:
        if winner == uid:
            target = con.execute("SELECT coins FROM players WHERE user_id=?", (opponent_id,)).fetchone()
            loot = min(250, max(0, target["coins"] // 10))
            con.execute(
                "UPDATE players SET coins=coins+?,points=points+25 WHERE user_id=?", (loot, uid)
            )
            con.execute(
                "UPDATE players SET coins=MAX(0,coins-?),points=MAX(0,points-8) WHERE user_id=?",
                (loot, opponent_id)
            )
            result = f"🏆 پیروز شدی!\\nغنیمت: {loot:,} سکه\\nامتیاز: +۲۵"
        else:
            con.execute(
                "UPDATE players SET points=MAX(0,points-5) WHERE user_id=?", (uid,)
            )
            result = "💥 شکست خوردی! این بار حریف قوی‌تر بود. ۵ امتیاز از دست دادی."
        con.execute(
            "INSERT INTO battles(attacker,defender,winner,created,mode) VALUES(?,?,?,?,?)",
            (uid, opponent_id, winner, time.time(), mode)
        )
    await update.message.reply_text(result)

async def random_attack(update, context):
    uid = update.effective_user.id
    with db() as con:
        candidates = con.execute(
            "SELECT user_id,name FROM players WHERE user_id<>? ORDER BY RANDOM() LIMIT 1",
            (uid,)
        ).fetchone()
    if not candidates:
        await update.message.reply_text("هنوز بازیکن دیگری در تمدن ثبت‌نام نکرده است.")
        return
    await update.message.reply_text(f"🎲 حریف تصادفی پیدا شد: {candidates['name']}\\nنبرد شروع شد...")
    await do_battle(update, candidates["user_id"], "random")

async def named_attack(update, username):
    username = username.strip().lstrip("@")
    if not username:
        await update.message.reply_text("نام کاربری را بفرست. مثال: حمله player123")
        return
    with db() as con:
        target = con.execute(
            "SELECT user_id FROM players WHERE lower(username)=lower(?)", (username,)
        ).fetchone()
    if not target:
        await update.message.reply_text(
            "بازیکن پیدا نشد. او باید حداقل یک‌بار ربات تمدن را /start کرده باشد."
        )
        return
    await do_battle(update, target["user_id"], "username")

async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    with db() as con:
        users = con.execute(
            "SELECT user_id,last_collect,last_reminder FROM players"
        ).fetchall()
    for p in users:
        if now - p["last_collect"] >= REMINDER_INTERVAL and now - p["last_reminder"] >= REMINDER_INTERVAL:
            try:
                await context.bot.send_message(
                    chat_id=p["user_id"],
                    text="⏰ فرمانده! ۲۴ ساعت از آخرین جمع‌آوری درآمدت گذشته. درآمد تمدنت منتظر توست؛ وارد بازی شو و آن را جمع‌آوری کن!"
                )
                with db() as con:
                    con.execute(
                        "UPDATE players SET last_reminder=? WHERE user_id=?",
                        (now, p["user_id"])
                    )
            except Exception as exc:
                log.warning("Reminder failed for %s: %s", p["user_id"], exc)

async def help_message(update, context):
    await update.message.reply_text(
        "📘 راهنمای تمدن\\n"
        "• درآمد هر ۳۰ دقیقه قابل جمع‌آوری است و با آفلاین بودن از بین نمی‌رود.\\n"
        "• ساختمان‌ها تا سطح ۲۰ ارتقا دارند.\\n"
        "• قیمت تجهیزات در فروشگاه نمایش داده می‌شود؛ ۵٪ تخفیف دارند.\\n"
        "• خرید: خرید تانک سبک ۲\\n"
        "• ارتقا: ارتقا 🌾 مزرعه\\n"
        "• حمله به بازیکن: حمله username\\n"
        "• حمله تصادفی: دکمه 🎲 حمله تصادفی\\n"
        "• مأموریت روزانه هر روز یک‌بار پاداش می‌دهد."
    )

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register(update.effective_user)
    text = (update.message.text or "").strip()
    uid = update.effective_user.id
    if text == "🏙 شهر من":
        await update.message.reply_text(home_text(uid))
    elif text == "🏗 ساختمان‌ها":
        await show_buildings(update, context)
    elif text == "💰 جمع‌آوری درآمد":
        amount, wait = collect_income(uid)
        if amount:
            await update.message.reply_text(f"💰 {amount:,} سکه جمع‌آوری شد!")
        else:
            await update.message.reply_text(
                f"هنوز ۳۰ دقیقه کامل نشده؛ {max(1,(wait+59)//60)} دقیقه دیگر صبر کن."
            )
    elif text == "🛒 فروشگاه نظامی":
        await show_shop(update, context)
    elif text == "⚔️ ارتش من":
        await show_army(update, context)
    elif text == "🎯 مأموریت روزانه":
        await daily_mission(update, context)
    elif text == "🏆 رتبه‌بندی":
        await leaderboard(update, context)
    elif text == "🎲 حمله تصادفی":
        await random_attack(update, context)
    elif text.startswith("حمله "):
        await named_attack(update, text[len("حمله "):])
    elif text.startswith("خرید "):
        await buy_unit(update, text)
    elif text.startswith("ارتقا "):
        await upgrade(update, text[len("ارتقا "):].strip())
    elif text in BUILDINGS:
        await upgrade(update, text)
    elif text == "ℹ️ راهنما":
        await help_message(update, context)
    else:
        await update.message.reply_text("از دکمه‌های منو استفاده کن یا دستور ℹ️ راهنما را ببین.")

async def after_start(app):
    app.job_queue.run_repeating(reminder_job, interval=300, first=30)

def main():
    if not TOKEN:
        raise RuntimeError(
            "متغیر TELEGRAM_BOT_TOKEN تنظیم نشده است؛ آن را در Environment Variables هاست وارد کن."
        )
    init_db()
    app = Application.builder().token(TOKEN).post_init(after_start).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Tamaddon bot is running.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
