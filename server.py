import os
import json
import logging
import sqlite3
import time
import re
import random
import secrets
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple, Any
from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI, Request, HTTPException, Depends, Header, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
import httpx

# ==================== ФИКС ДЛЯ DATETIME В SQLITE ====================
def adapt_datetime(dt):
    return dt.isoformat()

def convert_datetime(s):
    return datetime.fromisoformat(s.decode())

sqlite3.register_adapter(datetime, adapt_datetime)
sqlite3.register_converter("TIMESTAMP", convert_datetime)

# ==================== КОНФИГ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8707239993:AAEVh5E16a-lUyLzGov1fLIXvhV2IEAb788")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8814572765"))
DB_NAME = os.getenv("DB_NAME", "arzdrop.db")
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/"
CHANNEL_ARZDROP = "@ARZDROPCS2"
CHANNEL_ARTSTUDIO = "@ARTStudy16"
COURSE = float(os.getenv("COURSE", "217.39"))
SELL_PRICE_PERCENT = int(os.getenv("SELL_PRICE_PERCENT", "95"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://your-domain.com")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))

# ==================== ЛОГИРОВАНИЕ ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== АЧИВКА ДОНАТЕРА ====================
ACHIEVEMENT_LEVELS = [100, 500, 1000, 5000, 10000, 50000, 100000, 500000, 1000000, 5000000]
ACHIEVEMENT_REWARDS = [500, 2500, 5000, 6000, 7500, 8500, 10000, 12500, 15000, 25000]

def get_donator_level(total_donated: int) -> Tuple[int, int, int]:
    level = 0
    for i, req in enumerate(ACHIEVEMENT_LEVELS):
        if total_donated >= req:
            level = i + 1
        else:
            break
    
    if level == 0:
        return 0, ACHIEVEMENT_LEVELS[0], 0
    
    if level >= len(ACHIEVEMENT_LEVELS):
        return level, 0, ACHIEVEMENT_REWARDS[-1]
    
    next_req = ACHIEVEMENT_LEVELS[level]
    reward = ACHIEVEMENT_REWARDS[level - 1]
    
    return level, next_req - total_donated, reward

def check_donator_achievement(user_id: int):
    user = get_user(user_id)
    if not user:
        return
    
    total_donated = user.get('total_donated_rub', 0)
    level, needed, reward = get_donator_level(total_donated)
    
    if level > user.get('donator_level', 0):
        conn = get_db()
        cur = conn.cursor()
        cur.execute('UPDATE users SET donator_level = ? WHERE id = ?', (level, user_id))
        cur.execute('UPDATE users SET balance = balance + ? WHERE id = ?', (reward, user_id))
        conn.commit()
        conn.close()
        
        send_message(
            user_id,
            f"<b>🏆 ДОСТИЖЕНИЕ РАЗБЛОКИРОВАНО!</b>\n\n"
            f"📌 Тип: Донатер\n"
            f"🎯 Уровень: {level}\n"
            f"💰 Награда: +{reward} монет",
            parse_mode="HTML"
        )

# ==================== ВАЛИДАЦИЯ ====================
def is_name_valid(name: str) -> Tuple[bool, str]:
    if len(name) < 4:
        return False, "❌ Минимум 4 символа"
    if len(name) > 20:
        return False, "❌ Максимум 20 символов"
    if not re.match(r'^[а-яА-Яa-zA-Z0-9 _\-]+$', name):
        return False, "❌ Только буквы, цифры, пробелы, _ и -"
    return True, "✅ OK"

def is_tag_valid(tag: str) -> Tuple[bool, str]:
    if len(tag) < 2 or len(tag) > 5:
        return False, "❌ Тег от 2 до 5 символов"
    if not tag.isalnum():
        return False, "❌ Только буквы и цифры"
    return True, "✅ OK"

# ==================== БАЗА ДАННЫХ ====================
def get_db():
    return sqlite3.connect(DB_NAME, detect_types=sqlite3.PARSE_DECLTYPES)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    
    # Таблица пользователей
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            register_date TIMESTAMP,
            balance INTEGER DEFAULT 500,
            daily_streak INTEGER DEFAULT 0,
            last_daily TIMESTAMP,
            total_opened INTEGER DEFAULT 0,
            xp INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1,
            total_donated_rub INTEGER DEFAULT 0,
            last_deposit_date TIMESTAMP DEFAULT NULL,
            withdraw_unlocked BOOLEAN DEFAULT FALSE,
            is_banned BOOLEAN DEFAULT FALSE,
            ban_reason TEXT,
            ban_until TIMESTAMP,
            banned_by INTEGER,
            ban_date TIMESTAMP,
            is_admin BOOLEAN DEFAULT FALSE,
            steam_url TEXT,
            steam_id TEXT UNIQUE,
            group_id INTEGER,
            sub_arzdrop BOOLEAN DEFAULT FALSE,
            sub_artstudio BOOLEAN DEFAULT FALSE,
            last_top_notification INTEGER DEFAULT 0,
            agreed_to_terms BOOLEAN DEFAULT FALSE,
            withdraw_allowed BOOLEAN DEFAULT TRUE,
            donator_level INTEGER DEFAULT 0
        )
    ''')
    
    # Инвентарь
    cur.execute('''
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            skin_id INTEGER REFERENCES skins(id),
            acquired_date TIMESTAMP,
            is_tradeable BOOLEAN DEFAULT TRUE
        )
    ''')
    
    # Скины
    cur.execute('''
        CREATE TABLE IF NOT EXISTS skins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            weapon TEXT,
            rarity TEXT,
            wear TEXT,
            float_value REAL,
            price_rub INTEGER,
            price_usd REAL,
            collection TEXT
        )
    ''')
    
    # Кейсы
    cur.execute('''
        CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            price_open INTEGER,
            description TEXT
        )
    ''')
    
    # Связь кейсов и скинов
    cur.execute('''
        CREATE TABLE IF NOT EXISTS case_skins (
            case_id INTEGER REFERENCES cases(id),
            skin_id INTEGER REFERENCES skins(id),
            chance REAL,
            PRIMARY KEY (case_id, skin_id)
        )
    ''')
    
    # Промокоды
    cur.execute('''
        CREATE TABLE IF NOT EXISTS promocodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            reward INTEGER,
            uses_limit INTEGER,
            used_count INTEGER DEFAULT 0,
            expires_at TIMESTAMP,
            created_by INTEGER REFERENCES users(id),
            category TEXT DEFAULT 'coins',
            case_id INTEGER DEFAULT NULL
        )
    ''')
    
    # Использование промокодов
    cur.execute('''
        CREATE TABLE IF NOT EXISTS promocode_uses (
            user_id INTEGER REFERENCES users(id),
            promocode_id INTEGER REFERENCES promocodes(id),
            used_at TIMESTAMP,
            PRIMARY KEY (user_id, promocode_id)
        )
    ''')
    
    # Кланы
    cur.execute('''
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            tag TEXT,
            creator_id INTEGER REFERENCES users(id),
            max_members INTEGER DEFAULT 5,
            created_at TIMESTAMP,
            deleted BOOLEAN DEFAULT FALSE,
            is_admin_clan BOOLEAN DEFAULT FALSE
        )
    ''')
    
    # Участники кланов
    cur.execute('''
        CREATE TABLE IF NOT EXISTS group_members (
            group_id INTEGER REFERENCES groups(id),
            user_id INTEGER REFERENCES users(id),
            joined_at TIMESTAMP,
            rank TEXT DEFAULT 'MEMBER',
            PRIMARY KEY (group_id, user_id)
        )
    ''')
    
    # Друзья
    cur.execute('''
        CREATE TABLE IF NOT EXISTS friends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            friend_id INTEGER REFERENCES users(id),
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP,
            accepted_at TIMESTAMP
        )
    ''')
    
    # Заявки на вывод
    cur.execute('''
        CREATE TABLE IF NOT EXISTS withdraw_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            skin_id INTEGER REFERENCES skins(id),
            inventory_id INTEGER REFERENCES inventory(id),
            steam_url TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP,
            processed_at TIMESTAMP,
            admin_note TEXT,
            admin_msg_id INTEGER DEFAULT NULL
        )
    ''')
    
    # Настройки
    cur.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    
    # Депозиты
    cur.execute('''
        CREATE TABLE IF NOT EXISTS deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            amount REAL,
            method TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP,
            completed_at TIMESTAMP,
            admin_note TEXT
        )
    ''')
    
    # Заявки в клан
    cur.execute('''
        CREATE TABLE IF NOT EXISTS clan_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clan_id INTEGER REFERENCES groups(id),
            user_id INTEGER REFERENCES users(id),
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP,
            processed_at TIMESTAMP
        )
    ''')
    
    conn.commit()
    
    # Создаём админа
    cur.execute('SELECT id FROM users WHERE id = ?', (ADMIN_ID,))
    if not cur.fetchone():
        cur.execute('''
            INSERT INTO users (id, first_name, register_date, last_daily, balance, is_admin, withdraw_unlocked, agreed_to_terms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (ADMIN_ID, "Admin", datetime.now(), datetime.now() - timedelta(days=1), 0, 1, 1, 1))
        conn.commit()
    else:
        cur.execute('UPDATE users SET is_admin = 1 WHERE id = ?', (ADMIN_ID,))
        conn.commit()
    
    conn.close()
    print("✅ База данных инициализирована")

# ==================== ПОЛУЧЕНИЕ ДАННЫХ ====================
def get_user(user_id: int) -> Optional[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM users WHERE id = ?', (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        columns = ['id', 'username', 'first_name', 'register_date', 'balance', 'daily_streak',
                   'last_daily', 'total_opened', 'xp', 'level', 'total_donated_rub',
                   'last_deposit_date', 'withdraw_unlocked', 'is_banned', 'ban_reason',
                   'ban_until', 'banned_by', 'ban_date', 'is_admin',
                   'steam_url', 'steam_id', 'group_id', 'sub_arzdrop', 'sub_artstudio',
                   'last_top_notification', 'agreed_to_terms', 'withdraw_allowed', 'donator_level']
        return dict(zip(columns, row))
    return None

def create_user(user_id: int, username: str = None, first_name: str = None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id FROM users WHERE id = ?', (user_id,))
    if cur.fetchone():
        conn.close()
        return
    
    is_admin = 1 if user_id == ADMIN_ID else 0
    cur.execute('''
        INSERT INTO users (id, username, first_name, register_date, last_daily, balance, is_admin, withdraw_unlocked, agreed_to_terms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, username, first_name, datetime.now(), datetime.now() - timedelta(days=1), 500, is_admin, 1 if is_admin else 0, 0))
    conn.commit()
    conn.close()

def update_balance(user_id: int, amount: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE users SET balance = balance + ? WHERE id = ?', (amount, user_id))
    conn.commit()
    conn.close()

def get_balance(user_id: int) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT balance FROM users WHERE id = ?', (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0

def get_daily_reward(streak: int) -> int:
    if streak == 0:
        return 50
    return 50 * (2 ** streak)

def claim_daily(user_id: int) -> Tuple[int, int]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT daily_streak, last_daily FROM users WHERE id = ?', (user_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return 0, 0
    
    streak, last_daily = row
    if last_daily:
        last_daily = datetime.fromisoformat(last_daily)
    else:
        last_daily = datetime.now() - timedelta(days=1)
    
    now = datetime.now()
    
    if last_daily.date() == now.date():
        conn.close()
        return 0, 0
    
    if last_daily.date() == (now - timedelta(days=1)).date():
        streak += 1
    else:
        streak = 0
    
    reward = get_daily_reward(streak)
    cur.execute('UPDATE users SET balance = balance + ?, daily_streak = ?, last_daily = ? WHERE id = ?',
                (reward, streak, now, user_id))
    conn.commit()
    conn.close()
    return reward, streak

def get_inventory(user_id: int) -> List[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        SELECT s.*, i.id as inv_id, i.acquired_date
        FROM inventory i
        JOIN skins s ON i.skin_id = s.id
        WHERE i.user_id = ?
        ORDER BY i.acquired_date DESC
    ''', (user_id,))
    rows = cur.fetchall()
    conn.close()
    columns = ['id', 'name', 'weapon', 'rarity', 'wear', 'float_value', 'price_rub',
               'price_usd', 'collection', 'inv_id', 'acquired_date']
    return [dict(zip(columns, row)) for row in rows]

def get_inventory_value(user_id: int) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT SUM(s.price_rub) FROM inventory i JOIN skins s ON i.skin_id = s.id WHERE i.user_id = ?',
                (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row[0] else 0

def get_xp_for_level(level: int) -> int:
    levels = {
        1: 0, 2: 250, 3: 500, 4: 1000, 5: 2830,
        6: 4000, 7: 5500, 8: 7500, 9: 10000, 10: 13912,
        15: 35000, 20: 64011, 30: 150000, 40: 280000,
        50: 461462, 75: 1200000, 100: 2027598
    }
    if level in levels:
        return levels[level]
    return int(50 * (level ** 2.8))

def check_level_up(user_id: int) -> Tuple[bool, int, int]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT xp, level FROM users WHERE id = ?', (user_id,))
    xp, current_level = cur.fetchone()
    
    new_level = current_level
    while True:
        next_level_xp = get_xp_for_level(new_level + 1)
        if xp >= next_level_xp and next_level_xp > 0:
            new_level += 1
        else:
            break
    
    if new_level > current_level:
        level_rewards = {
            2: 500, 5: 3500, 10: 24500, 20: 171500, 50: 1200500, 100: 8403500
        }
        reward = level_rewards.get(new_level, 0)
        
        cur.execute('UPDATE users SET level = ? WHERE id = ?', (new_level, user_id))
        if reward > 0:
            cur.execute('UPDATE users SET balance = balance + ? WHERE id = ?', (reward, user_id))
        conn.commit()
        conn.close()
        return True, new_level, reward
    
    conn.close()
    return False, current_level, 0

def add_xp(user_id: int, amount: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE users SET xp = xp + ? WHERE id = ?', (amount, user_id))
    conn.commit()
    conn.close()
    return check_level_up(user_id)

def open_case(user_id: int, case_name: str) -> Optional[Dict]:
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('SELECT id, price_open FROM cases WHERE name = ?', (case_name,))
    case = cur.fetchone()
    if not case:
        conn.close()
        return None
    
    case_id, price = case
    
    cur.execute('SELECT balance FROM users WHERE id = ?', (user_id,))
    balance = cur.fetchone()[0]
    if balance < price:
        conn.close()
        return None
    
    cur.execute('UPDATE users SET balance = balance - ? WHERE id = ?', (price, user_id))
    cur.execute('''
        SELECT s.*, cs.chance
        FROM case_skins cs
        JOIN skins s ON cs.skin_id = s.id
        WHERE cs.case_id = ?
    ''', (case_id,))
    skins = cur.fetchall()
    weights = [skin[5] for skin in skins]
    selected = random.choices(skins, weights=weights, k=1)[0]
    
    skin_id = selected[0]
    cur.execute('INSERT INTO inventory (user_id, skin_id, acquired_date) VALUES (?, ?, ?)',
                (user_id, skin_id, datetime.now()))
    cur.execute('UPDATE users SET total_opened = total_opened + 1, xp = xp + 1 WHERE id = ?', (user_id,))
    
    xp_amount = 1 if price <= 500 else 11 if price <= 1500 else 36
    cur.execute('UPDATE users SET xp = xp + ? WHERE id = ?', (xp_amount, user_id))
    
    conn.commit()
    conn.close()
    
    columns = ['id', 'name', 'weapon', 'rarity', 'wear', 'float_value', 'price_rub',
               'price_usd', 'collection', 'chance']
    return dict(zip(columns, selected))

def get_all_users() -> List[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM users WHERE is_banned = FALSE')
    rows = cur.fetchall()
    conn.close()
    columns = ['id', 'username', 'first_name', 'register_date', 'balance', 'daily_streak',
               'last_daily', 'total_opened', 'xp', 'level', 'total_donated_rub',
               'last_deposit_date', 'withdraw_unlocked', 'is_banned', 'ban_reason',
               'ban_until', 'banned_by', 'ban_date', 'is_admin',
               'steam_url', 'steam_id', 'group_id', 'sub_arzdrop', 'sub_artstudio',
               'last_top_notification', 'agreed_to_terms', 'withdraw_allowed', 'donator_level']
    return [dict(zip(columns, row)) for row in rows]

def get_cases_list() -> List[Dict]:
    """Получить список всех кейсов"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id, name, price_open, description FROM cases')
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "name": r[1], "price": r[2], "description": r[3]} for r in rows]

# ==================== ТОПЫ ====================
def get_top_users() -> List[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        SELECT u.id, u.first_name, u.balance, u.total_opened, g.tag
        FROM users u
        LEFT JOIN groups g ON u.group_id = g.id
        WHERE u.is_banned = FALSE AND u.is_admin = FALSE
        ORDER BY u.balance DESC
        LIMIT 10
    ''')
    rows = cur.fetchall()
    conn.close()
    
    result = []
    for row in rows:
        result.append({
            'id': row[0],
            'first_name': row[1],
            'balance': row[2],
            'total_opened': row[3],
            'tag': row[4]
        })
    return result

def get_user_place(user_id: int) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        SELECT COUNT(*) + 1 FROM users 
        WHERE balance > (SELECT balance FROM users WHERE id = ?) 
        AND is_admin = FALSE AND is_banned = FALSE
    ''', (user_id,))
    place = cur.fetchone()[0]
    conn.close()
    return place

# ==================== БАН ====================
def is_user_banned(user_id: int) -> Tuple[bool, str]:
    user = get_user(user_id)
    if not user:
        return False, ""
    
    if not user.get('is_banned', False):
        return False, ""
    
    ban_until = user.get('ban_until')
    if ban_until:
        ban_until = datetime.fromisoformat(ban_until)
        if datetime.now() > ban_until:
            conn = get_db()
            cur = conn.cursor()
            cur.execute('''
                UPDATE users 
                SET is_banned = FALSE, ban_reason = NULL, ban_until = NULL, banned_by = NULL, ban_date = NULL
                WHERE id = ?
            ''', (user_id,))
            conn.commit()
            conn.close()
            return False, ""
    
    reason = user.get('ban_reason', 'Нарушение правил')
    return True, reason

def can_withdraw(user_id: int) -> Tuple[bool, str]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT withdraw_unlocked, last_deposit_date, withdraw_allowed FROM users WHERE id = ?', (user_id,))
    row = cur.fetchone()
    conn.close()
    
    if not row:
        return False, "Пользователь не найден"
    
    unlocked, last_date, allowed = row
    
    if not allowed:
        return False, "❌ Вывод запрещён администратором."
    
    if not unlocked:
        return False, "❌ Вывод заблокирован. Внеси минимум 230₽ для разблокировки."
    
    if not last_date:
        return False, "❌ Ошибка: дата последнего пополнения не найдена."
    
    last_date = datetime.fromisoformat(last_date)
    days_since = (datetime.now() - last_date).days
    
    if days_since > 90:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('UPDATE users SET withdraw_unlocked = FALSE WHERE id = ?', (user_id,))
        conn.commit()
        conn.close()
        return False, "❌ Вывод заблокирован. Прошло более 3 месяцев с последнего пополнения. Внеси минимум 115₽ для возобновления."
    
    if days_since > 60:
        return True, f"⚠️ Через {90 - days_since} дней вывод будет заблокирован. Пополни на 115₽ для продления."
    
    return True, "✅ Вывод доступен"

def sell_skin(user_id: int, inventory_id: int) -> Tuple[bool, str]:
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('SELECT skin_id FROM inventory WHERE id = ? AND user_id = ?', (inventory_id, user_id))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False, "❌ Скин не найден"
    
    skin_id = row[0]
    cur.execute('SELECT price_rub FROM skins WHERE id = ?', (skin_id,))
    price = cur.fetchone()[0]
    
    sell_price = int(price * SELL_PRICE_PERCENT / 100)
    
    cur.execute('DELETE FROM inventory WHERE id = ?', (inventory_id,))
    cur.execute('UPDATE users SET balance = balance + ? WHERE id = ?', (sell_price, user_id))
    conn.commit()
    conn.close()
    
    return True, f"✅ Скин продан за {sell_price} монет ({SELL_PRICE_PERCENT}% от стоимости)"

# ==================== ДРУЗЬЯ И КЛАНЫ ====================
def send_friend_request(from_user: int, to_user: int) -> bool:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id FROM friends WHERE user_id = ? AND friend_id = ?', (from_user, to_user))
    if cur.fetchone():
        conn.close()
        return False
    cur.execute('INSERT INTO friends (user_id, friend_id, created_at) VALUES (?, ?, ?)',
                (from_user, to_user, datetime.now()))
    conn.commit()
    conn.close()
    return True

def get_friend_requests(user_id: int) -> List[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        SELECT f.*, u.first_name, u.username
        FROM friends f
        JOIN users u ON f.user_id = u.id
        WHERE f.friend_id = ? AND f.status = 'pending'
    ''', (user_id,))
    rows = cur.fetchall()
    conn.close()
    columns = ['id', 'user_id', 'friend_id', 'status', 'created_at', 'accepted_at', 'first_name', 'username']
    return [dict(zip(columns, row)) for row in rows]

def accept_friend_request(request_id: int) -> bool:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE friends SET status = "accepted", accepted_at = ? WHERE id = ? AND status = "pending"',
                (datetime.now(), request_id))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def get_friends(user_id: int) -> List[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        SELECT u.id, u.first_name, u.username, u.balance, f.accepted_at
        FROM friends f
        JOIN users u ON (u.id = f.user_id OR u.id = f.friend_id)
        WHERE (f.user_id = ? OR f.friend_id = ?) AND f.status = "accepted" AND u.id != ?
    ''', (user_id, user_id, user_id))
    rows = cur.fetchall()
    conn.close()
    columns = ['id', 'first_name', 'username', 'balance', 'accepted_at']
    return [dict(zip(columns, row)) for row in rows]

def get_user_rank(user_id: int, group_id: int) -> Optional[str]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT rank FROM group_members WHERE group_id = ? AND user_id = ?', (group_id, user_id))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def can_manage_members(user_id: int, group_id: int) -> bool:
    rank = get_user_rank(user_id, group_id)
    return rank in ['LIDER', 'ZAM']

def can_kick(user_id: int, group_id: int) -> bool:
    rank = get_user_rank(user_id, group_id)
    return rank in ['LIDER', 'ZAM']

def can_assign_rank(user_id: int, group_id: int, target_rank: str) -> bool:
    rank = get_user_rank(user_id, group_id)
    if rank == 'LIDER':
        return True
    if rank == 'ZAM' and target_rank in ['ENTRY', 'MEMBER']:
        return True
    return False

def can_delete_clan(user_id: int, group_id: int) -> bool:
    return get_user_rank(user_id, group_id) == 'LIDER'

def can_transfer_leadership(user_id: int, group_id: int) -> bool:
    return get_user_rank(user_id, group_id) == 'LIDER'

def create_clan(name: str, tag: str, creator_id: int) -> Tuple[bool, str, Optional[int]]:
    valid, msg = is_name_valid(name)
    if not valid:
        return False, msg, None
    valid, msg = is_tag_valid(tag)
    if not valid:
        return False, msg, None
    
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id FROM groups WHERE name = ? AND deleted = FALSE', (name,))
    if cur.fetchone():
        conn.close()
        return False, "Клан с таким названием уже существует", None
    
    cur.execute('SELECT id FROM groups WHERE tag = ? AND deleted = FALSE', (tag,))
    if cur.fetchone():
        conn.close()
        return False, "Клан с таким тегом уже существует", None
    
    cur.execute('SELECT balance FROM users WHERE id = ?', (creator_id,))
    balance = cur.fetchone()[0]
    if balance < 100000:
        conn.close()
        return False, "Недостаточно монет! Нужно 100 000", None
    
    is_admin_clan = 1 if creator_id == ADMIN_ID else 0
    
    cur.execute('''
        INSERT INTO groups (name, tag, creator_id, max_members, created_at, is_admin_clan)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (name, tag, creator_id, 5, datetime.now(), is_admin_clan))
    group_id = cur.lastrowid
    
    cur.execute('INSERT INTO group_members (group_id, user_id, joined_at, rank) VALUES (?, ?, ?, ?)',
                (group_id, creator_id, datetime.now(), 'LIDER'))
    
    if is_admin_clan:
        cur.execute('UPDATE users SET is_admin = 1, group_id = ? WHERE id = ?', (group_id, creator_id))
    else:
        cur.execute('UPDATE users SET group_id = ? WHERE id = ?', (group_id, creator_id))
    
    cur.execute('UPDATE users SET balance = balance - 100000 WHERE id = ?', (creator_id,))
    conn.commit()
    conn.close()
    
    return True, "Клан создан!", group_id

def upgrade_clan(creator_id: int, new_limit: int, cost: int) -> Tuple[bool, str]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT balance FROM users WHERE id = ?', (creator_id,))
    balance = cur.fetchone()[0]
    if balance < cost:
        conn.close()
        return False, f"Недостаточно монет! Нужно {cost}"
    
    cur.execute('UPDATE groups SET max_members = ? WHERE creator_id = ? AND deleted = FALSE', (new_limit, creator_id))
    cur.execute('UPDATE users SET balance = balance - ? WHERE id = ?', (cost, creator_id))
    conn.commit()
    conn.close()
    return True, f"Клан улучшен до {new_limit} мест!"

def get_my_clan(user_id: int) -> Optional[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        SELECT g.*, COUNT(gm.user_id) as members_count
        FROM groups g
        LEFT JOIN group_members gm ON g.id = gm.group_id
        WHERE g.creator_id = ? AND g.deleted = FALSE
        GROUP BY g.id
    ''', (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        columns = ['id', 'name', 'tag', 'creator_id', 'max_members', 'created_at', 'deleted', 'members_count', 'is_admin_clan']
        return dict(zip(columns, row))
    return None

def get_clan_members(group_id: int) -> List[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        SELECT u.id, u.first_name, u.username, u.balance, gm.rank, gm.joined_at
        FROM group_members gm
        JOIN users u ON gm.user_id = u.id
        WHERE gm.group_id = ?
    ''', (group_id,))
    rows = cur.fetchall()
    conn.close()
    columns = ['id', 'first_name', 'username', 'balance', 'rank', 'joined_at']
    return [dict(zip(columns, row)) for row in rows]

def join_clan(user_id: int, group_id: int):
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('SELECT is_admin_clan FROM groups WHERE id = ? AND deleted = FALSE', (group_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False
    
    is_admin_clan = row[0]
    
    cur.execute('INSERT INTO group_members (group_id, user_id, joined_at, rank) VALUES (?, ?, ?, ?)',
                (group_id, user_id, datetime.now(), 'MEMBER'))
    
    if is_admin_clan:
        cur.execute('UPDATE users SET is_admin = 1, group_id = ? WHERE id = ?', (group_id, user_id))
    else:
        cur.execute('UPDATE users SET group_id = ? WHERE id = ?', (group_id, user_id))
    
    conn.commit()
    conn.close()
    return True

def delete_clan_by_admin(group_id: int) -> bool:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT name FROM groups WHERE id = ? AND deleted = FALSE', (group_id,))
    if not cur.fetchone():
        conn.close()
        return False
    cur.execute('UPDATE groups SET deleted = TRUE WHERE id = ?', (group_id,))
    cur.execute('DELETE FROM group_members WHERE group_id = ?', (group_id,))
    cur.execute('UPDATE users SET group_id = NULL WHERE group_id = ?', (group_id,))
    conn.commit()
    conn.close()
    return True

def get_all_clans() -> List[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        SELECT g.*, COUNT(gm.user_id) as members_count, u.first_name as creator_name
        FROM groups g
        LEFT JOIN group_members gm ON g.id = gm.group_id
        LEFT JOIN users u ON g.creator_id = u.id
        WHERE g.deleted = FALSE
        GROUP BY g.id
        ORDER BY g.created_at DESC
    ''')
    rows = cur.fetchall()
    conn.close()
    columns = ['id', 'name', 'tag', 'creator_id', 'max_members', 'created_at', 'deleted', 'members_count', 'creator_name', 'is_admin_clan']
    return [dict(zip(columns, row)) for row in rows]

def get_top_clans() -> List[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        SELECT g.id, g.name, g.tag, g.creator_id, COUNT(gm.user_id) as members_count,
               COALESCE(AVG(u.balance), 0) as avg_balance,
               COALESCE(SUM(u.balance), 0) as total_balance
        FROM groups g
        LEFT JOIN group_members gm ON g.id = gm.group_id
        LEFT JOIN users u ON gm.user_id = u.id
        WHERE g.deleted = FALSE AND u.is_banned = FALSE
        GROUP BY g.id
        HAVING members_count > 0
        ORDER BY avg_balance DESC
        LIMIT 10
    ''')
    rows = cur.fetchall()
    conn.close()
    columns = ['id', 'name', 'tag', 'creator_id', 'members_count', 'avg_balance', 'total_balance']
    return [dict(zip(columns, row)) for row in rows]

# ==================== ПРОМОКОДЫ ====================
def get_promocode(code: str) -> Optional[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM promocodes WHERE code = ?', (code,))
    row = cur.fetchone()
    conn.close()
    if row:
        columns = ['id', 'code', 'reward', 'uses_limit', 'used_count', 'expires_at', 'created_by', 'category', 'case_id']
        return dict(zip(columns, row))
    return None

def use_promocode(user_id: int, code: str) -> Tuple[bool, str]:
    conn = get_db()
    cur = conn.cursor()
    promo = get_promocode(code)
    if not promo:
        conn.close()
        return False, "❌ Промокод не найден"
    
    if promo['expires_at']:
        expires = datetime.fromisoformat(promo['expires_at'])
        if expires < datetime.now():
            conn.close()
            return False, "❌ Промокод истёк"
    
    if promo['uses_limit'] and promo['used_count'] >= promo['uses_limit']:
        conn.close()
        return False, "❌ Промокод уже использован"
    
    cur.execute('SELECT * FROM promocode_uses WHERE user_id = ? AND promocode_id = ?',
                (user_id, promo['id']))
    if cur.fetchone():
        conn.close()
        return False, "❌ Вы уже использовали этот промокод"
    
    category = promo.get('category', 'coins')
    
    if category == 'coins':
        cur.execute('UPDATE users SET balance = balance + ? WHERE id = ?', (promo['reward'], user_id))
        result_msg = f"+{promo['reward']} монет"
    
    elif category == 'cases':
        case_id = promo.get('case_id')
        if not case_id:
            conn.close()
            return False, "❌ Ошибка: кейс не указан"
        
        cur.execute('SELECT name, price_open FROM cases WHERE id = ?', (case_id,))
        case = cur.fetchone()
        if not case:
            conn.close()
            return False, "❌ Кейс не найден"
        
        case_name, case_price = case
        
        cur.execute('''
            SELECT s.*, cs.chance
            FROM case_skins cs
            JOIN skins s ON cs.skin_id = s.id
            WHERE cs.case_id = ?
        ''', (case_id,))
        skins = cur.fetchall()
        weights = [skin[5] for skin in skins]
        selected = random.choices(skins, weights=weights, k=1)[0]
        
        skin_id = selected[0]
        cur.execute('INSERT INTO inventory (user_id, skin_id, acquired_date) VALUES (?, ?, ?)',
                    (user_id, skin_id, datetime.now()))
        cur.execute('UPDATE users SET total_opened = total_opened + 1, xp = xp + 1 WHERE id = ?', (user_id,))
        
        skin_name = selected[1]
        weapon = selected[2]
        result_msg = f"🎁 {weapon} | {skin_name} из кейса {case_name}"
    
    else:
        conn.close()
        return False, "❌ Неизвестная категория промокода"
    
    cur.execute('UPDATE promocodes SET used_count = used_count + 1 WHERE id = ?', (promo['id'],))
    cur.execute('INSERT INTO promocode_uses (user_id, promocode_id, used_at) VALUES (?, ?, ?)',
                (user_id, promo['id'], datetime.now()))
    conn.commit()
    conn.close()
    return True, result_msg

def create_promocode(code: str, reward: int, uses_limit: int, expires_days: int, admin_id: int, category: str = "coins", case_id: int = None) -> bool:
    conn = get_db()
    cur = conn.cursor()
    expires_at = datetime.now() + timedelta(days=expires_days) if expires_days > 0 else None
    try:
        cur.execute('''
            INSERT INTO promocodes (code, reward, uses_limit, expires_at, created_by, category, case_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (code, reward, uses_limit, expires_at, admin_id, category, case_id))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False

def delete_promocode(code: str) -> bool:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM promocodes WHERE code = ?', (code,))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def get_all_promocodes() -> List[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM promocodes ORDER BY id DESC')
    rows = cur.fetchall()
    conn.close()
    columns = ['id', 'code', 'reward', 'uses_limit', 'used_count', 'expires_at', 'created_by', 'category', 'case_id']
    return [dict(zip(columns, row)) for row in rows]

# ==================== STEAM ====================
def extract_steam_id(steam_url: str) -> Optional[str]:
    trade_match = re.search(r'partner=(\d+)', steam_url)
    if trade_match:
        return trade_match.group(1)
    
    profile_match = re.search(r'/profiles/(\d+)', steam_url)
    if profile_match:
        return profile_match.group(1)
    
    return None

def link_steam(user_id: int, steam_url: str) -> Tuple[bool, str]:
    steam_id = extract_steam_id(steam_url)
    if not steam_id:
        return False, "❌ Не удалось получить ID Steam.\nПоддерживаются ссылки:\n• https://steamcommunity.com/profiles/765611...\n• https://steamcommunity.com/tradeoffer/new/?partner=...&token=..."
    
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id FROM users WHERE steam_id = ? AND id != ?', (steam_id, user_id))
    if cur.fetchone():
        conn.close()
        return False, "❌ Этот Steam уже привязан к другому аккаунту"
    
    cur.execute('UPDATE users SET steam_url = ?, steam_id = ? WHERE id = ?', (steam_url, steam_id, user_id))
    conn.commit()
    conn.close()
    return True, "✅ Steam успешно привязан!"

def unlink_steam(user_id: int) -> bool:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE users SET steam_url = NULL, steam_id = NULL WHERE id = ?', (user_id,))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0

# ==================== ВЫВОД ====================
def create_withdraw_request(user_id: int, inventory_id: int, skin_id: int, steam_url: str) -> Tuple[int, Optional[int]]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO withdraw_requests (user_id, skin_id, inventory_id, steam_url, created_at)
        VALUES (?, ?, ?, ?, ?)
    ''', (user_id, skin_id, inventory_id, steam_url, datetime.now()))
    request_id = cur.lastrowid
    conn.commit()
    conn.close()
    
    user = get_user(user_id)
    skin = get_skin(skin_id)
    
    msg = send_message(
        ADMIN_ID,
        f"<b>📢 НОВАЯ ЗАЯВКА НА ВЫВОД!</b>\n\n"
        f"🆔 Заявка: #{request_id}\n"
        f"👤 Пользователь: {user_id} ({user.get('first_name', 'User')})\n"
        f"🎯 Скин: {skin.get('weapon', '')} | {skin.get('name', '')}\n"
        f"💰 Цена: {skin.get('price_rub', 0)} монет (${skin.get('price_usd', 0)})\n"
        f"🔗 Steam: {steam_url}\n\n"
        f"⏳ Ожидает обработки",
        parse_mode="HTML"
    )
    
    if msg and msg.get('ok'):
        message_id = msg.get('result', {}).get('message_id')
        if message_id:
            pin_message(ADMIN_ID, message_id)
            conn = get_db()
            cur = conn.cursor()
            cur.execute('UPDATE withdraw_requests SET admin_msg_id = ? WHERE id = ?', (message_id, request_id))
            conn.commit()
            conn.close()
            return request_id, message_id
    
    return request_id, None

def get_pending_withdrawals() -> List[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        SELECT wr.*, s.name, s.weapon, u.first_name, u.username, u.steam_url as user_steam
        FROM withdraw_requests wr
        JOIN skins s ON wr.skin_id = s.id
        JOIN users u ON wr.user_id = u.id
        WHERE wr.status = 'pending'
        ORDER BY wr.created_at ASC
    ''')
    rows = cur.fetchall()
    conn.close()
    columns = ['id', 'user_id', 'skin_id', 'inventory_id', 'steam_url', 'status',
               'created_at', 'processed_at', 'admin_note', 'name', 'weapon', 'first_name', 'username', 'user_steam', 'admin_msg_id']
    return [dict(zip(columns, row)) for row in rows]

def process_withdraw(request_id: int, action: str) -> bool:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT user_id, inventory_id, admin_msg_id FROM withdraw_requests WHERE id = ? AND status = "pending"',
                (request_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False
    
    user_id, inv_id, admin_msg_id = row
    
    if action == 'approve':
        cur.execute('UPDATE withdraw_requests SET status = "approved", processed_at = ? WHERE id = ?',
                    (datetime.now(), request_id))
        cur.execute('DELETE FROM inventory WHERE id = ?', (inv_id,))
    else:
        cur.execute('UPDATE withdraw_requests SET status = "cancelled", processed_at = ? WHERE id = ?',
                    (datetime.now(), request_id))
    
    conn.commit()
    conn.close()
    
    if admin_msg_id:
        unpin_message(ADMIN_ID, admin_msg_id)
    
    return True

def get_skin(skin_id: int) -> Optional[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM skins WHERE id = ?', (skin_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        columns = ['id', 'name', 'weapon', 'rarity', 'wear', 'float_value', 'price_rub', 'price_usd', 'collection']
        return dict(zip(columns, row))
    return None

# ==================== ПОПОЛНЕНИЕ ====================
def add_deposit(user_id: int, rub: float, method: str = "admin_deposit"):
    conn = get_db()
    cur = conn.cursor()
    
    now = datetime.now()
    coins = int(rub * COURSE)
    
    cur.execute('UPDATE users SET balance = balance + ? WHERE id = ?', (coins, user_id))
    cur.execute('UPDATE users SET total_donated_rub = total_donated_rub + ? WHERE id = ?', (rub, user_id))
    
    if rub >= 230:
        cur.execute('UPDATE users SET withdraw_unlocked = TRUE, last_deposit_date = ? WHERE id = ?', (now, user_id))
    else:
        cur.execute('SELECT withdraw_unlocked FROM users WHERE id = ?', (user_id,))
        unlocked = cur.fetchone()[0]
        if unlocked:
            cur.execute('UPDATE users SET last_deposit_date = ? WHERE id = ?', (now, user_id))
    
    xp_map = {
        25000: 5, 50000: 10, 125000: 28, 250000: 60, 625000: 156, 1250000: 312
    }
    xp = 0
    for amount, xp_reward in xp_map.items():
        if rub >= amount:
            xp = xp_reward
    if xp > 0:
        cur.execute('UPDATE users SET xp = xp + ? WHERE id = ?', (xp, user_id))
    
    cur.execute('''
        INSERT INTO deposits (user_id, amount, method, status, created_at, completed_at, admin_note)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, rub, method, "completed", now, now, "Пополнение"))
    
    conn.commit()
    conn.close()
    
    check_donator_achievement(user_id)

# ==================== ПРОВЕРКА ПОДПИСОК ====================
def check_subscription(user_id: int) -> Tuple[bool, bool]:
    try:
        url = BASE_URL + "getChatMember"
        params1 = {"chat_id": CHANNEL_ARZDROP, "user_id": user_id}
        resp1 = requests.get(url, params=params1).json()
        params2 = {"chat_id": CHANNEL_ARTSTUDIO, "user_id": user_id}
        resp2 = requests.get(url, params=params2).json()
        
        sub1 = resp1.get('result', {}).get('status') in ['member', 'administrator', 'creator']
        sub2 = resp2.get('result', {}).get('status') in ['member', 'administrator', 'creator']
        return sub1, sub2
    except:
        return False, False

# ==================== ФУНКЦИИ ОТПРАВКИ ====================
def send_message(chat_id: int, text: str, reply_markup: Dict = None, parse_mode: str = "HTML"):
    url = BASE_URL + "sendMessage"
    params = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        params["reply_markup"] = json.dumps(reply_markup)
    return requests.get(url, params=params).json()

def pin_message(chat_id: int, message_id: int):
    url = BASE_URL + "pinChatMessage"
    params = {"chat_id": chat_id, "message_id": message_id}
    try:
        requests.get(url, params=params)
    except:
        pass

def unpin_message(chat_id: int, message_id: int):
    url = BASE_URL + "unpinChatMessage"
    params = {"chat_id": chat_id, "message_id": message_id}
    try:
        requests.get(url, params=params)
    except:
        pass

# ==================== PYDANTIC МОДЕЛИ ====================
class CaseOpenRequest(BaseModel):
    user_id: int
    case_name: str

class PromoCodeRequest(BaseModel):
    user_id: int
    code: str

class WithdrawRequest(BaseModel):
    user_id: int
    inventory_id: int
    skin_id: int
    steam_url: str

class DepositRequest(BaseModel):
    user_id: int
    amount_rub: float

class ClanCreateRequest(BaseModel):
    user_id: int
    name: str
    tag: str

class FriendRequest(BaseModel):
    user_id: int
    friend_id: int

# ==================== FASTAPI ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Жизненный цикл приложения"""
    logger.info("🚀 Starting ARZDROP Server...")
    
    # Инициализация БД
    init_db()
    logger.info("✅ Database initialized")
    
    yield
    
    logger.info("🛑 Shutting down...")

app = FastAPI(
    title="ARZDROP API",
    description="API для Telegram Mini App ARZDROP",
    version="1.0.0",
    lifespan=lifespan
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== WebSocket ====================
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[int, List[WebSocket]] = {}
    
    async def connect(self, user_id: int, websocket: WebSocket):
        await websocket.accept()
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(websocket)
        logger.info(f"User {user_id} connected. Total: {len(self.active_connections[user_id])}")
    
    def disconnect(self, user_id: int, websocket: WebSocket):
        if user_id in self.active_connections:
            self.active_connections[user_id].remove(websocket)
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]
    
    async def send_to_user(self, user_id: int, message: Dict):
        if user_id in self.active_connections:
            for websocket in self.active_connections[user_id]:
                try:
                    await websocket.send_json(message)
                except:
                    pass
    
    async def broadcast(self, message: Dict):
        for user_id in list(self.active_connections.keys()):
            await self.send_to_user(user_id, message)

manager = ConnectionManager()

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    await manager.connect(user_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                json_data = json.loads(data)
                event = json_data.get('event')
                
                if event == 'open_case':
                    case_name = json_data.get('case_name')
                    result = open_case(user_id, case_name)
                    if result:
                        await manager.send_to_user(user_id, {
                            'event': 'case_opened',
                            'data': result,
                            'new_balance': get_balance(user_id)
                        })
                    else:
                        await manager.send_to_user(user_id, {
                            'event': 'error',
                            'message': 'Недостаточно монет'
                        })
                
                elif event == 'get_balance':
                    await manager.send_to_user(user_id, {
                        'event': 'balance',
                        'balance': get_balance(user_id)
                    })
                
                elif event == 'claim_daily':
                    reward, streak = claim_daily(user_id)
                    if reward > 0:
                        await manager.send_to_user(user_id, {
                            'event': 'daily_claimed',
                            'reward': reward,
                            'streak': streak,
                            'new_balance': get_balance(user_id)
                        })
                    else:
                        await manager.send_to_user(user_id, {
                            'event': 'error',
                            'message': 'Уже получено сегодня'
                        })
                
            except json.JSONDecodeError:
                await manager.send_to_user(user_id, {
                    'event': 'error',
                    'message': 'Invalid JSON'
                })
                
    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)
        logger.info(f"User {user_id} disconnected")

# ==================== ГЛАВНАЯ СТРАНИЦА (ФРОНТ) ====================

@app.get("/")
async def serve_index():
    """Главная страница Mini App"""
    return FileResponse("index.html")

# ==================== API ЭНДПОИНТЫ ====================

@app.get("/api")
async def api_root():
    """Список всех API эндпоинтов"""
    return {
        "name": "ARZDROP API",
        "version": "1.0.0",
        "status": "online",
        "endpoints": [
            "/api/user/{user_id}",
            "/api/inventory/{user_id}",
            "/api/cases",
            "/api/case/open",
            "/api/promo/use",
            "/api/withdraw/request",
            "/api/deposit",
            "/api/clan/create",
            "/api/top",
            "/api/top/clans",
            "/api/balance/{user_id}",
            "/api/daily/{user_id}",
            "/api/daily/claim",
            "/api/friends/send",
            "/api/friends/{user_id}",
            "/api/friends/requests/{user_id}",
            "/api/friends/accept/{request_id}",
            "/api/admin/give",
            "/api/admin/withdraw/process",
            "/api/admin/withdrawals",
            "/api/clan/{user_id}",
            "/api/clan/join",
            "/api/skin/sell",
            "/api/steam/link",
            "/api/steam/unlink",
            "/api/config"
        ]
    }

@app.get("/api/user/{user_id}")
async def get_user_data(user_id: int):
    """Получение данных пользователя"""
    user = get_user(user_id)
    if not user:
        # Создаём пользователя если его нет
        create_user(user_id)
        user = get_user(user_id)
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Проверка бана
    banned, reason = is_user_banned(user_id)
    
    return {
        "id": user["id"],
        "first_name": user["first_name"],
        "username": user.get("username"),
        "balance": user["balance"],
        "level": user.get("level", 1),
        "xp": user.get("xp", 0),
        "total_opened": user.get("total_opened", 0),
        "daily_streak": user.get("daily_streak", 0),
        "is_admin": user.get("is_admin", False),
        "group_id": user.get("group_id"),
        "steam_url": user.get("steam_url"),
        "steam_id": user.get("steam_id"),
        "withdraw_unlocked": user.get("withdraw_unlocked", False),
        "is_banned": banned,
        "ban_reason": reason if banned else None,
        "total_donated_rub": user.get("total_donated_rub", 0),
        "donator_level": user.get("donator_level", 0)
    }

@app.get("/api/inventory/{user_id}")
async def get_user_inventory(user_id: int, limit: int = 50, offset: int = 0):
    """Получение инвентаря с пагинацией"""
    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    inventory = get_inventory(user_id)
    total = len(inventory)
    
    # Пагинация
    paginated = inventory[offset:offset + limit]
    
    return {
        "items": paginated,
        "total": total,
        "limit": limit,
        "offset": offset,
        "value": get_inventory_value(user_id)
    }

@app.get("/api/cases")
async def get_cases():
    """Получить список всех кейсов"""
    cases = get_cases_list()
    return {"cases": cases}

@app.post("/api/case/open")
async def open_case_endpoint(request: CaseOpenRequest):
    """Открытие кейса"""
    user = get_user(request.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    banned, reason = is_user_banned(request.user_id)
    if banned:
        raise HTTPException(status_code=403, detail=f"User is banned: {reason}")
    
    result = open_case(request.user_id, request.case_name)
    if result is None:
        raise HTTPException(status_code=400, detail="Insufficient balance or case not found")
    
    # Проверка повышения уровня
    leveled, new_level, reward = check_level_up(request.user_id)
    
    return {
        "success": True,
        "skin": result,
        "new_balance": get_balance(request.user_id),
        "level_up": {
            "occurred": leveled,
            "new_level": new_level if leveled else None,
            "reward": reward if leveled else 0
        }
    }

@app.post("/api/promo/use")
async def use_promo_endpoint(request: PromoCodeRequest):
    """Использование промокода"""
    success, msg = use_promocode(request.user_id, request.code)
    
    return {
        "success": success,
        "message": msg,
        "new_balance": get_balance(request.user_id) if success else None
    }

@app.post("/api/withdraw/request")
async def withdraw_request(request: WithdrawRequest):
    """Запрос на вывод скина"""
    user = get_user(request.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    can, msg = can_withdraw(request.user_id)
    if not can:
        raise HTTPException(status_code=400, detail=msg)
    
    # Проверяем, что скин принадлежит пользователю
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM inventory WHERE id = ?', (request.inventory_id,))
    row = cur.fetchone()
    conn.close()
    
    if not row or row[0] != request.user_id:
        raise HTTPException(status_code=403, detail="Skin not found or not yours")
    
    req_id, msg_id = create_withdraw_request(
        request.user_id,
        request.inventory_id,
        request.skin_id,
        request.steam_url
    )
    
    return {
        "success": True,
        "request_id": req_id,
        "message": "Заявка на вывод отправлена"
    }

@app.post("/api/deposit")
async def deposit_request(request: DepositRequest):
    """Пополнение баланса"""
    user = get_user(request.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    return {
        "success": True,
        "message": "Запрос на пополнение создан",
        "amount_coins": int(request.amount_rub * COURSE),
        "course": COURSE
    }

@app.post("/api/clan/create")
async def create_clan_endpoint(request: ClanCreateRequest):
    """Создание клана"""
    success, msg, group_id = create_clan(
        request.name,
        request.tag,
        request.user_id
    )
    
    return {
        "success": success,
        "message": msg,
        "group_id": group_id
    }

@app.get("/api/top")
async def get_top(limit: int = 10):
    """Топ игроков"""
    top_users = get_top_users()
    return {"top": top_users[:limit]}

@app.get("/api/top/clans")
async def get_top_clans(limit: int = 10):
    """Топ кланов"""
    top_clans = get_top_clans()
    return {"top": top_clans[:limit]}

@app.get("/api/balance/{user_id}")
async def get_balance_endpoint(user_id: int):
    """Получение баланса"""
    balance = get_balance(user_id)
    return {"balance": balance}

@app.get("/api/daily/{user_id}")
async def get_daily_info(user_id: int):
    """Информация о ежедневной награде"""
    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    last_daily = user.get('last_daily')
    if last_daily:
        last_daily = datetime.fromisoformat(last_daily)
    else:
        last_daily = datetime.now() - timedelta(days=1)
    
    now = datetime.now()
    can_claim = last_daily.date() != now.date()
    next_reward = 50
    
    if can_claim:
        if last_daily.date() == (now - timedelta(days=1)).date():
            next_reward = get_daily_reward(user['daily_streak'] + 1)
        else:
            next_reward = 50
    
    return {
        "can_claim": can_claim,
        "streak": user['daily_streak'],
        "next_reward": next_reward,
        "last_claim": last_daily.isoformat() if last_daily else None
    }

@app.post("/api/daily/claim")
async def claim_daily_endpoint(user_id: int):
    """Забрать ежедневную награду"""
    reward, streak = claim_daily(user_id)
    if reward == 0:
        raise HTTPException(status_code=400, detail="Already claimed today")
    
    # Проверка повышения уровня
    leveled, new_level, level_reward = check_level_up(user_id)
    
    return {
        "success": True,
        "reward": reward,
        "streak": streak,
        "new_balance": get_balance(user_id),
        "level_up": {
            "occurred": leveled,
            "new_level": new_level if leveled else None,
            "reward": level_reward if leveled else 0
        }
    }

@app.post("/api/friends/send")
async def send_friend_request_endpoint(request: FriendRequest):
    """Отправить заявку в друзья"""
    if request.user_id == request.friend_id:
        raise HTTPException(status_code=400, detail="Cannot add yourself")
    
    target = get_user(request.friend_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    
    success = send_friend_request(request.user_id, request.friend_id)
    if not success:
        raise HTTPException(status_code=400, detail="Request already sent or already friends")
    
    return {"success": True, "message": "Заявка отправлена"}

@app.get("/api/friends/{user_id}")
async def get_friends_list(user_id: int):
    """Получить список друзей"""
    friends = get_friends(user_id)
    return {"friends": friends}

@app.get("/api/friends/requests/{user_id}")
async def get_friend_requests_endpoint(user_id: int):
    """Получить входящие заявки в друзья"""
    requests = get_friend_requests(user_id)
    return {"requests": requests}

@app.post("/api/friends/accept/{request_id}")
async def accept_friend_request_endpoint(request_id: int):
    """Принять заявку в друзья"""
    success = accept_friend_request(request_id)
    if not success:
        raise HTTPException(status_code=400, detail="Request not found or already processed")
    
    return {"success": True, "message": "Заявка принята"}

@app.post("/api/admin/give")
async def admin_give_coins(user_id: int, admin_id: int, amount: int):
    """Выдать монеты (админ)"""
    admin = get_user(admin_id)
    if not admin or not admin.get('is_admin', False):
        raise HTTPException(status_code=403, detail="Access denied")
    
    update_balance(user_id, amount)
    return {
        "success": True,
        "target": user_id,
        "amount": amount,
        "new_balance": get_balance(user_id)
    }

@app.post("/api/admin/withdraw/process")
async def admin_process_withdraw(admin_id: int, request_id: int, action: str):
    """Обработать заявку на вывод (админ)"""
    admin = get_user(admin_id)
    if not admin or not admin.get('is_admin', False):
        raise HTTPException(status_code=403, detail="Access denied")
    
    if action not in ['approve', 'reject']:
        raise HTTPException(status_code=400, detail="Action must be 'approve' or 'reject'")
    
    success = process_withdraw(request_id, action)
    if not success:
        raise HTTPException(status_code=400, detail="Request not found or already processed")
    
    return {"success": True, "message": f"Заявка {action}ed"}

@app.get("/api/admin/withdrawals")
async def admin_get_withdrawals(admin_id: int):
    """Получить все заявки на вывод (админ)"""
    admin = get_user(admin_id)
    if not admin or not admin.get('is_admin', False):
        raise HTTPException(status_code=403, detail="Access denied")
    
    requests = get_pending_withdrawals()
    return {"withdrawals": requests}

@app.get("/api/clan/{user_id}")
async def get_user_clan(user_id: int):
    """Получить клан пользователя"""
    clan = get_my_clan(user_id)
    if not clan:
        return {"has_clan": False}
    
    members = get_clan_members(clan['id'])
    rank = get_user_rank(user_id, clan['id'])
    
    return {
        "has_clan": True,
        "clan": clan,
        "members": members,
        "user_rank": rank
    }

@app.post("/api/clan/join")
async def join_clan_endpoint(user_id: int, clan_id: int):
    """Вступить в клан"""
    success = join_clan(user_id, clan_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot join clan")
    
    return {"success": True, "message": "Вы вступили в клан"}

@app.post("/api/skin/sell")
async def sell_skin_endpoint(user_id: int, inventory_id: int):
    """Продать скин"""
    success, msg = sell_skin(user_id, inventory_id)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    
    return {
        "success": True,
        "message": msg,
        "new_balance": get_balance(user_id)
    }

@app.post("/api/steam/link")
async def link_steam_endpoint(user_id: int, steam_url: str):
    """Привязать Steam"""
    success, msg = link_steam(user_id, steam_url)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    
    return {"success": True, "message": msg}

@app.post("/api/steam/unlink")
async def unlink_steam_endpoint(user_id: int):
    """Отвязать Steam"""
    success = unlink_steam(user_id)
    if not success:
        raise HTTPException(status_code=400, detail="Steam not linked or not found")
    
    return {"success": True, "message": "Steam отвязан"}

@app.get("/api/config")
async def get_config():
    """Конфигурация для фронта"""
    return {
        "course": COURSE,
        "sell_percent": SELL_PRICE_PERCENT,
        "channels": {
            "arzdrop": CHANNEL_ARZDROP,
            "artstudio": CHANNEL_ARTSTUDIO
        },
        "achievement_levels": ACHIEVEMENT_LEVELS,
        "achievement_rewards": ACHIEVEMENT_REWARDS
    }

# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
