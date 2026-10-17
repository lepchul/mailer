#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Рассыльщик от имени аккаунта Telegram + панель управления в боте.

Как это устроено:
  * user  — Telethon-клиент ТВОЕГО АККАУНТА (api_id + api_hash). Он и шлёт сообщения в чаты.
  * bot   — обычный бот (токен от @BotFather). Это только панель управления:
            шаблоны, список чатов, настройки, запуск/остановка.
  * Всё хранится в SQLite-файле mailer.db (шаблоны, чаты, настройки, сессия аккаунта).

Настройка — переменные окружения (или впиши значения прямо ниже):
  API_ID      — с https://my.telegram.org  (число)
  API_HASH    — оттуда же (строка)
  BOT_TOKEN   — токен панели от @BotFather
  ADMIN_IDS   — твой Telegram ID (несколько — через запятую). Остальным бот не отвечает.

Возможности текста шаблона:
  {привет|здравствуйте|добрый день}  — спинтакс: каждый раз выбирается случайный вариант
  {title}                            — подставится название чата
  Форматирование Telegram (жирный, курсив, ссылки) сохраняется, если писать
  шаблон в боте обычным форматированием Telegram.

ВАЖНО: массовая рассылка с личного аккаунта — это риск бана/ограничения аккаунта.
Не ставь маленькие задержки, не рассылай в чаты, где это запрещено правилами.
"""

import asyncio
import logging
import os
import random
import re
import sqlite3
import time

from telethon import Button, TelegramClient, errors, events, utils
from telethon.sessions import StringSession

def load_env(path=".env"):
    """Мини-загрузчик файла .env (без доп. библиотек).
    Строки вида KEY=value попадают в переменные окружения.
    Уже заданные переменные (например, в панели хостинга) НЕ перезаписываются."""
    base = os.path.dirname(os.path.abspath(__file__))
    full = os.path.join(base, path)
    if not os.path.exists(full):
        return
    with open(full, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

# ───────────────────────────── НАСТРОЙКИ ─────────────────────────────
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}

DB_PATH = os.getenv("DB_PATH", "mailer.db")

# Значения по умолчанию (потом меняются в панели: ⚙️ Настройки)
DEFAULTS = {
    "delay_min": "20",   # мин. пауза между сообщениями, сек
    "delay_max": "45",   # макс. пауза между сообщениями, сек
    "loop_min": "0",     # повторять круг каждые N минут (0 = один проход и стоп)
    "session": "",       # StringSession аккаунта (заполняется при входе)
}
PAGE_SIZE = 10  # чатов на странице в списке
# ──────────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mailer")

# ───────────────────────────── БАЗА ДАННЫХ ─────────────────────────────
DB = sqlite3.connect(DB_PATH, check_same_thread=False)
DB.row_factory = sqlite3.Row
DB.executescript(
    """
    CREATE TABLE IF NOT EXISTS templates(
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        text TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS chats(
        id          INTEGER PRIMARY KEY,      -- Telegram peer id (с -100 для супергрупп)
        title       TEXT,
        template_id INTEGER                   -- NULL = брать случайный шаблон
    );
    CREATE TABLE IF NOT EXISTS settings(
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    """
)


def q(sql, args=(), one=False):
    """Короткий помощник: выполнить SQL и вернуть строки (или одну строку)."""
    cur = DB.execute(sql, args)
    DB.commit()
    rows = cur.fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def get_setting(key):
    row = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return row["value"] if row else DEFAULTS[key]


def set_setting(key, value):
    q("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))


# ───────────────────────────── КЛИЕНТЫ ─────────────────────────────
bot = TelegramClient("bot_panel", API_ID, API_HASH)
user: TelegramClient = None  # создаётся в main()

# Состояние диалога с админом (какой ввод мы сейчас ждём)
S: dict = {}


class Run:
    """Состояние текущей рассылки."""
    task = None
    stop = False
    ok = 0
    fail = 0
    current = ""
    started = 0.0
    errors: list = []


# ───────────────────────────── ТЕКСТ ШАБЛОНОВ ─────────────────────────────
SPIN = re.compile(r"\{([^{}]*\|[^{}]*)\}")


def render(text, title):
    """Подставляет {title} и раскрывает спинтакс {a|b|c}."""
    text = text.replace("{title}", title or "")
    while SPIN.search(text):
        text = SPIN.sub(lambda m: random.choice(m.group(1).split("|")), text)
    return text


def short(text, n=25):
    one = " ".join(text.split())
    return one if len(one) <= n else one[: n - 1] + "…"


# ───────────────────────────── РАССЫЛКА ─────────────────────────────
async def nap(seconds):
    """Сон, который можно прервать кнопкой «Стоп»."""
    end = time.time() + seconds
    while time.time() < end and not Run.stop:
        await asyncio.sleep(1)


async def send_one(chat, text):
    """Отправка одного сообщения с обработкой типичных ошибок."""
    for _ in range(2):
        try:
            await user.send_message(chat["id"], text, link_preview=False)
            Run.ok += 1
            return
        except errors.FloodWaitError as e:
            if e.seconds > 600:  # слишком долго — безопаснее остановиться
                Run.fail += 1
                Run.errors.append(f"{chat['title']}: FloodWait {e.seconds}с — рассылка остановлена")
                Run.stop = True
                return
            log.warning("FloodWait %s сек", e.seconds)
            await nap(e.seconds + 3)
        except errors.SlowModeWaitError as e:
            Run.fail += 1
            Run.errors.append(f"{chat['title']}: слоумод {e.seconds}с, пропуск")
            return
        except (errors.ChatWriteForbiddenError, errors.UserBannedInChannelError,
                errors.ChannelPrivateError) as e:
            Run.fail += 1
            Run.errors.append(f"{chat['title']}: {type(e).__name__}")
            return
        except Exception as e:  # noqa
            Run.fail += 1
            Run.errors.append(f"{chat['title']}: {type(e).__name__}: {e}")
            return
    Run.fail += 1


async def mailing_job(notify_id):
    Run.stop = False
    Run.ok = Run.fail = 0
    Run.errors = []
    Run.started = time.time()
    try:
        # Прогреваем кэш диалогов, чтобы Telethon знал access_hash всех чатов
        await user.get_dialogs(limit=None)
        while not Run.stop:
            chats = q("SELECT * FROM chats")
            tpls = q("SELECT * FROM templates")
            if not chats or not tpls:
                await bot.send_message(notify_id, "⚠️ Нужен хотя бы один чат и один шаблон.")
                return
            for c in chats:
                if Run.stop:
                    break
                tpl = None
                if c["template_id"]:
                    tpl = q("SELECT * FROM templates WHERE id=?", (c["template_id"],), one=True)
                if not tpl:
                    tpl = random.choice(tpls)  # у чата нет своего шаблона — случайный
                Run.current = c["title"] or str(c["id"])
                await send_one(c, render(tpl["text"], c["title"]))
                dmin, dmax = float(get_setting("delay_min")), float(get_setting("delay_max"))
                await nap(random.uniform(dmin, max(dmin, dmax)))

            loop = int(get_setting("loop_min"))
            if Run.stop or loop <= 0:
                break
            Run.current = f"ожидание {loop} мин до следующего круга"
            await nap(loop * 60)
    except Exception as e:  # noqa
        log.exception("Ошибка рассылки")
        Run.errors.append(f"Критическая ошибка: {e}")
    finally:
        Run.current = ""
        Run.task = None
        await bot.send_message(notify_id, "🏁 Рассылка завершена.\n\n" + status_text())


def status_text():
    running = Run.task is not None
    lines = [
        f"Статус: {'▶️ идёт' if running else '⏹ не запущена'}",
        f"Отправлено: {Run.ok}   Ошибок: {Run.fail}",
    ]
    if running:
        lines.append(f"Сейчас: {Run.current or '—'}")
        lines.append(f"Работает: {int((time.time() - Run.started) // 60)} мин")
    if Run.errors:
        lines.append("\nПоследние ошибки:")
        lines += [f"• {x}" for x in Run.errors[-5:]]
    return "\n".join(lines)


# ───────────────────────────── ДОБАВЛЕНИЕ ЧАТОВ ─────────────────────────────
async def add_chats(lines):
    added, failed = [], []
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        s = re.sub(r"^(https?://)?(t\.me|telegram\.me)/", "", s).lstrip("@")
        if s.startswith("+") or s.startswith("joinchat"):
            failed.append(f"{raw} — по инвайт-ссылке нельзя, вступи в чат и используй «Импорт групп»")
            continue
        key = int(s) if re.fullmatch(r"-?\d+", s) else s
        try:
            ent = await user.get_entity(key)
            cid = utils.get_peer_id(ent)
            title = getattr(ent, "title", None) or getattr(ent, "first_name", None) or s
            q("INSERT OR IGNORE INTO chats(id,title) VALUES(?,?)", (cid, title))
            added.append(title)
        except Exception as e:  # noqa
            failed.append(f"{raw} — {type(e).__name__}")
        await asyncio.sleep(1)  # не долбим API
    return added, failed


async def import_groups():
    n = 0
    async for d in user.iter_dialogs():
        if d.is_group:
            q("INSERT OR IGNORE INTO chats(id,title) VALUES(?,?)", (d.id, d.name))
            n += 1
    return n


# ───────────────────────────── ИНТЕРФЕЙС ─────────────────────────────
def B(text, data):
    return Button.inline(text, data.encode())


def kb_main():
    return [
        [B("📝 Шаблоны", "tpl:list"), B("💬 Чаты", "chat:list:0")],
        [B("⚙️ Настройки", "set:menu"), B("🔑 Аккаунт", "acc:menu")],
        [B("▶️ Запустить", "run:start"), B("⏹ Стоп", "run:stop")],
        [B("📊 Статус", "run:status")],
    ]


BACK = [[B("◀️ Меню", "menu")]]


async def show(e, text, buttons=None):
    """Редактирует сообщение (если это нажатие кнопки) или отвечает новым."""
    if isinstance(e, events.CallbackQuery.Event):
        try:
            await e.edit(text, buttons=buttons, parse_mode=None)
        except errors.MessageNotModifiedError:
            pass
    else:
        await e.respond(text, buttons=buttons, parse_mode=None)


async def show_menu(e):
    n_t = len(q("SELECT id FROM templates"))
    n_c = len(q("SELECT id FROM chats"))
    await show(e, f"Панель рассылки\nШаблонов: {n_t}   Чатов: {n_c}", kb_main())


async def show_tpl_list(e):
    rows = q("SELECT * FROM templates ORDER BY id")
    btns = [[B(f"#{r['id']} {r['name']}", f"tpl:view:{r['id']}")] for r in rows[:40]]
    btns.append([B("➕ Добавить шаблон", "tpl:add")])
    btns.append([B("◀️ Меню", "menu")])
    await show(e, "📝 Шаблоны (случайный из них берётся для чатов без своего шаблона)", btns)


async def show_chat_list(e, page):
    rows = q(
        "SELECT c.*, t.name AS tname FROM chats c LEFT JOIN templates t ON t.id=c.template_id "
        "ORDER BY c.title"
    )
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    btns = []
    for r in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        label = f"{short(r['title'] or str(r['id']), 28)} · {('#' + str(r['template_id'])) if r['template_id'] else '🎲'}"
        btns.append([B(label, f"chat:view:{r['id']}")])
    nav = []
    if page > 0:
        nav.append(B("⬅️", f"chat:list:{page - 1}"))
    nav.append(B(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(B("➡️", f"chat:list:{page + 1}"))
    btns.append(nav)
    btns.append([B("➕ Добавить", "chat:add"), B("📥 Импорт групп", "chat:import")])
    btns.append([B("🗑 Очистить список", "chat:clear"), B("◀️ Меню", "menu")])
    await show(e, f"💬 Чаты: {len(rows)}\n🎲 — случайный шаблон, #N — закреплённый шаблон", btns)


async def show_settings(e):
    text = (
        "⚙️ Настройки\n"
        f"Пауза между сообщениями: {get_setting('delay_min')}–{get_setting('delay_max')} сек\n"
        f"Повтор круга: {'каждые ' + get_setting('loop_min') + ' мин' if get_setting('loop_min') != '0' else 'выключен (один проход)'}"
    )
    btns = [
        [B("Мин. пауза", "set:dmin"), B("Макс. пауза", "set:dmax")],
        [B("Повтор круга (мин)", "set:loop")],
        [B("◀️ Меню", "menu")],
    ]
    await show(e, text, btns)


async def show_account(e):
    if await user.is_user_authorized():
        me = await user.get_me()
        text = f"🔑 Аккаунт: {me.first_name} (@{me.username or '—'}), id {me.id}"
        btns = [[B("🚪 Выйти", "acc:logout")], [B("◀️ Меню", "menu")]]
    else:
        text = "🔑 Аккаунт не авторизован."
        btns = [[B("📲 Войти", "acc:login")], [B("◀️ Меню", "menu")]]
    await show(e, text, btns)


# ───────────────────────────── КНОПКИ ─────────────────────────────
@bot.on(events.CallbackQuery)
async def on_callback(e):
    if e.sender_id not in ADMIN_IDS:
        return await e.answer("⛔ Нет доступа", alert=True)
    d = e.data.decode().split(":")
    uid = e.sender_id
    S.pop(uid, None)  # любая кнопка отменяет ожидание ввода
    a, b = d[0], (d[1] if len(d) > 1 else "")

    if a == "noop":
        return await e.answer()
    if a == "menu":
        return await show_menu(e)

    # ── шаблоны ──
    if a == "tpl":
        if b == "list":
            return await show_tpl_list(e)
        if b == "add":
            S[uid] = {"step": "tpl_add"}
            return await show(e, "Пришли текст шаблона одним сообщением.\n\n"
                                 "Подсказки: {привет|здравствуйте} — случайный вариант, {title} — название чата.",
                              [[B("Отмена", "tpl:list")]])
        tid = int(d[2])
        if b == "view":
            r = q("SELECT * FROM templates WHERE id=?", (tid,), one=True)
            if not r:
                return await show_tpl_list(e)
            sample = render(r["text"], "Пример чата")
            return await show(e, f"Шаблон #{tid}\n\n{r['text']}\n\n— — —\nПример после обработки:\n{sample}",
                              [[B("✏️ Изменить", f"tpl:edit:{tid}"), B("🗑 Удалить", f"tpl:del:{tid}")],
                               [B("◀️ К шаблонам", "tpl:list")]])
        if b == "edit":
            S[uid] = {"step": "tpl_edit", "id": tid}
            return await show(e, "Пришли новый текст шаблона.", [[B("Отмена", f"tpl:view:{tid}")]])
        if b == "del":
            return await show(e, f"Удалить шаблон #{tid}?",
                              [[B("Да, удалить", f"tpl:delok:{tid}"), B("Нет", f"tpl:view:{tid}")]])
        if b == "delok":
            q("DELETE FROM templates WHERE id=?", (tid,))
            q("UPDATE chats SET template_id=NULL WHERE template_id=?", (tid,))
            return await show_tpl_list(e)

    # ── чаты ──
    if a == "chat":
        if b == "list":
            return await show_chat_list(e, int(d[2]))
        if b == "add":
            S[uid] = {"step": "chats_add"}
            return await show(e, "Пришли чаты — по одному в строке:\n@username, t.me/username или числовой ID.\n"
                                 "Аккаунт должен быть участником чата.",
                              [[B("Отмена", "chat:list:0")]])
        if b == "import":
            await e.answer("Импортирую…")
            n = await import_groups()
            return await show(e, f"Импортировано групп: {n}. Лишние удали из списка вручную.",
                              [[B("К чатам", "chat:list:0")]])
        if b == "clear":
            return await show(e, "Очистить весь список чатов?",
                              [[B("Да", "chat:clearok"), B("Нет", "chat:list:0")]])
        if b == "clearok":
            q("DELETE FROM chats")
            return await show_chat_list(e, 0)
        cid = int(d[2])
        if b == "view":
            r = q("SELECT c.*, t.name AS tname FROM chats c LEFT JOIN templates t ON t.id=c.template_id "
                  "WHERE c.id=?", (cid,), one=True)
            if not r:
                return await show_chat_list(e, 0)
            tpl = f"#{r['template_id']} {r['tname']}" if r["template_id"] else "случайный из всех 🎲"
            return await show(e, f"Чат: {r['title']}\nID: {cid}\nШаблон: {tpl}",
                              [[B("📝 Выбрать шаблон", f"chat:tpl:{cid}")],
                               [B("🗑 Убрать из списка", f"chat:del:{cid}")],
                               [B("◀️ К чатам", "chat:list:0")]])
        if b == "tpl":
            rows = q("SELECT * FROM templates ORDER BY id")
            btns = [[B("🎲 Случайный", f"chat:set:{cid}:0")]]
            btns += [[B(f"#{r['id']} {r['name']}", f"chat:set:{cid}:{r['id']}")] for r in rows[:40]]
            btns.append([B("◀️ Назад", f"chat:view:{cid}")])
            return await show(e, "Какой шаблон использовать для этого чата?", btns)
        if b == "set":
            tid = int(d[3])
            q("UPDATE chats SET template_id=? WHERE id=?", (tid or None, cid))
            e.data = f"chat:view:{cid}".encode()
            return await on_callback(e)
        if b == "del":
            q("DELETE FROM chats WHERE id=?", (cid,))
            return await show_chat_list(e, 0)

    # ── настройки ──
    if a == "set":
        if b == "menu":
            return await show_settings(e)
        names = {"dmin": "минимальную паузу между сообщениями (сек)",
                 "dmax": "максимальную паузу между сообщениями (сек)",
                 "loop": "интервал повтора круга в минутах (0 = без повтора)"}
        S[uid] = {"step": "set_" + b}
        return await show(e, f"Пришли число: {names[b]}.", [[B("Отмена", "set:menu")]])

    # ── аккаунт ──
    if a == "acc":
        if b == "menu":
            return await show_account(e)
        if b == "login":
            S[uid] = {"step": "login_phone"}
            return await show(e, "Пришли номер телефона аккаунта в формате +79991234567.",
                              [[B("Отмена", "acc:menu")]])
        if b == "logout":
            global user
            try:
                await user.log_out()
            except Exception:  # noqa
                pass
            set_setting("session", "")
            user = TelegramClient(StringSession(), API_ID, API_HASH)
            await user.connect()
            return await show_account(e)

    # ── запуск ──
    if a == "run":
        if b == "start":
            if Run.task:
                return await e.answer("Уже запущена", alert=True)
            if not await user.is_user_authorized():
                return await e.answer("Сначала войди в аккаунт (🔑 Аккаунт)", alert=True)
            Run.task = asyncio.create_task(mailing_job(uid))
            return await show(e, "▶️ Рассылка запущена.", [[B("📊 Статус", "run:status"), B("⏹ Стоп", "run:stop")]])
        if b == "stop":
            if not Run.task:
                return await e.answer("Не запущена", alert=True)
            Run.stop = True
            return await e.answer("Останавливаю после текущей отправки…", alert=True)
        if b == "status":
            return await show(e, status_text(), [[B("🔄 Обновить", "run:status"), B("◀️ Меню", "menu")]])


# ───────────────────────────── ТЕКСТОВЫЙ ВВОД ─────────────────────────────
@bot.on(events.NewMessage(func=lambda ev: ev.is_private))
async def on_message(e):
    global user
    uid = e.sender_id
    if uid not in ADMIN_IDS:
        return
    raw = e.raw_text or ""
    if raw.startswith("/start") or raw.startswith("/menu"):
        S.pop(uid, None)
        return await show_menu(e)

    st = S.get(uid)
    if not st:
        return await show_menu(e)
    step = st["step"]

    # шаблоны (e.text сохраняет форматирование Telegram в виде markdown)
    if step == "tpl_add":
        S.pop(uid)
        cur = q("INSERT INTO templates(name,text) VALUES(?,?)", (short(raw), e.text))
        return await show_tpl_list(e)
    if step == "tpl_edit":
        S.pop(uid)
        q("UPDATE templates SET name=?, text=? WHERE id=?", (short(raw), e.text, st["id"]))
        return await show_tpl_list(e)

    # чаты
    if step == "chats_add":
        S.pop(uid)
        msg = await e.respond("Добавляю…")
        added, failed = await add_chats(raw.splitlines())
        text = f"Добавлено: {len(added)}"
        if failed:
            text += "\n\nНе удалось:\n" + "\n".join(failed[:15])
        await msg.edit(text, buttons=[[B("К чатам", "chat:list:0")]])
        return

    # настройки
    if step.startswith("set_"):
        if not raw.strip().isdigit():
            return await e.respond("Нужно целое число. Попробуй ещё раз.")
        key = {"set_dmin": "delay_min", "set_dmax": "delay_max", "set_loop": "loop_min"}[step]
        set_setting(key, int(raw.strip()))
        S.pop(uid)
        return await show_settings(e)

    # вход в аккаунт
    if step == "login_phone":
        phone = raw.strip()
        try:
            sent = await user.send_code_request(phone)
        except Exception as ex:  # noqa
            S.pop(uid)
            return await e.respond(f"Не удалось отправить код: {type(ex).__name__}", buttons=BACK)
        S[uid] = {"step": "login_code", "phone": phone, "hash": sent.phone_code_hash}
        return await e.respond(
            "Код отправлен в Telegram.\n\nВведи его ЧЕРЕЗ ДЕФИСЫ, например 1-2-3-4-5 — "
            "иначе Telegram может счесть код скомпрометированным и отменить вход."
        )
    if step == "login_code":
        code = re.sub(r"\D", "", raw)
        try:
            await user.sign_in(phone=st["phone"], code=code, phone_code_hash=st["hash"])
        except errors.SessionPasswordNeededError:
            S[uid] = {"step": "login_pw"}
            return await e.respond("Включена двухфакторка. Пришли облачный пароль (сообщение я удалю).")
        except (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError):
            S.pop(uid)
            return await e.respond("Код неверный или просрочен. Начни вход заново.", buttons=BACK)
        except Exception as ex:  # noqa
            S.pop(uid)
            return await e.respond(f"Ошибка входа: {type(ex).__name__}", buttons=BACK)
        S.pop(uid)
        set_setting("session", user.session.save())
        return await e.respond("✅ Вход выполнен.", buttons=BACK)
    if step == "login_pw":
        S.pop(uid)
        try:
            await user.sign_in(password=raw)
        except Exception as ex:  # noqa
            return await e.respond(f"Ошибка пароля: {type(ex).__name__}", buttons=BACK)
        finally:
            try:
                await e.delete()
            except Exception:  # noqa
                pass
        set_setting("session", user.session.save())
        return await e.respond("✅ Вход выполнен.", buttons=BACK)


# ───────────────────────────── ЗАПУСК ─────────────────────────────
async def main():
    global user
    if not (API_ID and API_HASH and BOT_TOKEN and ADMIN_IDS):
        raise SystemExit("Заполни API_ID, API_HASH, BOT_TOKEN и ADMIN_IDS (см. шапку файла).")
    user = TelegramClient(StringSession(get_setting("session")), API_ID, API_HASH)
    await user.connect()
    await bot.start(bot_token=BOT_TOKEN)
    log.info("Панель запущена. Аккаунт авторизован: %s", await user.is_user_authorized())
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
