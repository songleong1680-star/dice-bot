import logging
import random
import sqlite3
import time
import asyncio
from functools import partial
from datetime import datetime
from collections import deque

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler, 
    filters
)

# ======================
# Game 5 Dynamic Cooldown
# ======================
BASE_COOLDOWN = 2.0      # 低峰最小冷却（秒）
MAX_COOLDOWN = 5.0       # 高峰最大冷却（秒）
WINDOW_SECONDS = 10      # 统计窗口（最近 10 秒）
HISTORY_LIMIT = 5        # 只看最近 5 次

# 每个 chat 的 Game5 历史时间
last_game5_time = {}     # (chat_id, user_id) -> time
game5_history = {}       # (chat_id, user_id) -> deque

def get_dynamic_cooldown(key) -> float:
    now = time.time()
    history = game5_history.get(key)

    if history is None:
        history = deque(maxlen=HISTORY_LIMIT)
        game5_history[key] = history

    # 清理窗口外的记录
    while history and now - history[0] > WINDOW_SECONDS:
        history.popleft()

    active = len(history)  # 窗口内的 Game5 次数
    cooldown = BASE_COOLDOWN + active * 0.5
    return min(cooldown, MAX_COOLDOWN)

# ======================
# CONFIG
# ======================
BOT_TOKEN = "8572610086:AAEiqih9FHr6705JjWz2D3jYGJQ8VdLDRGo"

BANKER_IDS = {5571909470, 6655863309, 6983854916, 7967425773}
ADMIN_CONTACT = "https://t.me/dice918admin"

BASE_BANKER_WIN_RATE = 0.20
LOSE_PROTECT_TRIGGER = 3
CONFIRM_TIMEOUT = 30

# Game 4｜猜点数倍率
GUESS_NUMBER_WIN_MULTIPLIER = 5

# Game 5｜稳定骰
STABLE_DICE_WIN_MULTIPLIER = 0.4

DB_FILE = "dicebot.db"

logging.basicConfig(level=logging.INFO)

# ======================
# DATABASE
# ======================
conn = sqlite3.connect(DB_FILE, check_same_thread=False)

# ✅ WAL 模式（你原本的）
conn.execute("PRAGMA journal_mode=WAL;")

cur = conn.cursor()

# ===== users 表（改这里）=====
conn = sqlite3.connect(DB_FILE, check_same_thread=False)

# WAL 模式
conn.execute("PRAGMA journal_mode=WAL;")

cur = conn.cursor()

# ===== users =====
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER,
    chat_id INTEGER,
    language TEXT,
    balance REAL DEFAULT 0,
    current_game INTEGER,
    lose_streak INTEGER DEFAULT 0,
    win INTEGER DEFAULT 0,
    lose INTEGER DEFAULT 0,
    draw INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, chat_id)
)
""")

# ===== pending_admin（🔥关键修复）=====
cur.execute("""
CREATE TABLE IF NOT EXISTS pending_admin (
    id INTEGER PRIMARY KEY AUTOINCREMENT,   -- ⭐ 新增ID
    admin_id INTEGER,                       -- ⭐ 不再是主键
    chat_id INTEGER,
    target_user INTEGER,
    action TEXT,
    amount REAL,
    expire_at REAL
)
""")

# ===== banker_ledger =====
cur.execute("""
CREATE TABLE IF NOT EXISTS banker_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    admin_id INTEGER,
    action TEXT,
    amount REAL,
    created_at REAL
)
""")

conn.commit()

# ======================
# LANGUAGE TEXT
# ======================
TEXT = {
    "cn": {
        "choose_lang": "请选择语言：",
        "rules": (
            "🎲 游戏规则\n\n"
            "游戏1｜比大小\n"
            "- 玩家掷 1 颗骰子\n"
            "- 庄家掷 1 颗骰子\n"
            "- 点数大者胜，平局退回下注\n\n"
            "游戏2｜大小\n"
            "- 玩家下注并选择 big / small\n"
            "- 1–3 小，4–6 大\n"
            "- 庄家掷骰子决定结果\n\n"
            "游戏3｜单 / 双\n"
            "- 玩家下注并选择 odd / even\n"
            "- 1,3,5 单｜2,4,6 双\n"
            "- 庄家掷骰子决定结果\n\n"
            "- 游戏4｜猜点数(1-6)\n"
            "- 从1-6中选择一个点数\n"
            "- 猜中:赢5倍,猜错:输1倍\n"
            "- 庄家掷骰子决定结果"
        ),
        "select_game": "🎮 请选择游戏：",
        "rule_g1": (
              "🎲 游戏 1｜比大小\n\n"
              "玩家掷 1 颗骰子\n"
              "庄家掷 1 颗骰子\n"
              "点数大者胜\n"
              "平局退回下注"
        ),
        "rule_g2": (
              "🎲 游戏 2｜大小\n\n"
              "选择 大 或 小\n"
              "1–3 小，4–6 大\n"
              "猜中赢 1 倍"
        ),
        "rule_g3": (
              "🎲 游戏 3｜单 / 双\n\n"
              "选择 单 或 双\n"
              "1,3,5 单｜2,4,6 双\n"
              "猜中赢 1 倍"
        ),
        "rule_g4": (
              "🎯 游戏 4｜猜点数\n\n"
              "从 1–6 选择一个点数\n"
              "猜中赢 5 倍\n"
              "猜错输 1 倍"
        ),
        "rule_g5": (
              "🎲 游戏 5｜稳定骰\n\n"
              "庄家掷 1 颗骰子\n"
              "点数 1–4：玩家赢 0.4 倍\n"
              "点数 5–6：玩家输 1 倍"
        ),
        "g5": (
              "🎲 游戏 5｜稳定骰\n\n"
              "👉 点击按钮下注金额"
        ),
        "g1": (
              "🎲 游戏1｜比大小\n\n"
              "👉 点击按钮下注金额"
        ),
        "g2": (
              "🎲 游戏2｜大小\n\n"
              "👉 点击按钮下注金额"
        ),
        "g3": (
              "🎲 游戏3｜单 / 双\n\n"
              "👉 点击按钮下注金额"
        ),
        "g4": "🎯 游戏4｜猜点数\n\n请选择下注金额：",
        "need_game": "❗ 请先选择游戏：/game",
        "bet_ack": "✅ 已下注",
        "cooldown_wait": "⏳ 请稍候再试",
        "g5_limit": "❌ Game 5 单局下注不能超过 100",
        "start_game_btn": "▶ 开始游戏",
        "no_balance_tip": "❌ 当前余额为 0\n请联系客服充值或咨询",
        "back_btn": "⬅ 返回",
        "contact_admin_btn": "📞 联系管理员",
        "custom_amount_btn": "💎 Custom Bet",
        "bet_prompt": "💰 请选择下注金额：",
        "enter_custom_amount": "✏️ 请输入下注金额：",
        "invalid_amount": "❌ 请输入正确的数字金额",
        "guess_title": "🎯 请选择一个点数：",
        "your_choice": "你的选择",
        "dice_result_title": "🎲 骰子结果",
        "banker_label": "庄家",
        "roll_btn": "🎲 Roll",
        "big_btn": "⬆️ 大",
        "small_btn": "⬇️ 小",
        "odd_btn": "🔵 单",
        "even_btn": "⚪ 双",
        "bet_locked": "✅ 已下注 {amt}\n👉 点击按钮 Roll",
        "player_label": "玩家",
        "bet_locked_no_roll": "✅ 已下注 {amt}",
        "bet_locked_auto": "✅ 已下注 {amt}\n正在掷骰子…",
        "balance_label": "余额",
        "no_balance": "❌ 余额不足",
        "roll_first": "❗ 请先下注",
        "result": (
            "🎲 骰子结果\n"
            "玩家：{p}\n"
            "庄家：{b}\n\n"
            "{res}\n"
            "{delta}\n"
            "💰 当前余额：{bal}"
        ),
        "win": "🏆 玩家胜利",
        "lose": "❌ 玩家失败",
        "draw": "🤝 平局，退回下注",
        "next": "👉 下一局",
        "confirm": "⚠️ 请在 30 秒内输入 /confirm 或 /cancel",
        "grant_ok": "✅ 已上分 {amt}\n💰 当前余额：{bal}",
        "deduct_ok": "✅ 已下分 {amt}\n💰 当前余额：{bal}",
        "stats": (
            "📊 我的战绩\n"
            "💰 当前余额：{bal}\n"
            "🎮 总局数：{total}\n"
            "🏆 赢：{w}\n"
            "❌ 输：{l}\n"
            "🤝 平：{d}\n"
            "🔥 连输：{ls}"
        ),
    },
    "en": {
        "choose_lang": "Choose language:",
        "rules": (
            "🎲 Game Rules\n\n"
            "Game 1｜High Dice\n"
            "- Player rolls 1 dice\n"
            "- Banker rolls 1 dice\n"
            "- Higher wins, draw refunded\n\n"
            "Game 2｜Big / Small\n"
            "- Player bets and chooses\n"
            "- 1–3 Small, 4–6 Big\n\n"
            "Game 3｜Odd / Even\n"
            "- Player bets and chooses\n"
            "- 1,3,5 Odd | 2,4,6 Even\n\n"
            "- Game 4｜Guess the Number(1-6)\n"
            "- Player bets and chooses\n"
            "- Number from 1 to 6\n"
            "- Guess correctly: Win 5 times your bet\n"
            "- Guess incorrectly: Lose 1 time your bet"
        ),
        "select_game": "🎮 Choose game:",
        "rule_g1": (
              "🎲 Game 1｜High Dice\n\n"
              "Player rolls 1 dice\n"
              "Banker rolls 1 dice\n"
              "Higher number wins\n"
              "Draw = refund"
        ),
        "rule_g2": (
              "🎲 Game 2｜Big / Small\n\n"
              "Choose Big or Small\n"
              "1–3 Small, 4–6 Big\n"
              "Correct guess wins 1x"
        ),
        "rule_g3": (
              "🎲 Game 3｜Odd / Even\n\n"
              "Choose Odd or Even\n"
              "1,3,5 Odd | 2,4,6 Even\n"
              "Correct guess wins 1x"
        ),
        "rule_g4": (
              "🎯 Game 4｜Guess Number\n\n"
              "Choose a number from 1–6\n"
              "Correct guess wins 5x\n"
              "Wrong guess loses 1x"
        ),
        "rule_g5": (
              "🎲 Game 5｜Stable Dice\n\n"
              "Banker rolls 1 dice\n"
              "1–4: Player wins 0.4x\n"
              "5–6: Player loses 1x"
        ),
        "g5": (
              "🎲 Game 5｜Stable Dice\n\n"
              "👉 Tap a bet amount button"
        ),
        "g1": (
              "🎲 Game 1｜High Dice\n\n"
              "👉 Tap a bet amount button"
        ),
        "g2": (
              "🎲 Game 2｜Big / Small\n\n"
              "👉 Tap a bet amount button"
        ),
        "g3": (
              "🎲 Game 3｜Odd / Even\n\n"
              "👉 Tap a bet amount button"
        ),
        "g4": "🎯 Game 4｜Guess Number\n\nPlease choose bet amount:",
        "need_game": "❗ Please select game first: /game",
        "bet_ack": "✅ Bet placed",
        "cooldown_wait": "⏳ Please wait a moment and try again",
        "g5_limit": "❌ Game 5 bet cannot exceed 100",
        "start_game_btn": "▶ Start Game",
        "no_balance_tip": "❌ Your balance is 0\nPlease contact admin to top up or enquire",
        "back_btn": "⬅ Back",
        "contact_admin_btn": "📞 Contact Admin",
        "custom_amount_btn": "💎 Custom Bet",
        "bet_prompt": "💰 Please choose bet amount:",
        "enter_custom_amount": "✏️ Please enter bet amount:",
        "invalid_amount": "❌ Please enter a valid amount",
        "guess_title": "🎯 Please choose a number:",
        "your_choice": "Your Choice",
        "dice_result_title": "🎲 Dice Result",
        "player_label": "Player",
        "banker_label": "Banker",
        "roll_btn": "🎲 Roll",
        "big_btn": "⬆️ Big",
        "small_btn": "⬇️ Small",
        "odd_btn": "🔵 Odd",
        "even_btn": "⚪ Even",
        "bet_locked": "✅ Bet locked {amt}\n👉 Tap Roll button",
        "bet_locked_no_roll": "✅ Bet {amt}",
        "bet_locked_auto": "✅ Bet locked {amt}\nRolling dice…",
        "balance_label": "Balance",
        "no_balance": "❌ Insufficient balance",
        "roll_first": "❗ Please bet first",
        "result": (
            "🎲 Dice Result\n"
            "Player: {p}\n"
            "Banker: {b}\n\n"
            "{res}\n"
            "{delta}\n"
            "💰 Balance: {bal}"
        ),
        "win": "🏆 You Win",
        "lose": "❌ You Lose",
        "draw": "🤝 Draw, refunded",
        "next": "👉 Next round",
        "confirm": "⚠️ Confirm within 30 seconds: /confirm or /cancel",
        "grant_ok": "✅ Granted {amt}\n💰 Balance: {bal}",
        "deduct_ok": "✅ Deducted {amt}\n💰 Balance: {bal}",
        "stats": (
            "📊 My Stats\n"
            "💰 Balance: {bal}\n"
            "🎮 Total games: {total}\n"
            "🏆 Win: {w}\n"
            "❌ Lose: {l}\n"
            "🤝 Draw: {d}\n"
            "🔥 Lose streak: {ls}"
        ),
    },
    "bm": {
        "choose_lang": "Pilih bahasa:",
        "rules": (
            "🎲 Peraturan Permainan\n\n"
            "Game 1｜Besar\n"
            "- Pemain baling 1 dadu\n"
            "- Banker baling 1 dadu\n"
            "- Point tinggi menang, seri refund\n\n"
            "Game 2｜Besar / Kecil\n"
            "- 1–3 Kecil, 4–6 Besar\n"
            "- Player letak bet, pilih Big atau Small\n"
            "- Keputusan ikut balingan dadu\n\n"
            "Game 3｜Ganjil / Genap\n"
            "-Player letak bet, pilih Odd atau Even\n"
            "- 1,3,5 Ganjil | 2,4,6 Genap\n"
            "-Keputusan ikut balingan dadu\n\n"
            "- Game 4｜Teka Nombor(1-6)\n"
            "- Player letak bet, pilih Nombor daripada 1-6\n"
            "- Betul: Menang 5x, Salah: Kalah 1x"
            "- Keputusan ikut balingan dadu\n"
        ),
        "select_game": "🎮 Pilih permainan:",
        "rule_g1": (
              "🎲 Game 1｜Besar\n\n"
              "Player baling 1 dadu\n"
              "Banker baling 1 dadu\n"
              "Nombor lebih besar menang\n"
              "Seri = refund"
        ),
        "rule_g2": (
              "🎲 Game 2｜Besar / Kecil\n\n"
              "Pilih Besar atau Kecil\n"
              "1–3 Kecil, 4–6 Besar\n"
              "Teka betul menang 1x"
        ),
        "rule_g3": (
              "🎲 Game 3｜Ganjil / Genap\n\n"
              "Pilih Ganjil atau Genap\n"
              "1,3,5 Ganjil | 2,4,6 Genap\n"
              "Teka betul menang 1x"
        ),
        "rule_g4": (
              "🎯 Game 4｜Teka Nombor\n\n"
              "Pilih nombor dari 1–6\n"
              "Betul menang 5x\n"
              "Salah kalah 1x"
        ),
        "rule_g5": (
              "🎲 Game 5｜Dadu Stabil\n\n"
              "Banker baling 1 dadu\n"
              "1–4: Pemain menang 0.4x\n"
              "5–6: Pemain kalah 1x"
        ),
        "g5": (
              "🎲 Game 5｜Dadu Stabil\n\n"
              "👉 Tekan butang jumlah pertaruhan"
        ),
        "g1": (
              "🎲 Game 1｜Besar Kecil\n\n"
              "👉 Tekan butang jumlah"
        ),
        "g2": (
              "🎲 Game 2｜Besar / Kecil\n\n"
              "👉 Tekan butang jumlah"
        ),
        "g3": (
              "🎲 Game 3｜Ganjil / Genap\n\n"
              "👉 Tekan butang jumlah"
        ),
        "g4": "🎯 Game 4｜Teka Nombor\n\nSila pilih jumlah pertaruhan:",
        "need_game": "❗ Sila pilih permainan dahulu: /game",
        "bet_ack": "✅ Pertaruhan diterima",
        "cooldown_wait": "⏳ Sila tunggu sebentar dan cuba lagi",
        "g5_limit": "❌ Pertaruhan Game 5 tidak boleh melebihi 100",
        "start_game_btn": "▶ Mula Permainan",
        "no_balance_tip": "❌ Baki anda 0\nSila hubungi admin untuk tambah nilai atau pertanyaan",
        "back_btn": "⬅ Kembali",
        "contact_admin_btn": "📞 Hubungi Admin",
        "custom_amount_btn": "💎 Custom Bet",
        "bet_prompt": "💰 Sila pilih jumlah pertaruhan:",
        "enter_custom_amount": "✏️ Sila masukkan jumlah pertaruhan:",
        "invalid_amount": "❌ Sila masukkan nombor yang betul",
        "guess_title": "🎯 Sila pilih satu nombor:",
        "your_choice": "Pilihan Anda",
        "dice_result_title": "🎲 Keputusan Dadu",
        "player_label": "Pemain",
        "banker_label": "Banker",
        "roll_btn": "🎲 Roll",
        "big_btn": "⬆️ Besar",
        "small_btn": "⬇️ Kecil",
        "odd_btn": "🔵 Ganjil",
        "even_btn": "⚪ Genap",
        "bet_locked": "✅ Pertaruhan {amt}\n👉 Tekan butang Roll",
        "bet_locked_no_roll": "✅ Pertaruhan {amt}",
        "bet_locked_auto": "✅ Pertaruhan {amt}\nSedang baling dadu…",
        "balance_label": "Baki",
        "no_balance": "❌ Baki tidak cukup",
        "roll_first": "❗ Sila bertaruh dahulu",
        "result": (
            "🎲 Keputusan Dadu\n"
            "Pemain: {p}\n"
            "Banker: {b}\n\n"
            "{res}\n"
            "{delta}\n"
            "💰 Baki: {bal}"
        ),
        "win": "🏆 Menang",
        "lose": "❌ Kalah",
        "draw": "🤝 Seri",
        "next": "👉 Pusingan seterusnya",
        "confirm": "⚠️ Sahkan dalam 30 saat: /confirm atau /cancel",
        "grant_ok": "✅ Tambah {amt}\n💰 Baki: {bal}",
        "deduct_ok": "✅ Tolak {amt}\n💰 Baki: {bal}",
        "stats": (
            "📊 Statistik Saya\n"
            "💰 Baki: {bal}\n"
            "🎮 Jumlah permainan: {total}\n"
            "🏆 Menang: {w}\n"
            "❌ Kalah: {l}\n"
            "🤝 Seri: {d}\n"
            "🔥 Kalah berturut: {ls}"
        ),
    },
}

# ======================
# HELPERS
# ======================
def get_user(uid, cid):
    cur.execute(
        "SELECT language,balance,current_game,lose_streak,win,lose,draw FROM users WHERE user_id=? AND chat_id=?",
        (uid, cid),
    )
    return cur.fetchone()

def ensure_user(uid, cid, lang):
    if not get_user(uid, cid):
        cur.execute(
            "INSERT INTO users (user_id,chat_id,language) VALUES (?,?,?)",
            (uid, cid, lang),
        )
        conn.commit()

def update_balance_atomic(uid, cid, delta):
    cur.execute(
        "UPDATE users SET balance = balance + ? WHERE user_id=? AND chat_id=?",
        (delta, uid, cid)
    )
    conn.commit()

def get_balance(uid, cid):
    cur.execute(
        "SELECT balance FROM users WHERE user_id=? AND chat_id=?",
        (uid, cid)
    )
    row = cur.fetchone()
    return round(row[0], 2) if row else 0

def banker_bias(lose_streak):
    if lose_streak >= LOSE_PROTECT_TRIGGER:
        return BASE_BANKER_WIN_RATE - 0.15
    return BASE_BANKER_WIN_RATE

BET_AMOUNTS = [1,2,5,10,15,30,50,100,150,300,500]

def start_game_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 开始游戏", callback_data="open_game")]
    ])

def start_game_keyboard(lang: str):
    label_map = {
        "cn": "🎮 开始游戏",
        "en": "🎮 Start Game",
        "bm": "🎮 Mula Permainan",
    }
    label = label_map.get(lang, "🎮 Start Game")

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data="OPEN_GAME")]
    ])

def start_game_keyboard(lang: str):
    label_map = {
        "cn": "🎮 开始游戏",
        "en": "🎮 Start Game",
        "bm": "🎮 Mula Permainan",
    }
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label_map.get(lang, "🎮 Start Game"), callback_data="START_GAME")]
    ])

def roll_keyboard(lang):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(TEXT[lang]["roll_btn"], callback_data="rollbtn")]
    ])

def big_small_keyboard(lang):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(TEXT[lang]["big_btn"], callback_data="choice_big"),
            InlineKeyboardButton(TEXT[lang]["small_btn"], callback_data="choice_small"),
        ]
    ])

def odd_even_keyboard(lang):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(TEXT[lang]["odd_btn"], callback_data="choice_odd"),
            InlineKeyboardButton(TEXT[lang]["even_btn"], callback_data="choice_even"),
        ]
    ])

def guess_number_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1️⃣", callback_data="guess_1"),
                        InlineKeyboardButton("2️⃣", callback_data="guess_2"),
                        InlineKeyboardButton("3️⃣", callback_data="guess_3"),
                ],
                [
                       InlineKeyboardButton("4️⃣", callback_data="guess_4"),
                       InlineKeyboardButton("5️⃣", callback_data="guess_5"),
                       InlineKeyboardButton("6️⃣", callback_data="guess_6"),
                ]
            ])

def universal_bet_keyboard(game_id, lang):
    BET_AMOUNTS = [1, 5, 10, 20, 50, 100, 200, 500]

    kb = [
        [
            InlineKeyboardButton(f"💰 {BET_AMOUNTS[0]}", callback_data=f"betu_{game_id}_{BET_AMOUNTS[0]}"),
            InlineKeyboardButton(f"💰 {BET_AMOUNTS[1]}", callback_data=f"betu_{game_id}_{BET_AMOUNTS[1]}"),
            InlineKeyboardButton(f"💰 {BET_AMOUNTS[2]}", callback_data=f"betu_{game_id}_{BET_AMOUNTS[2]}"),
            InlineKeyboardButton(f"💰 {BET_AMOUNTS[3]}", callback_data=f"betu_{game_id}_{BET_AMOUNTS[3]}"),
        ],
        [
            InlineKeyboardButton(f"💰 {BET_AMOUNTS[4]}", callback_data=f"betu_{game_id}_{BET_AMOUNTS[4]}"),
            InlineKeyboardButton(f"💰 {BET_AMOUNTS[5]}", callback_data=f"betu_{game_id}_{BET_AMOUNTS[5]}"),
            InlineKeyboardButton(f"💰 {BET_AMOUNTS[6]}", callback_data=f"betu_{game_id}_{BET_AMOUNTS[6]}"),
            InlineKeyboardButton(f"💰 {BET_AMOUNTS[7]}", callback_data=f"betu_{game_id}_{BET_AMOUNTS[7]}"),
        ],
        [
            InlineKeyboardButton(
                TEXT[lang]["custom_amount_btn"],
                callback_data=f"betu_{game_id}_custom"
            )
        ]
    ]

    return InlineKeyboardMarkup(kb)

def bet_amount_keyboard_game5(lang: str):

    kb = [
        [
            InlineKeyboardButton("💰 1", callback_data="betu_5_1"),
            InlineKeyboardButton("💰 5", callback_data="betu_5_5"),
            InlineKeyboardButton("💰 10", callback_data="betu_5_10"),
        ],
        [
            InlineKeyboardButton("💰 20", callback_data="betu_5_20"),
            InlineKeyboardButton("💰 50", callback_data="betu_5_50"),
            InlineKeyboardButton("💰 100", callback_data="betu_5_100"),
        ],
        [
            InlineKeyboardButton(
                TEXT[lang]["custom_amount_btn"],
                callback_data="betu_5_custom"
            )
        ]
    ]

    return InlineKeyboardMarkup(kb)

async def game2_bet_button_v3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    amt = int(q.data.split("_")[1])

    uid = q.from_user.id
    cid = q.message.chat.id

    u = get_user(uid, cid)
    if not u:
        return

    lang, bal, *_ = u
    t = TEXT[lang]

    if bal < amt:
        await q.message.reply_text(t["no_balance"])
        return

    # ⭐ 强制设定 Game2
    context.user_data["bet"] = amt
    context.user_data["choice"] = None
    context.user_data["force_game"] = 2

    await q.message.reply_text(
        t["bet_locked_no_roll"].format(amt=amt),
        reply_markup=big_small_keyboard(lang)
    )

async def game2_entry_v3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id, update.effective_chat.id)
    if not u:
        return

    lang = u[0]
    t = TEXT[lang]

    await update.message.reply_text(
        t["g2"] + "\n\n" + "💰 请选择下注金额：",
        reply_markup=universal_bet_keyboard(2, lang)
    )

# ======================
# START / LANGUAGE
# ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("🇲🇾 Malay", callback_data="lang_bm")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")],
        [InlineKeyboardButton("🇨🇳 中文", callback_data="lang_cn")],
    ]
    await update.message.reply_text("Choose language:", reply_markup=InlineKeyboardMarkup(kb))

async def lang_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    lang = q.data.split("_")[1]
    uid = q.from_user.id
    cid = q.message.chat.id

    # 确保用户存在 & 写语言
    ensure_user(uid, cid, lang)
    cur.execute(
        "UPDATE users SET language=? WHERE user_id=? AND chat_id=?",
        (lang, uid, cid)
    )
    conn.commit()

    t = TEXT[lang]

    # ===== 三语入口文案 =====
    if lang == "cn":
        text = (
            "🎲 第一次玩？\n\n"
            "👉 先试【大小】\n"
            "👉 简单容易\n\n"
            "👇 点下面直接开始"
        )
        btn_start = "🎲 快速开始（大小）"
        btn_other = "📋 其他游戏"

    elif lang == "bm":
        text = (
            "🎲 First time?\n\n"
            "👉 Try Big / Small dulu\n"
            "👉 Senang & cepat\n\n"
            "👇 Tekan bawah terus main"
        )
        btn_start = "🎲 START CEPAT"
        btn_other = "📋 GAME LAIN"

    else:  # en
        text = (
            "🎲 First time?\n\n"
            "👉 Try Big / Small first\n"
            "👉 Easy & fast\n\n"
            "👇 Tap below to play"
        )
        btn_start = "🎲 QUICK START"
        btn_other = "📋 OTHER GAMES"

    # ===== 新入口按钮（只有2个）=====
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(btn_start, callback_data="quick_start"),
        ],
        [
            InlineKeyboardButton(btn_other, callback_data="other_games"),
        ],
        [
            InlineKeyboardButton(
                t["contact_admin_btn"],
                url=ADMIN_CONTACT
            )
        ]
    ])

    await q.edit_message_text(
        text,
        reply_markup=keyboard
    )

async def show_game_rule_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    gid = int(q.data.split("_")[1])  # nrule_1 ~ nrule_4
    uid = q.from_user.id
    cid = q.message.chat.id

    u = get_user(uid, cid)
    if not u:
        return

    lang = u[0]
    t = TEXT[lang]

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                t["start_game_btn"],
                callback_data=f"nstart_{gid}"
            )
        ],
        [
            InlineKeyboardButton(
                t["back_btn"],
                callback_data="nback_games"
            )
        ]
    ])

    await q.edit_message_text(
        t[f"rule_g{gid}"],
        reply_markup=keyboard
    )

async def start_game_new_with_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    gid = int(q.data.split("_")[1])
    uid = q.from_user.id
    cid = q.message.chat.id

    u = get_user(uid, cid)
    if not u:
        return

    lang, balance, *_ = u
    t = TEXT[lang]

    # ===== 没余额 =====
    if balance <= 0:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    t["contact_admin_btn"],
                    url=ADMIN_CONTACT
                )
            ],
            [
                InlineKeyboardButton(
                    t["back_btn"],
                    callback_data="nback_games"
                )
            ]
        ])

        await q.message.reply_text(
            t["no_balance_tip"],
            reply_markup=keyboard
        )
        return

    # ===== 更新当前游戏 =====
    cur.execute(
        "UPDATE users SET current_game=? WHERE user_id=? AND chat_id=?",
        (gid, uid, cid)
    )
    conn.commit()

    # ===== Game 5 =====
    if gid == 5:
        await q.message.reply_text(
            t["g5"],
            reply_markup=bet_amount_keyboard_game5(lang)
        )

    # ===== Game 1~4 =====
    else:
        await q.message.reply_text(
            t[f"g{gid}"],
            reply_markup=universal_bet_keyboard(gid, lang)
        )

async def back_to_game_list_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    u = get_user(q.from_user.id, q.message.chat.id)
    if not u:
        return

    lang = u[0]
    t = TEXT[lang]

    # ===== 三语入口文案 =====
    if lang == "cn":
        text = (
            "🎲 第一次玩？\n\n"
            "👉 先试【大小】\n"
            "👉 简单容易\n\n"
            "👇 点下面直接开始"
        )
        btn_start = "🎲 快速开始（大小）"
        btn_other = "📋 其他游戏"

    elif lang == "bm":
        text = (
            "🎲 First time?\n\n"
            "👉 Try Big / Small dulu\n"
            "👉 Senang & cepat\n\n"
            "👇 Tekan bawah terus main"
        )
        btn_start = "🎲 START CEPAT"
        btn_other = "📋 GAME LAIN"

    else:
        text = (
            "🎲 First time?\n\n"
            "👉 Try Big / Small first\n"
            "👉 Easy & fast\n\n"
            "👇 Tap below to play"
        )
        btn_start = "🎲 QUICK START"
        btn_other = "📋 OTHER GAMES"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(btn_start, callback_data="quick_start")],
        [InlineKeyboardButton(btn_other, callback_data="other_games")],
        [
            InlineKeyboardButton(
                t["contact_admin_btn"],
                url=ADMIN_CONTACT
            )
        ]
    ])

    await q.edit_message_text(text, reply_markup=keyboard)

# ======================
# GAME
# ======================
async def clean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ⭐ iPhone 强制清除 Reply Keyboard（最终方案）
    await update.message.reply_text(
        "已清除底部按钮",
        reply_markup=ReplyKeyboardRemove()
    )

# 第一条：专门清 Reply Keyboard（iPhone 只认这个）
async def game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
    " ",
    reply_markup=ReplyKeyboardRemove()
    )

# 第二条：正常发游戏选择（InlineKeyboard）
    await update.message.reply_text(
    t["select_game"],
    reply_markup=InlineKeyboardMarkup(kb)
    )

async def game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id, update.effective_chat.id)
    if not u:
        return

    lang = u[0]
    t = TEXT[lang]

    kb = [
        [
            InlineKeyboardButton("🎲 Dice Duel ⚔️", callback_data="game_1"),
            InlineKeyboardButton("📈 Big Small Rush 🔥", callback_data="game_2"),
        ],
        [
            InlineKeyboardButton("⚖️ Odd Even Clash 🎯", callback_data="game_3"),
            InlineKeyboardButton("🎯 Lucky Number Hit 🍀", callback_data="game_4"),
        ],
        [
            InlineKeyboardButton("💰 Stable Profit Dice 🎁", callback_data="game_5"),
        ],
    ]

    await update.message.reply_text(
        "🎮 Choose Your Game & Start Winning 🔥",
        reply_markup=InlineKeyboardMarkup(kb)
    )

# ✅ 只保留一个（你之前重复了）
async def open_game_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    fake_update = Update(
        update.update_id,
        message=q.message
    )

    await game(fake_update, context)


# ✅ START GAME 按钮（同步改）
async def start_game_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    uid = q.from_user.id
    cid = q.message.chat.id

    u = get_user(uid, cid)
    if not u:
        return

    kb = [
        [
            InlineKeyboardButton("🎲 Dice Duel ⚔️", callback_data="game_1"),
            InlineKeyboardButton("📈 Big Small Rush 🔥", callback_data="game_2"),
        ],
        [
            InlineKeyboardButton("⚖️ Odd Even Clash 🎯", callback_data="game_3"),
            InlineKeyboardButton("🎯 Lucky Number Hit 🍀", callback_data="game_4"),
        ],
        [
            InlineKeyboardButton("💰 Stable Profit Dice 🎁", callback_data="game_5"),
        ],
    ]

    await q.message.reply_text(
        "🎮 Choose Your Game & Start Winning 🔥",
        reply_markup=InlineKeyboardMarkup(kb)
    )

# ======================
# START GAME BUTTON (FINAL FIX)
# ======================
async def start_game_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    uid = query.from_user.id
    cid = query.message.chat.id

    u = get_user(uid, cid)
    if not u:
        return

    # 🎮 最终统一游戏菜单
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎲 Dice Duel ⚔️", callback_data="game_1"),
            InlineKeyboardButton("📈 Big Small Rush 🔥", callback_data="game_2"),
        ],
        [
            InlineKeyboardButton("⚖️ Odd Even Clash 🎯", callback_data="game_3"),
            InlineKeyboardButton("🎯 Lucky Number Hit 🍀", callback_data="game_4"),
        ],
        [
            InlineKeyboardButton("💰 Stable Profit Dice 🎁", callback_data="game_5"),
        ],
    ])

    await query.message.reply_text(
        "🎮 Choose Your Game & Start Winning 🔥",
        reply_markup=keyboard
    )

async def quick_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    uid = q.from_user.id
    cid = q.message.chat.id

    u = get_user(uid, cid)
    if not u:
        return

    lang = u[0]
    t = TEXT[lang]

    # 设定当前游戏 = 2（Big Small）
    cur.execute(
        "UPDATE users SET current_game=? WHERE user_id=? AND chat_id=?",
        (2, uid, cid),
    )
    conn.commit()

    # 直接进入下注界面
    await q.message.reply_text(
        t["g2"],
        reply_markup=universal_bet_keyboard(2, lang)
    )

async def other_games(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    u = get_user(q.from_user.id, q.message.chat.id)
    if not u:
        return

    lang = u[0]
    t = TEXT[lang]

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎲 Dice Duel ⚔️", callback_data="nrule_1"),
            InlineKeyboardButton("📈 Big Small Rush 🔥", callback_data="nrule_2"),
        ],
        [
            InlineKeyboardButton("⚖️ Odd Even Clash 🎯", callback_data="nrule_3"),
            InlineKeyboardButton("🎯 Lucky Number Hit 🍀", callback_data="nrule_4"),
        ],
        [
            InlineKeyboardButton("💰 Stable Profit Dice 🎁", callback_data="nrule_5"),
        ],
        [
            InlineKeyboardButton(
                t["contact_admin_btn"],
                url=ADMIN_CONTACT
            )
        ]
    ])

    await q.edit_message_text(
        t["select_game"],
        reply_markup=keyboard
    )

async def game_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    gid = int(q.data.split("_")[1])
    uid = q.from_user.id
    cid = q.message.chat.id

    cur.execute(
        "UPDATE users SET current_game=? WHERE user_id=? AND chat_id=?",
        (gid, uid, cid),
    )
    conn.commit()

    lang = get_user(uid, cid)[0]
    t = TEXT[lang]

    # ===== Game 5 =====
    if gid == 5:
        await q.edit_message_text(
            t["g5"],
            reply_markup=bet_amount_keyboard_game5(lang)
        )

    # ===== Game 1~4 =====
    else:
        await q.edit_message_text(
            t[f"g{gid}"],
            reply_markup=universal_bet_keyboard(gid, lang)
        )

# ======================
# BET
# ======================
async def bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id, update.effective_chat.id)
    if not u or not u[2]:
        await update.message.reply_text(TEXT[u[0]]["need_game"])
        return

    lang, bal, game, lose, *_ = u
    t = TEXT[lang]

    amt = float(context.args[0])
    if bal < amt:
        await update.message.reply_text(t["no_balance"])
        return

    choice = context.args[1] if len(context.args) > 1 else None
    context.user_data["bet"] = amt
    context.user_data["choice"] = choice

    if game == 1:
        await update.message.reply_text(t["bet_locked"].format(amt=amt))
        return

    # Game 2 & 3 auto roll
    await update.message.reply_text(t["bet_locked_auto"].format(amt=amt))
    await roll(update, context)

# ======================
# BET（按钮下注）
# ======================
async def bet_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    # 解析 callback_data：betbtn_游戏ID_金额
    _, game_id, amt = q.data.split("_")
    game_id = int(game_id)
    amt = int(amt)

    uid = q.from_user.id
    cid = q.message.chat.id

    u = get_user(uid, cid)
    if not u:
        return

    lang, bal, _, *_ = u
    t = TEXT[lang]

    # 余额不足
    if bal < amt:
        await q.message.reply_text(t["no_balance"])
        return

    # ===== 关键：真正“下注成功”的地方（一定要有）=====
    context.user_data["bet"] = amt
    context.user_data["choice"] = None
    # ===================================================

    # Game 1：显示 Roll 按钮（不 roll）
    if game_id == 1:
        await q.message.reply_text(
            t["bet_locked"].format(amt=amt),
            reply_markup=roll_keyboard(lang)
        )
        return

    # Game 2：显示 大 / 小
    if game_id == 2:
        await q.message.reply_text(
            t["bet_locked_no_roll"].format(amt=amt),
            reply_markup=big_small_keyboard(lang)
        )
        return

    # Game 3：显示 单 / 双
    if game_id == 3:
        await q.message.reply_text(
            t["bet_locked_no_roll"].format(amt=amt),
            reply_markup=odd_even_keyboard(lang)
        )
        return

async def custom_bet_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("await_custom_bet"):
        return

    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ 请输入正确的数字金额")
        return

    amt = int(text)
    game_id = context.user_data.pop("custom_game")
    context.user_data.pop("await_custom_bet", None)

    uid = update.effective_user.id
    cid = update.effective_chat.id

    u = get_user(uid, cid)
    if not u:
        return

    lang, bal, *_ = u
    t = TEXT[lang]

    if amt <= 0:
        await update.message.reply_text("❌ 金额必须大于 0")
        return

    if bal < amt:
        await update.message.reply_text(t["no_balance"])
        return

    context.user_data["bet"] = amt
    context.user_data["choice"] = None

    if game_id == 1:
        await update.message.reply_text(
            t["bet_locked"].format(amt=amt),
            reply_markup=roll_keyboard(lang)
        )
        return

    if game_id == 2:
        await update.message.reply_text(
            t["bet_locked"].format(amt=amt),
            reply_markup=big_small_keyboard(lang)
        )
        return

    if game_id == 3:
        await update.message.reply_text(
            t["bet_locked"].format(amt=amt),
            reply_markup=odd_even_keyboard(lang)
        )
        return

async def bet_button_v2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    _, game_id, amt = q.data.split("_")
    game_id = int(game_id)
    amt = int(amt)

    uid = q.from_user.id
    cid = q.message.chat.id

    u = get_user(uid, cid)
    if not u:
        return

    lang, bal, current_game, *_ = u
    t = TEXT[lang]

    if bal < amt:
        await q.message.reply_text(t["no_balance"])
        return

    # ⭐ 记录下注（按钮版）
    context.user_data["bet"] = amt
    context.user_data["choice"] = None

    # Game 1 → Roll
    if game_id == 1:
        await q.message.reply_text(
            t["bet_locked"].format(amt=amt),
            reply_markup=roll_keyboard(lang)
        )
        return

    # Game 2 → Big / Small
    if game_id == 2:
        await q.message.reply_text(
            t["bet_locked"].format(amt=amt),
            reply_markup=big_small_keyboard(lang)
        )
        return

    # Game 3 → Odd / Even
    if game_id == 3:
        await q.message.reply_text(
            t["bet_locked"].format(amt=amt),
            reply_markup=odd_even_keyboard(lang)
        )
        return

# ======================
# ROLL
# ======================
async def roll_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    fake_update = Update(
        update.update_id,
        message=q.message
    )

    await roll(fake_update, context)

async def choice_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    context.user_data["choice"] = q.data.split("_")[1]

    fake_update = Update(
        update.update_id,
        message=q.message
    )

    await roll(fake_update, context)

async def game1_auto_roll(q, context):
    uid = q.from_user.id
    cid = q.message.chat.id

    u = get_user(uid, cid)
    if not u:
        return

    lang, bal, _, lose_streak, win_c, lose_c, draw_c = u
    t = TEXT[lang]

    # 防止二次 Roll
    amt = context.user_data.pop("bet", None)
    if amt is None:
        await q.message.reply_text(t["roll_first"])
        return

    # 🎲 玩家骰
    player_msg = await q.message.reply_dice()
    await asyncio.sleep(2.8)
    player = player_msg.dice.value

    # 🎲 庄家骰
    banker_msg = await q.message.reply_dice()
    await asyncio.sleep(2.8)
    banker = banker_msg.dice.value

    # ===== 判定 =====
    if player > banker:
        res = t["win"]
        delta = amt
        win_c += 1
        lose_streak = 0

    elif player < banker:
        res = t["lose"]
        delta = -amt
        lose_c += 1
        lose_streak += 1

    else:
        res = t["draw"]
        delta = 0
        draw_c += 1

    # ===== 更新余额（统一放这里）=====
    if delta != 0:
        update_balance_atomic(uid, cid, delta)

    new_bal = get_balance(uid, cid)

    # ===== 数据库 =====
    cur.execute(
        "UPDATE users SET lose_streak=?, win=?, lose=?, draw=? WHERE user_id=? AND chat_id=?",
        (lose_streak, win_c, lose_c, draw_c, uid, cid)
    )
    conn.commit()

    # ===== 结果展示 =====
    result_text = (
        f"{t['dice_result_title']}\n"
        f"{t['player_label']}：{player}\n"
        f"{t['banker_label']}：{banker}\n\n"
        f"{res}\n"
        f"{'+' if delta > 0 else ''}{delta}\n"
        f"💰 Balance: {new_bal:.2f}".rstrip('0').rstrip('.')
    )

    await q.message.reply_text(
        result_text,
        reply_markup=universal_bet_keyboard(1, lang)
    )

async def game2_auto_roll(q, context, choice: str):
    uid = q.from_user.id
    cid = q.message.chat.id

    u = get_user(uid, cid)
    if not u:
        return

    lang, bal, _, lose_streak, win_c, lose_c, draw_c = u
    t = TEXT[lang]

    # 防止二次点击
    amt = context.user_data.pop("bet", None)
    if amt is None:
        await q.message.reply_text(t["roll_first"])
        return

    # 🎲 庄家骰
    dice_msg = await q.message.reply_dice()
    await asyncio.sleep(2.8)
    banker = dice_msg.dice.value

    win = (choice == "big" and banker >= 4) or (choice == "small" and banker <= 3)

    if win:
        res = t["win"]
        delta = amt
        win_c += 1
        lose_streak = 0

    else:
        res = t["lose"]
        delta = -amt
        lose_c += 1
        lose_streak += 1

    # ===== ✅ 正确更新余额 =====
    update_balance_atomic(uid, cid, delta)
    new_bal = get_balance(uid, cid)

    # ===== 数据库 =====
    cur.execute(
        "UPDATE users SET lose_streak=?, win=?, lose=? WHERE user_id=? AND chat_id=?",
        (lose_streak, win_c, lose_c, uid, cid)
    )
    conn.commit()

    # ===== 显示结果 =====
    choice_text = t["big_btn"] if choice == "big" else t["small_btn"]

    result_text = (
        f"🎯 {t['your_choice']}: {choice_text}\n"
        f"{t['dice_result_title']}\n"
        f"{t['banker_label']}: {banker}\n\n"
        f"{res}\n"
        f"{'+' if delta > 0 else ''}{delta}\n"
        f"💰 {t['balance_label']}: {new_bal:.2f}"
    )

    await q.message.reply_text(
        result_text,
        reply_markup=universal_bet_keyboard(2, lang)
    )

async def game3_auto_roll(q, context, choice: str):
    uid = q.from_user.id
    cid = q.message.chat.id

    u = get_user(uid, cid)
    if not u:
        return

    lang, bal, _, lose_streak, win_c, lose_c, draw_c = u
    t = TEXT[lang]

    # 防止二次点击
    amt = context.user_data.pop("bet", None)
    if amt is None:
        await q.message.reply_text(t["roll_first"])
        return

    # 🎲 庄家骰
    dice_msg = await q.message.reply_dice()
    await asyncio.sleep(2.8)
    banker = dice_msg.dice.value

    win = (choice == "odd" and banker % 2 == 1) or (choice == "even" and banker % 2 == 0)

    if win:
        res = t["win"]
        delta = amt
        win_c += 1
        lose_streak = 0

    else:
        res = t["lose"]
        delta = -amt
        lose_c += 1
        lose_streak += 1

    # ===== ✅ 正确更新余额 =====
    update_balance_atomic(uid, cid, delta)
    new_bal = get_balance(uid, cid)

    # ===== 数据库 =====
    cur.execute(
        "UPDATE users SET lose_streak=?, win=?, lose=? WHERE user_id=? AND chat_id=?",
        (lose_streak, win_c, lose_c, uid, cid)
    )
    conn.commit()

    # ===== 显示结果 =====
    choice_text = t["odd_btn"] if choice == "odd" else t["even_btn"]

    result_text = (
        f"🎯 {t['your_choice']}: {choice_text}\n"
        f"{t['dice_result_title']}\n"
        f"{t['banker_label']}: {banker}\n\n"
        f"{res}\n"
        f"{'+' if delta > 0 else ''}{delta}\n"
        f"💰 {t['balance_label']}: {new_bal:.2f}"
    )

    await q.message.reply_text(
        result_text,
        reply_markup=universal_bet_keyboard(3, lang)
    )

async def game4_auto_roll(q, context, guess: int):
    uid = q.from_user.id
    cid = q.message.chat.id

    u = get_user(uid, cid)
    if not u:
        return

    lang, bal, _, lose_streak, win_c, lose_c, draw_c = u
    t = TEXT[lang]

    # 防止二次点击
    amt = context.user_data.pop("bet", None)
    if amt is None:
        await q.message.reply_text(t["roll_first"])
        return

    # 🎲 庄家骰
    dice_msg = await q.message.reply_dice()
    await asyncio.sleep(2.8)
    result = dice_msg.dice.value

    # ===== 🎯 判定 =====
    if result == guess:
        res = t["win"]
        delta = amt * GUESS_NUMBER_WIN_MULTIPLIER
        win_c += 1
        lose_streak = 0

    else:
        res = t["lose"]
        delta = -amt
        lose_c += 1
        lose_streak += 1

    # ===== 💰 正确更新余额（不会错钱）=====
    update_balance_atomic(uid, cid, delta)
    new_bal = get_balance(uid, cid)

    # ===== 📊 更新战绩（不动 balance）=====
    cur.execute(
        "UPDATE users SET lose_streak=?, win=?, lose=? WHERE user_id=? AND chat_id=?",
        (lose_streak, win_c, lose_c, uid, cid)
    )
    conn.commit()

    # ===== 📢 显示结果 =====
    result_text = (
        f"🎯 {t['your_choice']}: {guess}\n"
        f"{t['dice_result_title']}\n"
        f"{t['banker_label']}: {result}\n\n"
        f"{res}\n"
        f"{'+' if delta > 0 else ''}{delta}\n"
        f"💰 {t['balance_label']}: {new_bal:.2f}"
    )

    await context.bot.send_message(
        chat_id=cid,
        text=result_text,
        reply_markup=universal_bet_keyboard(4, lang)
    )

async def game5_auto_roll(q, context):
    uid = q.from_user.id
    cid = q.message.chat.id
    key = (cid, uid)

    u = get_user(uid, cid)
    if not u:
        return

    lang, bal, lose_streak, win_c, lose_c = u[0], u[1], u[3], u[4], u[5]
    t = TEXT[lang]

    now = time.time()
    cooldown = get_dynamic_cooldown(key)
    last = last_game5_time.get(key, 0)

    if now - last < cooldown:
        await q.message.reply_text(t["cooldown_wait"])
        return

    last_game5_time[key] = now
    game5_history.setdefault(key, deque(maxlen=HISTORY_LIMIT)).append(now)

    amt = context.user_data.pop("bet", None)
    if amt is None:
        await q.message.reply_text(t["roll_first"])
        return

    # 🎲 掷骰（动画）
    dice_msg = await q.message.reply_dice()

    # ⏳ 等动画（⚠️ 不要拆 coroutine）
    await asyncio.sleep(2.8)

    banker = dice_msg.dice.value

    # ===== 结算 =====
    if banker <= 4:
        delta = amt * STABLE_DICE_WIN_MULTIPLIER
        res = t["win"]
        lose_streak = 0
        win_c += 1

    else:
        delta = -amt
        res = t["lose"]
        lose_streak += 1
        lose_c += 1

    # ===== ✅ 正确更新余额 =====
    update_balance_atomic(uid, cid, delta)
    new_bal = get_balance(uid, cid)

    # ===== 数据库（executor，不阻塞）=====
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        partial(
            cur.execute,
            "UPDATE users SET lose_streak=?, win=?, lose=? WHERE user_id=? AND chat_id=?",
            (lose_streak, win_c, lose_c, uid, cid)
        )
    )
    await loop.run_in_executor(None, conn.commit)

    # ===== 结算展示（最终版，只 1 次）=====
    final_text = (
        f"{t['bet_locked_no_roll'].format(amt=amt)}\n\n"
        f"{t['dice_result_title']}\n"
        f"{t['banker_label']}: {banker}\n\n"
        f"{res}\n"
        f"{'+' if delta > 0 else ''}{delta}\n"
        f"💰 {t['balance_label']}: {new_bal:.2f}".rstrip('0').rstrip('.')
    )

    await q.message.reply_text(
        final_text,
        reply_markup=bet_amount_keyboard_game5(lang)
    )

async def roll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id, update.effective_chat.id)
    if not u:
        return

    lang, bal, game, lose, win_c, lose_c, draw_c = u
    t = TEXT[lang]

    if "bet" not in context.user_data:
        await update.message.reply_text(t["roll_first"])
        return

    amt = context.user_data.pop("bet")
    choice = context.user_data.pop("choice", None)

    # 🎲 animated dice
    if game == 1:
        p_msg = await update.message.reply_dice()
        await asyncio.sleep(2.8)   # 等玩家骰子动画跑完
        b_msg = await update.message.reply_dice()
        await asyncio.sleep(2.8)   # 等庄家骰子动画跑完

        player = p_msg.dice.value
        banker = b_msg.dice.value
    else:
        await asyncio.sleep(2.8)
        b_msg = await update.message.reply_dice()
        await asyncio.sleep(2.8)

        player = "-"
        banker = b_msg.dice.value

    win = False
    draw = False

    if game == 1:
        if player > banker:
            win = True
        elif player == banker:
            draw = True
    elif game == 2:
        win = (choice == "big" and banker >= 4) or (choice == "small" and banker <= 3)
    elif game == 3:
        win = (choice == "odd" and banker % 2 == 1) or (choice == "even" and banker % 2 == 0)
    elif game == 5:
        # Game 5｜稳定骰
        if banker <= 4:
            win = True
        else:
            win = False

    if draw:
        res = t["draw"]
        delta = 0
        draw_c += 1

    elif win:
        res = t["win"]
        if game == 5:
            delta = amt * STABLE_DICE_WIN_MULTIPLIER
        else:
            delta = amt
        win_c += 1
        lose = 0

    else:
        res = t["lose"]
        delta = -amt
        lose_c += 1
        lose += 1

    # ===== ✅ 正确更新余额（不会错钱）=====
    if delta != 0:
        update_balance_atomic(uid, cid, delta)

    new_bal = get_balance(uid, cid)

    # ===== ✅ 更新战绩（不再动 balance）=====
    cur.execute(
        "UPDATE users SET lose_streak=?, win=?, lose=?, draw=? WHERE user_id=? AND chat_id=?",
        (lose_streak, win_c, lose_c, draw_c, uid, cid),
    )
    conn.commit()

    # ===== 显示结果 =====
    final_text = t["result"].format(
        p=player,
        b=banker,
        res=res,
        delta=f"{'+' if delta > 0 else ''}{delta}",
        bal=new_bal,
    )

    await update.message.reply_text(
        final_text,
        reply_markup=bet_amount_keyboard_v2(game, lang)
    )

async def game2_choice_auto_roll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    choice = q.data.split("_")[1]  # big / small

    uid = q.from_user.id
    cid = q.message.chat.id

    u = get_user(uid, cid)
    if not u:
        return

    lang = u[0]
    t = TEXT[lang]

    # 必须已下注
    if "bet" not in context.user_data:
        await q.message.reply_text(t["roll_first"])
        return

    # ⭐ 关键：后台并发执行（不卡）
    asyncio.create_task(game2_auto_roll(q, context, choice))

async def game3_choice_auto_roll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    choice = q.data.split("_")[1]  # odd / even

    uid = q.from_user.id
    cid = q.message.chat.id

    u = get_user(uid, cid)
    if not u:
        return

    lang = u[0]
    t = TEXT[lang]

    if "bet" not in context.user_data:
        await q.message.reply_text(t["roll_first"])
        return

    # ⭐ 并发执行（不卡）
    asyncio.create_task(game3_auto_roll(q, context, choice))

async def game3_bet_button_v3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    amt = int(q.data.split("_")[1])

    uid = q.from_user.id
    cid = q.message.chat.id

    u = get_user(uid, cid)
    if not u:
        return

    lang, bal, *_ = u
    t = TEXT[lang]

    if bal < amt:
        await q.message.reply_text(t["no_balance"])
        return

    # 记录下注
    context.user_data["bet"] = amt
    context.user_data["choice"] = None

    # 直接给 Odd / Even（不需要 /roll）
    await q.message.reply_text(
        t["bet_locked_no_roll"].format(amt=amt),
        reply_markup=odd_even_keyboard(lang)
    )

async def bet_button_universal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    _, game_id, value = q.data.split("_")
    game_id = int(game_id)

    uid = q.from_user.id
    cid = q.message.chat.id
    u = get_user(uid, cid)
    if not u:
        return

    lang, bal, *_ = u
    t = TEXT[lang]

    # ===== 自定义金额 =====
    if value == "custom":
        context.user_data["await_custom_bet"] = True
        context.user_data["custom_game"] = game_id
        await q.message.reply_text(t["enter_custom_amount"])
        return

    # ===== 固定金额 =====
    amt = int(value)

    # ===== Game 5 单局下注上限 =====
    if game_id == 5 and amt > 100:
        await update.message.reply_text(t["g5_limit"])
        return

    # ===== 余额检查 =====
    if bal < amt:
        await q.message.reply_text(t["no_balance"])
        return

    # ===== Game 5 单局限额 =====
    if game_id == 5 and amt > 100:
        await update.message.reply_text(t["g5_limit"])
        return

    # ===== 固定金额 =====
    amt = int(value)
    if bal < amt:
        await q.message.reply_text(t["no_balance"])
        return

    context.user_data["bet"] = amt
    context.user_data["choice"] = None

    # ===== Game 1 =====
    if game_id == 1:
        await q.message.reply_text(
            t["bet_locked"].format(amt=amt),
            reply_markup=roll_keyboard(lang)
        )
        return

    # ===== Game 2 =====
    if game_id == 2:
        await q.message.reply_text(
            t["bet_locked_no_roll"].format(amt=amt),
            reply_markup=big_small_keyboard(lang)
        )
        return

    # ===== Game 3 =====
    if game_id == 3:
        await q.message.reply_text(
            t["bet_locked_no_roll"].format(amt=amt),
            reply_markup=odd_even_keyboard(lang)
        )
        return

    # ===== Game 4｜猜点数 =====
    if game_id == 4:
        await context.bot.send_message(
            chat_id=q.message.chat.id,
            text=t["bet_locked_no_roll"].format(amt=amt)
        )
        await context.bot.send_message(
            chat_id=q.message.chat.id,
            text=t["guess_title"],
            reply_markup=guess_number_keyboard()
        )
        return

    # ===== Game 5｜稳定骰 =====
    if game_id == 5:
        context.user_data["bet"] = amt

        # ② 直接进入 Game5 自动掷骰
        # ② 后台异步执行（不阻塞）
        asyncio.create_task(game5_auto_roll(q, context))

        return


async def game4_guess_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    # 玩家选择的点数 1–6
    guess = int(q.data.split("_")[1])

    uid = q.from_user.id
    cid = q.message.chat.id
    u = get_user(uid, cid)
    if not u:
        return

    lang, bal, _, lose_streak, win_c, lose_c, draw_c = u
    t = TEXT[lang]

    # ===== 必须先确认有下注 =====
    amt = context.user_data.get("bet")
    if amt is None:
        await context.bot.send_message(
            chat_id=q.message.chat.id,
            text=t["roll_first"]
        )
        return

    # ===== 掷骰（只 1 颗）=====
    dice_msg = await q.message.reply_dice()
    await asyncio.sleep(2.8)
    result = dice_msg.dice.value


async def game4_guess_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    guess = int(q.data.split("_")[1])  # 1–6

    uid = q.from_user.id
    cid = q.message.chat.id

    u = get_user(uid, cid)
    if not u:
        return

    lang = u[0]
    t = TEXT[lang]

    if "bet" not in context.user_data:
        await context.bot.send_message(
            chat_id=q.message.chat.id,
            text=t["roll_first"]
        )
        return

    # ⭐ 并发执行（不卡）
    asyncio.create_task(game4_auto_roll(q, context, guess))

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 只处理自定义下注金额
    if context.user_data.get("await_custom_bet"):
        await custom_amount_input(update, context)
        return


    # 其它文字全部忽略（避免被吃掉）
    return

    # ===== 更新数据库 =====
    cur.execute(
        "UPDATE users SET lose_streak=?, win=?, lose=? WHERE user_id=? AND chat_id=?",
        (lose_streak, win_c, lose_c, uid, cid)
    )
    conn.commit()

    # ===== 清掉下注（结算后才清）=====
    context.user_data.pop("bet", None)

    # ===== 显示结算结果 =====
    result_text = (
        f"🎯 {t['your_choice']}: {guess}\n"
        f"{t['dice_result_title']}\n"
        f"{t['banker_label']}: {result}\n\n"
        f"{res}\n"
        f"{'+' if delta > 0 else ''}{delta}\n"
        f"💰 {t.get('balance_label', 'Balance')}: {new_bal:.2f}".rstrip('0').rstrip('.')
    )

    await q.message.reply_text(result_text)


    # ===== 回到下一局下注 =====
    final_text = result_text

    await q.message.reply_text(
        final_text,
        reply_markup=universal_bet_keyboard(4, lang)
    )

async def custom_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if not text.isdigit():
        await update.message.reply_text("❌ Please enter a valid number")
        return

    amt = int(text)

    game_id = context.user_data.pop("custom_game", None)
    context.user_data.pop("await_custom_bet", None)

    if game_id is None:
        return

    uid = update.effective_user.id
    cid = update.effective_chat.id
    u = get_user(uid, cid)
    if not u:
        return

    # ✅ 先取语言
    lang = u[0]
    t = TEXT[lang]

    # ===== Game 5 限额（⚠️ 一定要在 t 定义之后）=====
    if game_id == 5 and amt > 100:
        await update.message.reply_text(t["g5_limit"])
        return

    bal = u[1]

    if amt <= 0 or bal < amt:
        await update.message.reply_text(t["no_balance"])
        return

    context.user_data["bet"] = amt
    context.user_data["choice"] = None

    # ===== 分游戏处理 =====
    if game_id == 1:
        await update.message.reply_text(
            t["bet_locked"].format(amt=amt),
            reply_markup=roll_keyboard(lang)
        )

    elif game_id == 2:
        await update.message.reply_text(
            t["bet_locked_no_roll"].format(amt=amt),
            reply_markup=big_small_keyboard(lang)
        )

    elif game_id == 3:
        await update.message.reply_text(
            t["bet_locked_no_roll"].format(amt=amt),
            reply_markup=odd_even_keyboard(lang)
        )

    elif game_id == 4:
        await context.bot.send_message(
            chat_id=update.message.chat.id,
            text=t["bet_locked_no_roll"].format(amt=amt)
        )
        await context.bot.send_message(
            chat_id=update.message.chat.id,
            text=t["guess_title"],
            reply_markup=guess_number_keyboard()
        )
	
    elif game_id == 5:
        await update.message.reply_text(
            t["bet_locked_auto"].format(amt=amt)
        )

        # 👇 复用你已有的 Game5 自动结算
        class FakeQuery:
            def __init__(self, message, user):
                self.message = message
                self.from_user = user

        fake_q = FakeQuery(update.message, update.effective_user)
        asyncio.create_task(game5_auto_roll(fake_q, context))

# ======================
# ROLL (BUTTON VERSION - FINAL)
# ======================
async def roll_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    uid = q.from_user.id
    cid = q.message.chat.id

    u = get_user(uid, cid)
    if not u:
        return

    lang, bal, game, *_ = u
    t = TEXT[lang]

    # 只处理 Game 1
    if game != 1:
        return

    # 必须先下注
    if "bet" not in context.user_data:
        await q.message.reply_text(t["roll_first"])
        return

    # ⭐ 核心：不 await，不 sleep
    asyncio.create_task(game1_auto_roll(q, context))

# ======================
# ADMIN
# ======================
async def grant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id

    if admin_id not in BANKER_IDS or not update.message.reply_to_message:
        return

    # ===== 自动删除庄家指令 =====
    try:
        await update.message.delete()
    except Exception as e:
        print(e)

    # ===== 金额处理（支持小数）=====
    try:
        amt = float(context.args[0])
    except:
        await context.bot.send_message(
            chat_id=admin_id,
            text="❌ 请输入正确金额，例如 /grant 10 或 /grant 10.5"
        )
        return

    target = update.message.reply_to_message.from_user.id
    chat_id = update.effective_chat.id

    # ===== 写入 pending =====
    cur.execute(
        """
        INSERT INTO pending_admin
        (admin_id, chat_id, target_user, action, amount, expire_at)
        VALUES (?,?,?,?,?,?)
        """,
        (
            admin_id,
            chat_id,
            target,
            "grant",
            amt,
            time.time() + CONFIRM_TIMEOUT
        ),
    )
    conn.commit()

    # ===== 私聊确认 =====
    await context.bot.send_message(
        chat_id=admin_id,
        text=f"⚠️ 确认充值 {amt}\n请输入 /confirm 或 /cancel"
    )

async def grants(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id

    if admin_id not in BANKER_IDS or not update.message.reply_to_message:
        return

    # ===== 自动删除庄家指令 =====
    try:
        await update.message.delete()
    except Exception as e:
        print(e)

    # ===== 金额处理（支持小数）=====
    try:
        amt = float(context.args[0])
    except:
        await context.bot.send_message(
            chat_id=admin_id,
            text="❌ 请输入正确金额，例如 /grants 10 或 /grants 10.5"
        )
        return

    target = update.message.reply_to_message.from_user.id
    chat_id = update.effective_chat.id

    # ===== 写入 pending =====
    cur.execute(
        """
        INSERT INTO pending_admin
        (admin_id, chat_id, target_user, action, amount, expire_at)
        VALUES (?,?,?,?,?,?)
        """,
        (
            admin_id,
            chat_id,
            target,
            "grant_no_btn",
            amt,
            time.time() + CONFIRM_TIMEOUT
        ),
    )
    conn.commit()

    # ===== 私聊确认 =====
    await context.bot.send_message(
        chat_id=admin_id,
        text=f"⚠️ 确认充值 {amt}\n请输入 /confirm 或 /cancel"
    )

async def deduct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id

    if admin_id not in BANKER_IDS or not update.message.reply_to_message:
        return

    # ===== 自动删除庄家指令 =====
    try:
        await update.message.delete()
    except Exception as e:
        print(e)

    # ===== 金额处理（支持小数）=====
    try:
        amt = float(context.args[0])
    except:
        await context.bot.send_message(
            chat_id=admin_id,
            text="❌ 请输入正确金额，例如 /deduct 10 或 /deduct 10.5"
        )
        return

    target = update.message.reply_to_message.from_user.id
    chat_id = update.effective_chat.id

    # ===== 写入 pending =====
    cur.execute(
        """
        INSERT INTO pending_admin
        (admin_id, chat_id, target_user, action, amount, expire_at)
        VALUES (?,?,?,?,?,?)
        """,
        (
            admin_id,
            chat_id,
            target,
            "deduct",
            amt,
            time.time() + CONFIRM_TIMEOUT
        ),
    )
    conn.commit()

    # ===== 私聊确认 =====
    await context.bot.send_message(
        chat_id=admin_id,
        text=f"⚠️ 确认下分 {amt}\n请输入 /confirm 或 /cancel"
    )

async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):

    # ===== 防重复执行 =====
    if context.user_data.get("confirm_lock"):
        return
    context.user_data["confirm_lock"] = True

    admin_id = update.effective_user.id

    try:
        # ===== 权限检查 =====
        if admin_id not in BANKER_IDS:
            return

        # ===== 取最新一笔 pending（🔥关键）=====
        cur.execute(
            """
            SELECT id, chat_id, target_user, action, amount
            FROM pending_admin
            WHERE admin_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (admin_id,)
        )
        row = cur.fetchone()

        if not row:
            await update.message.reply_text("❌ 没有待确认的操作")
            return

        pending_id, chat_id, target_id, action, amt = row

        # ===== 只删除这一笔（🔥修复你之前的大bug）=====
        cur.execute(
            "DELETE FROM pending_admin WHERE id=?",
            (pending_id,)
        )
        conn.commit()

        # ===== 获取用户 =====
        user = get_user(target_id, chat_id)
        if not user:
            return

        lang = user[0]
        t = TEXT[lang]

        # ===== 更新余额 =====
        if action in ["grant", "grant_no_btn"]:
            update_balance_atomic(target_id, chat_id, amt)

        else:
            current_bal = get_balance(target_id, chat_id)

            # ❌ 余额为0
            if current_bal <= 0:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text="❌ 玩家余额为0，无法扣分"
                )
                return

            # ❌ 不够扣
            if current_bal < amt:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"❌ 余额不足（当前: {current_bal}）"
                )
                return

            # ✅ 正常扣
            update_balance_atomic(target_id, chat_id, -amt)

        # ===== 记录流水 =====
        cur.execute(
            """
            INSERT INTO banker_ledger (chat_id, admin_id, action, amount, created_at)
            VALUES (?,?,?,?,?)
            """,
            (chat_id, admin_id, action, amt, time.time())
        )
        conn.commit()

        # ===== 最新余额 =====
        new_bal = get_balance(target_id, chat_id)

        # ===== 通知庄家 =====
        await context.bot.send_message(
            chat_id=admin_id,
            text=f"✅ {action.upper()} {amt} 成功\n玩家新余额：{new_bal:.2f}".rstrip('0').rstrip('.')
        )

        # ===== 群通知 =====
        if action in ["grant", "grant_no_btn"]:
            msg_key = "grant_ok"
        else:
            msg_key = "deduct_ok"

        await context.bot.send_message(
            chat_id=chat_id,
            text=t[msg_key].format(
                amt=amt,
                bal=f"{new_bal:.2f}".rstrip('0').rstrip('.')
            ),
            reply_markup=start_game_keyboard(lang) if action == "grant" else None
        )

    finally:
        # ===== 一定解锁 =====
        context.user_data["confirm_lock"] = False

# ======================
# STATS
# ======================
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cid = update.effective_chat.id

    u = get_user(uid, cid)
    if not u:
        return

    lang, _, _, ls, w, l, d = u
    bal = get_balance(uid, cid)

    t = TEXT[lang]
    total = w + l + d

    await update.message.reply_text(
        t["stats"].format(
            bal=f"{bal:.2f}".rstrip('0').rstrip('.'),
            total=total,
            w=w,
            l=l,
            d=d,
            ls=ls
        )
    )

# ======================
# STATUS (BANKER ONLY, PRIVATE, REAL AMOUNT)
# ======================
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    # 非庄家不理
    if uid not in BANKER_IDS:
        return

    chat_id = update.effective_chat.id

    # 累计庄家盈亏（grant +，deduct -）
    cur.execute(
        """
        SELECT
            COALESCE(SUM(
                CASE
                    WHEN action='grant' THEN amount
                    WHEN action='deduct' THEN -amount
                    ELSE 0
                END
            ), 0)
        FROM banker_ledger
        WHERE chat_id=?
        """,
        (chat_id,)
    )

    banker_profit = cur.fetchone()[0]

    text = (
        "📊 庄家状态（累计）\n\n"
        f"🏦 庄家盈亏：{banker_profit:+}\n\n"
        "📌 说明：\n"
        "• grant = 庄家盈利\n"
        "• deduct = 庄家亏损\n"
        "• 从建群第一笔开始永久累计"
    )

    # 私聊庄家
    await context.bot.send_message(
        chat_id=uid,
        text=text
    )

# =====================
# BANKER STATS (NEW)
# =====================
async def banker_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in BANKER_IDS:
        return  # 非庄家直接无视

    chat_id = update.effective_chat.id

    # 玩家数
    cur.execute(
        "SELECT COUNT(*) FROM users WHERE chat_id=? AND balance > 0",
        (chat_id,),
    )
    player_count = cur.fetchone()[0]

    # 总局数（以赢+输+平估算）
    cur.execute(
        "SELECT SUM(win + lose + draw) FROM users WHERE chat_id=?",
        (chat_id,),
    )
    row = cur.fetchone()
    total_rounds = row[0] if row and row[0] else 0

    # 玩家赢 / 输
    cur.execute(
        "SELECT SUM(win), SUM(lose) FROM users WHERE chat_id=?",
        (chat_id,),
    )
    w, l = cur.fetchone()
    w = w or 0
    l = l or 0

    # 庄家盈亏 = 玩家输 - 玩家赢
    banker_profit = l - w

    text = (
        "📊 庄家统计（本群）\n\n"
        f"👥 玩家数：{player_count}\n"
        f"🎮 总局数：{total_rounds}\n\n"
        f"🏆 玩家赢：+{w}\n"
        f"❌ 玩家输：-{l}\n\n"
        f"🏦 庄家盈亏：{banker_profit:+}\n"
        f"🎯 当前胜率控制：{int(BASE_BANKER_WIN_RATE * 100)}%"
    )

    await update.message.reply_text(text)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # ===== Commands =====
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("game", game))
    app.add_handler(CommandHandler("clean", clean))
    app.add_handler(CommandHandler("bet", bet))
    app.add_handler(CommandHandler("roll", roll))
    app.add_handler(CommandHandler("grant", grant))
    app.add_handler(CommandHandler("grants", grants))
    app.add_handler(CommandHandler("deduct", deduct))
    app.add_handler(CommandHandler("confirm", confirm))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("banker_stats", banker_stats))
    app.add_handler(CommandHandler("status", status))

    # ===== ✅ 自动触发（不用 /）=====
    app.add_handler(MessageHandler(filters.Regex(r'(?i)^start'), start))
    app.add_handler(MessageHandler(filters.Regex(r'(?i)^game'), game))

    # ===== Language & Game Flow =====
    app.add_handler(CallbackQueryHandler(lang_select, pattern="^lang_"))
    app.add_handler(CallbackQueryHandler(start_game_callback, pattern="^START_GAME$"))
    app.add_handler(CallbackQueryHandler(quick_start, pattern="^quick_start$"))
    app.add_handler(CallbackQueryHandler(other_games, pattern="^other_games$"))
    app.add_handler(CallbackQueryHandler(show_game_rule_new, pattern="^nrule_[1-5]$"))
    app.add_handler(CallbackQueryHandler(start_game_new_with_balance, pattern="^nstart_[1-5]$"))
    app.add_handler(CallbackQueryHandler(back_to_game_list_new, pattern="^nback_games$"))
    app.add_handler(CallbackQueryHandler(game_select, pattern="^game_"))

    # ===== Betting =====
    app.add_handler(CallbackQueryHandler(bet_button_universal, pattern="^betu_"))

    # ===== Choices =====
    app.add_handler(CallbackQueryHandler(game2_choice_auto_roll, pattern="^choice_(big|small)$"))
    app.add_handler(CallbackQueryHandler(game3_choice_auto_roll, pattern="^choice_(odd|even)$"))

    # ===== Game 4 Guess =====
    app.add_handler(CallbackQueryHandler(game4_guess_number, pattern="^guess_[1-6]$"))

    # ===== Roll =====
    app.add_handler(CallbackQueryHandler(roll_button, pattern="^rollbtn$"))

    # ===== Text (Custom Amount) =====
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex("^/"), text_router))

    print("🎲 Dice Game Bot running (FINAL)")
    app.run_polling()


if __name__ == "__main__":
    main()






















