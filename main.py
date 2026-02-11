#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Aiogram pet-bot (webhook-ready, fallback to polling).
Env:
  TELEGRAM_BOT_TOKEN  (required)
  USE_WEBHOOK=1       (optional, enable webhook mode)
  WEBHOOK_URL         (required if USE_WEBHOOK=1) e.g. https://yourbot.domain/
  PORT                (optional, default 8443)
DB: pet_bot.db (sqlite, async via aiosqlite)
Timezone: Europe/Moscow (MSK)
"""

import os
import asyncio
import random
from datetime import datetime, timedelta, time
import aiosqlite
import pytz
from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# ---- Config ----
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is required")

USE_WEBHOOK = os.getenv("USE_WEBHOOK", "0") == "1"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/") if os.getenv("WEBHOOK_URL") else ""
PORT = int(os.getenv("PORT", "8443"))

DB_PATH = "pet_bot.db"
MSK = pytz.timezone("Europe/Moscow")

# ---- Logging ----
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("petbot")
logger.info("Bot starting; webhook=%s", USE_WEBHOOK)

# ---- Database helpers (aiosqlite) ----

CREATE_TABLE_SQL = """
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
);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_TABLE_SQL)
        await db.commit()

async def row_to_dict(row):
    if not row:
        return None
    # get column names
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("PRAGMA table_info(pets)")
        cols = [r[1] for r in await cur.fetchall()]
        await cur.close()
    return dict(zip(cols, row))

async def get_pet(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM pets WHERE chat_id = ?", (chat_id,))
        row = await cur.fetchone()
        await cur.close()
    return await row_to_dict(row)

async def create_pet(chat_id, owner_name, username):
    pet_name = f"Ваня({username or 'без_ника'})"
    today = datetime.now(MSK).date().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO pets (chat_id, owner_name, pet_name, last_reset) VALUES (?, ?, ?, ?)",
            (chat_id, owner_name, pet_name, today)
        )
        await db.commit()
    return await get_pet(chat_id)

async def update_pet(chat_id, **kwargs):
    if not kwargs:
        return
    keys = list(kwargs.keys())
    vals = [kwargs[k] for k in keys]
    set_clause = ", ".join([f"{k} = ?" for k in keys])
    vals.append(chat_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE pets SET {set_clause} WHERE chat_id = ?", vals)
        await db.commit()

# ---- Time helpers ----
def now_msk():
    return datetime.now(MSK)

def meal_period_for_dt(dt: datetime):
    h = dt.hour
    if 5 <= h <= 11:
        return "morning"
    if 12 <= h <= 16:
        return "afternoon"
    return "evening"

# ---- Game logic: daily reset ----
async def apply_daily_reset_for_all():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM pets")
        rows = await cur.fetchall()
        # get column names
        colinfo = await db.execute("PRAGMA table_info(pets)")
        cols = [r[1] for r in await colinfo.fetchall()]
        await colinfo.close()

        for row in rows:
            pet = dict(zip(cols, row))
            chat_id = pet['chat_id']
            fed_m = pet['feed_morning'] > 0
            fed_a = pet['feed_afternoon'] > 0
            fed_e = pet['feed_evening'] > 0
            walked_m = pet['walk_morning'] > 0
            walked_e = pet['walk_evening'] > 0

            anger = pet.get('anger', 0)
            # if day had no 3 feeds and no 2 walks -> anger = 100
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
            if hunger >= 100:
                updates['sick_flag'] = 1
                updates['boycott_active'] = 1
                updates['total_feeds'] = max(0, pet.get('total_feeds', 0) - 3)
                updates['total_walks'] = max(0, pet.get('total_walks', 0) - 2)
                hunger = max(hunger, 100)

            if anger >= 100:
                updates['experience'] = max(0, pet.get('experience', 0) - 5)
                updates['boycott_active'] = 1

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

            # apply update
            keys = list(updates.keys())
            vals = [updates[k] for k in keys]
            set_clause = ", ".join([f"{k} = ?" for k in keys])
            vals.append(chat_id)
            await db.execute(f"UPDATE pets SET {set_clause} WHERE chat_id = ?", vals)

        await db.commit()
    logger.info("Daily reset applied to all pets")

async def daily_reset_background():
    while True:
        now = now_msk()
        tomorrow = (now + timedelta(days=1)).date()
        next_midnight = MSK.localize(datetime.combine(tomorrow, time.min))
        wait = (next_midnight - now).total_seconds()
        logger.info("Waiting %.0f seconds until next daily reset at %s", wait, next_midnight.isoformat())
        await asyncio.sleep(wait)
        try:
            await apply_daily_reset_for_all()
        except Exception:
            logger.exception("Error in daily reset")

# ---- Post-action handling (boycott/sick) ----
async def handle_post_action(chat_id):
    pet = await get_pet(chat_id)
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
        await update_pet(chat_id, **updates)
        logger.info("Post-action updates for %s: %s", chat_id, updates)

# ---- Bot handlers ----
bot = Bot(token=TELEGRAM_BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user = message.from_user
    await create_pet(message.chat.id, user.full_name or user.username or "Игрок", user.username)
    await message.answer("Привет! Питомец создан. Используйте /name, /feed, /walk, /status, /help")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Правила (кратко):\n"
        "- Кормить 3 раза в день (желательно 09:00/14:00/19:00 МСК)\n"
        "- Гулять 2 раза в день (желательно 09:00 и 19:00)\n"
        "- Пропуски влияют на шкалы негодования и голода\n"
        "- При hunger>=100 или anger>=100 питомец уходит в бойкот и ждёт /feed или /walk; после команды ждём 2 часа для восстановления."
    )

@dp.message(Command("name"))
async def cmd_name(message: Message):
    args = message.get_args().strip()
    if not args:
        await message.answer("Использование: /name <имя>")
        return
    pet_name = f"Ваня({args})"
    await update_pet(message.chat.id, pet_name=pet_name)
    await message.answer(f"Имя питомца установлено: {pet_name}")

@dp.message(Command("status"))
async def cmd_status(message: Message):
    pet = await get_pet(message.chat.id)
    if not pet:
        await message.answer("Питомец не найден. Введите /start")
        return
    text = (
        f"Статус {pet['pet_name']}:\n"
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
    await message.answer(text)

@dp.message(Command("feed"))
async def cmd_feed(message: Message):
    pet = await get_pet(message.chat.id)
    if not pet:
        await message.answer("Питомец не найден. /start")
        return

    # check active boycott_until
    if pet.get('boycott_until'):
        try:
            until = datetime.fromisoformat(pet['boycott_until']).astimezone(MSK)
            if now_msk() < until:
                await message.answer(f"Питомец на таймере до {until:%Y-%m-%d %H:%M:%S} МСК. Попробуйте позже.")
                return
        except Exception:
            pass

    period = meal_period_for_dt(now_msk())
    field = f"feed_{period}"
    current = pet.get(field, 0)
    new_cnt = current + 1

    messages = []
    # Overfeed: >2 in same period
    if new_cnt > 2:
        new_exp = max(0, pet.get('experience', 0) - 2)
        await update_pet(message.chat.id, experience=new_exp)
        messages.append("Питомец переел — опыт -2.")
        if random.random() < 0.01:
            await update_pet(message.chat.id, hunger_scale=min(200, pet.get('hunger_scale', 0) + 50))
            messages.append("Шанс сработал: питомец заболел от переедания.")

    await update_pet(message.chat.id,
                     **{field: new_cnt,
                        "total_feeds": pet.get('total_feeds', 0) + 1,
                        "experience": pet.get('experience', 0) + 1})

    await handle_post_action(message.chat.id)

    pet2 = await get_pet(message.chat.id)
    if pet2.get('hunger_scale', 0) >= 100:
        await update_pet(message.chat.id, sick_until=(now_msk() + timedelta(hours=2)).isoformat())
        messages.append("Питомец болен — выздоровление начнётся через 2 часа после команды.")

    await message.answer("Покормлено. " + (" ".join(messages) if messages else ""))

@dp.message(Command("walk"))
async def cmd_walk(message: Message):
    pet = await get_pet(message.chat.id)
    if not pet:
        await message.answer("Питомец не найден. /start")
        return

    if pet.get('boycott_until'):
        try:
            until = datetime.fromisoformat(pet['boycott_until']).astimezone(MSK)
            if now_msk() < until:
                await message.answer(f"Питомец на таймере до {until:%Y-%m-%d %H:%M:%S} МСК. Попробуйте позже.")
                return
        except Exception:
            pass

    h = now_msk().hour
    period = "morning" if 5 <= h <= 11 else "evening"
    field = f"walk_{period}"
    current = pet.get(field, 0)
    new_cnt = current + 1

    await update_pet(message.chat.id,
                     **{field: new_cnt,
                        "total_walks": pet.get('total_walks', 0) + 1,
                        "experience": pet.get('experience', 0) + 1})

    messages = []
    if random.random() < 0.01:
        loss = random.randint(1, 3)
        await update_pet(message.chat.id, experience=max(0, pet.get('experience', 0) - loss))
        messages.append(f"Во время прогулки случилось неприятное событие — опыт -{loss}.")

    await handle_post_action(message.chat.id)

    pet2 = await get_pet(message.chat.id)
    if pet2.get('hunger_scale', 0) >= 100:
        await update_pet(message.chat.id, sick_until=(now_msk() + timedelta(hours=2)).isoformat())
        messages.append("Питомец болен — выздоровление начнётся через 2 часа после команды.")

    await message.answer("Прогулка выполнена. " + (" ".join(messages) if messages else ""))

# ---- Startup / Runner ----
async def on_startup(app=None):
    # ensure db
    await init_db()
    # set webhook if needed
    if USE_WEBHOOK and WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL)
        logger.info("Webhook set to %s", WEBHOOK_URL)
    # start background daily reset
    asyncio.create_task(daily_reset_background())

async def on_shutdown(app=None):
    # remove webhook if used
    if USE_WEBHOOK and WEBHOOK_URL:
        try:
            await bot.delete_webhook()
        except Exception:
            pass
    await bot.session.close()

async def run_webhook():
    await init_db()
    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/")
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_shutdown)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Webhook server started on port %s", PORT)
    # keep alive
    while True:
        await asyncio.sleep(3600)

async def run_polling():
    await init_db()
    await on_startup()
    logger.info("Starting polling (fallback).")
    # aiogram v3: start polling
    await dp.start_polling(bot)

def main():
    if USE_WEBHOOK and WEBHOOK_URL:
        asyncio.run(run_webhook())
    else:
        asyncio.run(run_polling())

if __name__ == "__main__":
    main()
