import asyncio
import time
from contextlib import asynccontextmanager

import aiosqlite
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from config import BOT_TOKEN, WEBAPP_URL, BOT_SECRET, HOST, PORT, DB_PATH


REACTIONS = ("support", "hug", "sad")


# ---------------- DATABASE ----------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS posts(
            id INTEGER PRIMARY KEY,
            text TEXT NOT NULL,
            ts INTEGER NOT NULL
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS reactions(
            post_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(post_id, type)
        )
        """)
        await db.commit()


async def add_post(text: str) -> int:
    pid = int(time.time() * 1000)
    ts = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO posts(id, text, ts) VALUES(?,?,?)", (pid, text, ts))
        for t in REACTIONS:
            await db.execute(
                "INSERT OR IGNORE INTO reactions(post_id, type, count) VALUES(?,?,0)",
                (pid, t)
            )
        await db.commit()
    return pid


async def get_feed(cursor: int | None, limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        if cursor:
            rows = await db.execute_fetchall(
                "SELECT id, text, ts FROM posts WHERE id < ? ORDER BY id DESC LIMIT ?",
                (cursor, limit)
            )
        else:
            rows = await db.execute_fetchall(
                "SELECT id, text, ts FROM posts ORDER BY id DESC LIMIT ?",
                (limit,)
            )

        out: list[dict] = []
        for pid, text, ts in rows:
            rrows = await db.execute_fetchall(
                "SELECT type, count FROM reactions WHERE post_id = ?",
                (pid,)
            )
            out.append({
                "id": pid,
                "text": text,
                "ts": ts,
                "reactions": {t: c for t, c in rrows}
            })
        return out


async def inc_reaction(post_id: int, rtype: str):
    if rtype not in REACTIONS:
        raise ValueError("bad reaction")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE reactions SET count = count + 1 WHERE post_id = ? AND type = ?",
            (post_id, rtype)
        )
        await db.commit()


# ---------------- API (lifespan вместо on_event) ----------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


api = FastAPI(title="ShutApp API", lifespan=lifespan)

# CORS: чтобы GitHub Pages (egorka47.github.io) мог делать fetch к API
api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # для MVP так. Потом сузим до твоего домена Pages
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class PostIn(BaseModel):
    text: str


class ReactIn(BaseModel):
    type: str


@api.get("/health")
async def health():
    return {"ok": True}


@api.get("/feed")
async def api_feed(cursor: int | None = None, limit: int = 20):
    return await get_feed(cursor=cursor, limit=min(limit, 50))


@api.post("/bot/post")
async def api_bot_post(data: PostIn, x_bot_secret: str | None = Header(default=None)):
    if x_bot_secret != BOT_SECRET:
        raise HTTPException(401, "bad secret")
    text = data.text.strip()
    if not text:
        raise HTTPException(400, "empty")
    pid = await add_post(text)
    return {"ok": True, "id": pid}


@api.post("/posts/{post_id}/react")
async def api_react(post_id: int, data: ReactIn):
    try:
        await inc_reaction(post_id, data.type)
    except ValueError:
        raise HTTPException(400, "bad reaction")
    return {"ok": True}


# ---------------- BOT ----------------
bot = Bot(BOT_TOKEN)
dp = Dispatcher()


class NewPost(StatesGroup):
    text = State()


def open_app_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖤 Открыть ShutApp", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])


@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "ShutApp — место, где можно выговориться.\n\n"
        "Пиши посты здесь в личке.\n"
        "Лента и реакции — в приложении.",
        reply_markup=open_app_kb(),
    )


@dp.message(F.text == "/newpost")
async def newpost(m: Message, state: FSMContext):
    if m.chat.type != "private":
        await m.answer("Посты можно писать только в личке.")
        return
    await state.set_state(NewPost.text)
    await m.answer("Напиши текст поста одним сообщением.")


@dp.message(NewPost.text)
async def save_post(m: Message, state: FSMContext):
    text = (m.text or "").strip()
    if not text:
        await m.answer("Пусто.")
        return

    await add_post(text)
    await state.clear()
    await m.answer("✅ Пост опубликован.", reply_markup=open_app_kb())


@dp.message()
async def fallback(m: Message):
    await m.answer("Чтобы написать пост: /newpost", reply_markup=open_app_kb())


# ---------------- RUN BOTH ----------------
async def run():
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(api, host=HOST, port=PORT, log_level="info"))
    await asyncio.gather(
        server.serve(),
        dp.start_polling(bot),
    )


if __name__ == "__main__":
    asyncio.run(run())
