#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minimal Telegram pet-bot.
Env variables:
- TELEGRAM_BOT_TOKEN (required)
- USE_WEBHOOK (optional, "1" to enable webhook mode)
- WEBHOOK_URL (required if USE_WEBHOOK=1) e.g. https://yourbot.example.com/
- PORT (optional, default 8443)

Features:
- feed 3x/day, walk 2x/day
- recommended schedule: 09:00 (breakfast+walk), 14:00 (lunch), 19:00 (dinner+walk) MSK
- daily reset at 00:00 MSK (resets daily counters and applies penalties)
- anger & hunger scales (logic per spec)
- boycott & sickness logic:
    * if hunger >=100 or anger >=100 at daily reset -> sick_flag and boycott_active set (pet waits for /feed or /walk)
    * while boycott_active==1 owner must call /feed or /walk; after that bot sets boycott_until = now+2h and/or sick_until = now+2h
- simple sqlite storage (pet_bot.db)
- no forbidden content; minor random "unpleasant events" (aggressive passersby) with <1% chance
"""

import os
import logging
import random
import asyncio
import sqlite3
from datetime import datetime, timedelta, time
import pytz

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ---------- Config ----------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set in environment")

USE_WEBHOOK = os.environ.get("USE_WEBHOOK", "0") == "1"
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
PORT = int(os.environ.get("PORT", "8443"))

MSK = pytz.timezone("Europe/Moscow")
DB_PATH = "pet_bot.db"

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
logger.info("Using TELEGRAM_BOT_TOKEN: %s", bool(TELEGRAM_BOT_TOKEN))
logger.info("Webhook mode: %s", USE_WEBHOOK)

# ---------- DB helpers ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pets (
            chat_id INTEGER PRIMARY KEY,
            owner_name TEXT,
            pet_name TEXT,
            feed_morning INTEGER DEFAULT 0,
            feed_afternoon INTEGER DEFAULT 0,
            feed_evening INTEGER DEFAULT 0,
            walk_morning INTEGER DEFAULT 0,
            walk_evening INTEGER DEFAULT 0,
            total_feeds INTEGER DEFAULT 0,
            total_walks INTEGER DEFAULT 0,
            anger INTEGER DEFAULT 0,
            hunger_scale INTEGER DEFAULT 0,
            sick_until TEXT DEFAULT NULL,
            boycott_until TEXT DEFAULT NULL,
            experience INTEGER DEFAULT 0,
            days_lived INTEGER DEFAULT 0,
            last_reset TEXT DEFAULT NULL,
            boycott_active INTEGER DEFAULT 0,
            sick_flag INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def row_to_pet(row):
    if not row:
        return None
    # get columns
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(pets)")
    cols = [c[1] for c in cur.fetchall()]
    conn.close()
    return dict(zip(cols, row))

def get_pet(chat_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT * FROM pets WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    conn.close()
    return row_to_pet(row)

def create_pet(chat_id, owner_name, username):
    pet_name = f"Ваня({username or 'без_ника'})"
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    today = datetime.now(MSK).date().isoformat()
    cur.execute("""
        INSERT OR IGNORE INTO pets (chat_id, owner_name, pet_name, last_reset)
        VALUES (?, ?, ?, ?)
    """, (chat_id, owner_name, pet_name, today))
    conn.commit()
    conn.close()
    return get_pet(chat_id)

def update_pet(chat_id, **kwargs):
    if not kwargs:
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    parts = []
    vals = []
    for k, v in kwargs.items():
        parts.append(f"{k} = ?")
        vals.append(v)
    vals.append(chat_id)
    cur.execute(f"UPDATE pets SET {', '.join(parts)} WHERE chat_id = ?", vals)
    conn.commit()
    conn.close()

# ---------- Time helpers ----------
def now_msk():
    return datetime.now(MSK)

def meal_period_for_dt(dt: datetime):
    h = dt.hour
    if 5 <= h <= 11:
        return "morning"
    if 12 <= h <= 16:
        return "afternoon"
    return "evening"

# ---------- Game logic: daily reset ----------
def apply_daily_reset_all():
    """
    Runs at 00:00 MSK.
    Applies penalties per rules and resets daily counters.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT * FROM pets")
    rows = cur.fetchall()
    cur.execute("PRAGMA table_info(pets)")
    cols = [c[1] for c in cur.fetchall()]

    for r in rows:
        pet = dict(zip(cols, r))
        chat_id = pet['chat_id']

        fed_m = pet['feed_morning'] > 0
        fed_a = pet['feed_afternoon'] > 0
        fed_e = pet['feed_evening'] > 0
        walked_m = pet['walk_morning'] > 0
        walked_e = pet['walk_evening'] > 0

        anger = pet.get('anger', 0)
        # if during the day you didn't feed 3x AND didn't walk 2x -> anger = 100
        if (not fed_m) and (not fed_a) and (not fed_e) and (not walked_m) and (not walked_e):
            anger = 100
        else:
            if not fed_m:
                anger += random.randint(28, 30)
            if not walked_m:
                anger += random.randint(16, 20)
            if not fed_a:
                anger += random.randint(20, 20)
            if not fed_e:
                anger += random.randint(32, 34)
            if not walked_e:
                anger += random.randint(16, 20)
            if anger > 100:
                anger = 100

        hunger_add = 0
        if not fed_m:
            hunger_add += 20
        if not fed_a:
            hunger_add += 20
        if not fed_e:
            hunger_add += 20
        if not walked_m:
            hunger_add += 20
        if not walked_e:
            hunger_add += 20

        hunger = pet.get('hunger_scale', 0) + hunger_add

        updates = {}
        # if hunger >=100 -> sick_flag and boycott_active; apply penalties to totals
        if hunger >= 100:
            updates['sick_flag'] = 1
            updates['boycott_active'] = 1
            updates['total_feeds'] = max(0, pet.get('total_feeds', 0) - 3)
            updates['total_walks'] = max(0, pet.get('total_walks', 0) - 2)
            hunger = max(hunger, 100)

        # anger >=100 -> bite -> -5 experience and boycott_active
        if anger >= 100:
            updates['experience'] = max(0, pet.get('experience', 0) - 5)
            updates['boycott_active'] = 1

        # always write anger and hunger
        updates['anger'] = anger
        updates['hunger_scale'] = hunger
        updates['days_lived'] = pet.get('days_lived', 0) + 1

        # reset daily counters
        updates.update({
            'feed_morning': 0,
            'feed_afternoon': 0,
            'feed_evening': 0,
            'walk_morning': 0,
            'walk_evening': 0,
            'last_reset': datetime.now(MSK).date().isoformat()
        })

        update_pet(chat_id, **updates)
        logger.info("Daily reset applied for %s: %s", chat_id, updates)

    conn.close()

async def daily_reset_loop():
    """
    Background task: wait until next 00:00 MSK, run reset, then sleep 24h.
    """
    while True:
        now = now_msk()
        # compute next midnight MSK
        tomorrow = (now + timedelta(days=1)).date()
        next_midnight = MSK.localize(datetime.combine(tomorrow, time.min))
        wait_seconds = (next_midnight - now).total_seconds()
        logger.info("Daily reset scheduled in %.0f seconds (at %s)", wait_seconds, next_midnight.isoformat())
        await asyncio.sleep(wait_seconds)
        try:
            apply_daily_reset_all()
        except Exception:
            logger.exception("Error during daily reset")

# ---------- Post-action handler (boycott/sick) ----------
def handle_post_action(chat_id):
    """
    Called after /feed or /walk; if boycott_active==1 -> we clear it and set boycott_until = now+2h.
    If sick_flag==1 -> clear it and set sick_until = now+2h.
    """
    pet = get_pet(chat_id)
    if not pet:
        return
    updates = {}
    now = now_msk()
    if pet.get('boycott_active', 0) == 1:
        updates['boycott_active'] = 0
        updates['boycott_until'] = (now + timedelta(hours=2)).isoformat()
    if pet.get('sick_flag', 0) == 1:
        updates['sick_flag'] = 0
        updates['sick_until'] = (now + timedelta(hours=2)).isoformat()
    if updates:
        update_pet(chat_id, **updates)
        logger.info("Post-action updates for %s: %s", chat_id, updates)

# ---------- Commands ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    owner_name = user.full_name or user.username or "Игрок"
    create_pet(update.effective_chat.id, owner_name, user.username)
    await update.message.reply_text(
        "Привет! Ваш питомец создан.\n"
        "Команды: /status /feed /walk /name <имя> /help\n"
        "Рекомендуем: 09:00 (завтрак+прогулка), 14:00 (обед), 19:00 (ужин+прогука) МСК."
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Правила кратко:\n"
        "- Кормить 3 раза в день, гулять 2 раза. Пропуски увеличивают шкалы.\n"
        "- Если hunger>=100 или anger>=100 — питомец уходит в бойкот (ожидает /feed или /walk), затем после команды ждёт 2 часа.\n"
        "- Переедание (>2 в одном периоде) даёт -2 опыта и шанс болезни <1%."
    )

async def cmd_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /name <имя>")
        return
    nickname = " ".join(context.args).strip()
    pet_name = f"Ваня({nickname})"
    update_pet(update.effective_chat.id, pet_name=pet_name)
    await update.message.reply_text(f"Имя питомца установлено: {pet_name}")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pet = get_pet(update.effective_chat.id)
    if not pet:
        await update.message.reply_text("Питомец не найден. Введите /start")
        return
    text = (
        f"Статус питомца {pet['pet_name']}:\n"
        f"Кормления сегодня — утро:{pet['feed_morning']} обед:{pet['feed_afternoon']} вечер:{pet['feed_evening']}\n"
        f"Прогулки — утро:{pet['walk_morning']} вечер:{pet['walk_evening']}\n"
        f"Всего кормежек: {pet['total_feeds']}  Всего прогулок: {pet['total_walks']}\n"
        f"Шкала негодования: {pet['anger']} /100\n"
        f"Шкала голода/болезни: {pet['hunger_scale']} /100\n"
        f"Опыт: {pet['experience']}\nДней: {pet['days_lived']}\n"
    )
    if pet.get('boycott_active'):
        text += "Питомец в бойкоте и ждёт команды /feed или /walk.\n"
    if pet.get('boycott_until'):
        text += f"Бойкот/таймер до: {pet.get('boycott_until')}\n"
    if pet.get('sick_flag'):
        text += "Питомец болен — вызовите /feed или /walk, затем подождите 2 часа.\n"
    if pet.get('sick_until'):
        text += f"Выздоровление до: {pet.get('sick_until')}\n"
    await update.message.reply_text(text)

async def cmd_feed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    pet = get_pet(chat_id)
    if not pet:
        await update.message.reply_text("Питомец не найден. /start")
        return

    # If boycott_until active and not expired -> forbid immediate action
    if pet.get('boycott_until'):
        try:
            until = datetime.fromisoformat(pet['boycott_until']).astimezone(MSK)
            if now_msk() < until:
                await update.message.reply_text(f"Питомец на таймере бойкота/выздоровления до {until:%Y-%m-%d %H:%M:%S} МСК. Попробуйте позже.")
                return
        except Exception:
            pass

    period = meal_period_for_dt(now_msk())
    field = f"feed_{period}"
    current = pet.get(field, 0)
    new_cnt = current + 1

    messages = []
    # Overfeed: more than 2 times in same period
    if new_cnt > 2:
        new_exp = max(0, pet.get('experience', 0) - 2)
        update_pet(chat_id, experience=new_exp)
        messages.append("Питомец переел — опыт -2.")
        if random.random() < 0.01:
            update_pet(chat_id, hunger_scale=min(200, pet.get('hunger_scale', 0) + 50))
            messages.append("Шанс сработал: питомец заболел от переедания.")

    update_pet(chat_id, **{field: new_cnt, "total_feeds": pet.get('total_feeds', 0) + 1, "experience": pet.get('experience', 0) + 1})

    # refresh pet
    handle_post_action(chat_id)

    pet2 = get_pet(chat_id)
    if pet2.get('hunger_scale', 0) >= 100:
        update_pet(chat_id, sick_until=(now_msk() + timedelta(hours=2)).isoformat())
        messages.append("Питомец болен — выздоровление начнётся через 2 часа после команды.")

    await update.message.reply_text("Покормлено. " + (" ".join(messages) if messages else ""))

async def cmd_walk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    pet = get_pet(chat_id)
    if not pet:
        await update.message.reply_text("Питомец не найден. /start")
        return

    if pet.get('boycott_until'):
        try:
            until = datetime.fromisoformat(pet['boycott_until']).astimezone(MSK)
            if now_msk() < until:
                await update.message.reply_text(f"Питомец на таймере бойкота/выздоровления до {until:%Y-%m-%d %H:%M:%S} МСК. Попробуйте позже.")
                return
        except Exception:
            pass

    h = now_msk().hour
    period = "morning" if 5 <= h <= 11 else "evening"
    field = f"walk_{period}"
    current = pet.get(field, 0)
    new_cnt = current + 1

    update_pet(chat_id, **{field: new_cnt, "total_walks": pet.get('total_walks', 0) + 1, "experience": pet.get('experience', 0) + 1})

    messages = []
    # safe random negative event <1%
    if random.random() < 0.01:
        loss = random.randint(1, 3)
        update_pet(chat_id, experience=max(0, pet.get('experience', 0) - loss))
        messages.append(f"Во время прогулки случилось нападение зоофилов! питомец болен — опыт -{loss}.")

    # handle boycott/sick post-action
    handle_post_action(chat_id)

    pet2 = get_pet(chat_id)
    if pet2.get('hunger_scale', 0) >= 100:
        update_pet(chat_id, sick_until=(now_msk() + timedelta(hours=2)).isoformat())
        messages.append("Питомец болен — выздоровление начнётся через 2 часа после команды.")

    await update.message.reply_text("Прогулка выполнена. " + (" ".join(messages) if messages else ""))

# ---------- App startup ----------
async def start_background_tasks(application):
    # start daily reset loop
    application.create_task(daily_reset_loop())

def build_and_run():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("name", cmd_name))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("feed", cmd_feed))
    app.add_handler(CommandHandler("walk", cmd_walk))

    # startup tasks
    app.post_init = start_background_tasks

    # run webhook or polling resilient
    if USE_WEBHOOK and WEBHOOK_URL:
        logger.info("Starting webhook on %s:%s -> %s", "0.0.0.0", PORT, WEBHOOK_URL)
        app.run_webhook(listen="0.0.0.0", port=PORT, webhook_url=WEBHOOK_URL)
    else:
        logger.info("Starting polling (timeout=60s, resilient loop)")
        # resilient polling
        while True:
            try:
                app.run_polling(poll_interval=1.0, timeout=60, drop_pending_updates=True)
                break
            except Exception as e:
                logger.exception("Polling failed, retrying in 10s: %s", e)
                try:
                    asyncio.run(asyncio.sleep(10))
                except Exception:
                    import time
                    time.sleep(10)

if __name__ == "__main__":
    build_and_run()
