"""SQLite unit of work; transactions are short, atomic, restart-safe.

Database work runs via asyncio.to_thread so aiogram event loop isn't blocked.
Never hold a transaction while calling a remote Telegram or payment API.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
from html import escape
from pathlib import Path
import re
import sqlite3
from typing import Callable, TypeVar

T = TypeVar('T')
DEFAULT_PROJECT_NAME = 'Aurora Access'
BRANDED_TEMPLATES = (
    'welcome', 'welcome_back', 'join_welcome', 'catalog', 'help', 'info',
    'promo_push_text', 'review_push_text',
)


def _tokenize_brand_mentions(con: sqlite3.Connection, previous_name: str):
    names = {previous_name, DEFAULT_PROJECT_NAME, 'LuxuryHub'}
    spellings = {variant for name in names for variant in (name, escape(name), escape(name, quote=False))}
    alternatives = '|'.join(re.escape(name) for name in sorted(spellings, key=len, reverse=True) if name)
    pattern = re.compile(
        r"""<(?:"[^"]*"|'[^']*'|[^'"<>])*?>|(?:https?://|(?:t|telegram)\.me/)[^\s<]+|@[\w]+|\{[^{}]+\}|"""
        rf'(?P<brand>(?<!\w)(?:{alternatives})(?!\w))',
        re.IGNORECASE,
    )
    keys = ','.join('?' for _ in BRANDED_TEMPLATES)
    for key, value in con.execute(f'SELECT key,value FROM settings WHERE key IN ({keys})', BRANDED_TEMPLATES).fetchall():
        updated = pattern.sub(lambda match: '{project_name}' if match['brand'] else match[0], value)
        if updated != value:
            con.execute('UPDATE settings SET value=? WHERE key=?', (updated, key))
    for review_id, caption in con.execute("SELECT id,caption FROM reviews WHERE caption!=''").fetchall():
        updated = pattern.sub(lambda match: '{project_name}' if match['brand'] else match[0], caption)
        if updated != caption:
            con.execute('UPDATE reviews SET caption=? WHERE id=?', (updated, review_id))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def in_seconds(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec='seconds')


SCHEMA = '''
CREATE TABLE IF NOT EXISTS users(
 user_id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '',
 name TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, last_seen TEXT NOT NULL,
 active_promo TEXT NOT NULL DEFAULT '',
 last_viewed_tariff_id INTEGER,
 last_viewed_at TEXT,
 view_reminder_sent INTEGER NOT NULL DEFAULT 0,
 view_reminder_sent_at TEXT,
 promo_push_sent INTEGER NOT NULL DEFAULT 0,
 promo_push_code TEXT NOT NULL DEFAULT '',
 promo_push_sent_at TEXT,
 first_start_at TEXT,
 promo_push_retry_at TEXT,
 review_push_retry_at TEXT,
 last_review_push_at TEXT,
 promo_expiry_reminder_sent INTEGER NOT NULL DEFAULT 0,
 cold_nudge_sent INTEGER NOT NULL DEFAULT 0,
 winback_push_sent INTEGER NOT NULL DEFAULT 0,
 broadcast_opt_in INTEGER NOT NULL DEFAULT 0,
 broadcast_opt_out INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tariffs(
 id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
 description TEXT NOT NULL DEFAULT '', rub INTEGER NOT NULL CHECK(rub>=1),
 stars INTEGER NOT NULL CHECK(stars>=1), duration_days INTEGER NOT NULL DEFAULT 0
 CHECK(duration_days>=0), active INTEGER NOT NULL DEFAULT 1,
 channel_ids TEXT NOT NULL DEFAULT '', media_id TEXT NOT NULL DEFAULT '',
 media_type TEXT NOT NULL DEFAULT '',
 preview_media_id TEXT NOT NULL DEFAULT '', preview_media_type TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings(
 key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '',
 media_id TEXT NOT NULL DEFAULT '', media_type TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS welcome_channels(
 chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
 is_admin INTEGER NOT NULL DEFAULT 0, can_invite INTEGER NOT NULL DEFAULT 0,
 enabled INTEGER NOT NULL DEFAULT 0, auto_approve INTEGER NOT NULL DEFAULT 0,
 rights_at INTEGER
);
CREATE TABLE IF NOT EXISTS product_channel_links(
 chat_id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS campaign_touches(
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
 campaign TEXT NOT NULL, prepared_at TEXT NOT NULL, sent_at TEXT, clicked_at TEXT
);
CREATE INDEX IF NOT EXISTS campaign_touches_report ON campaign_touches(sent_at, campaign);
CREATE INDEX IF NOT EXISTS campaign_touches_user ON campaign_touches(user_id, clicked_at);
CREATE TABLE IF NOT EXISTS campaign_orders(
 order_id TEXT PRIMARY KEY, touch_id INTEGER NOT NULL REFERENCES campaign_touches(id),
 clicked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS campaign_orders_touch ON campaign_orders(touch_id);
CREATE TABLE IF NOT EXISTS welcome_requests(
 chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, requested_at INTEGER NOT NULL,
 PRIMARY KEY(chat_id,user_id)
);
CREATE INDEX IF NOT EXISTS welcome_requests_age ON welcome_requests(requested_at);
CREATE TABLE IF NOT EXISTS welcome_approvals(
 chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, event_id TEXT NOT NULL,
 created_at TEXT NOT NULL, retry_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(chat_id,user_id)
);
CREATE INDEX IF NOT EXISTS welcome_approvals_retry ON welcome_approvals(retry_at);
CREATE TABLE IF NOT EXISTS greeting_events(
 id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
 requested_at INTEGER NOT NULL, sent_at TEXT, engaged_at TEXT,
 UNIQUE(chat_id,user_id,requested_at)
);
CREATE INDEX IF NOT EXISTS greeting_events_time ON greeting_events(requested_at,chat_id);
CREATE INDEX IF NOT EXISTS greeting_events_user ON greeting_events(user_id,sent_at);
CREATE TABLE IF NOT EXISTS promos(
 code TEXT PRIMARY KEY, discount INTEGER NOT NULL CHECK(discount>=0),
 discount_percent INTEGER NOT NULL DEFAULT 0 CHECK(discount_percent>=0 AND discount_percent<=99),
 max_uses INTEGER NOT NULL CHECK(max_uses>=0), used INTEGER NOT NULL DEFAULT 0 CHECK(used>=0),
 enabled INTEGER NOT NULL DEFAULT 1,
 owner_user_id INTEGER,
 expires_at TEXT,
 is_personal INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS promo_redemptions(
 code TEXT NOT NULL, user_id INTEGER NOT NULL, order_id TEXT NOT NULL UNIQUE,
 PRIMARY KEY(code,user_id),
 FOREIGN KEY(code) REFERENCES promos(code)
);
CREATE TABLE IF NOT EXISTS orders(
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, tariff_id INTEGER,
 title TEXT NOT NULL, amount_rub INTEGER NOT NULL, amount_stars INTEGER NOT NULL,
 duration_days INTEGER NOT NULL, channels TEXT NOT NULL,
 promo_code TEXT NOT NULL DEFAULT '',
 provider TEXT NOT NULL, external_id TEXT NOT NULL DEFAULT '',
 payment_url TEXT NOT NULL DEFAULT '', charge_id TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'pending',
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, paid_at TEXT,
 notice_sent INTEGER NOT NULL DEFAULT 0,
 order_kind TEXT NOT NULL DEFAULT 'tariff',
 whale_alert_sent INTEGER NOT NULL DEFAULT 0,
 FOREIGN KEY(user_id) REFERENCES users(user_id),
 FOREIGN KEY(tariff_id) REFERENCES tariffs(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS orders_pending ON orders(status,provider,created_at);
CREATE INDEX IF NOT EXISTS orders_promo ON orders(promo_code,status);
CREATE UNIQUE INDEX IF NOT EXISTS orders_charge_unique ON orders(provider,charge_id) WHERE charge_id != '';
CREATE TABLE IF NOT EXISTS stars_refund_inbox(
 charge_id TEXT PRIMARY KEY, amount INTEGER NOT NULL, user_id INTEGER
);
CREATE TABLE IF NOT EXISTS purchases(
 id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL UNIQUE,
 user_id INTEGER NOT NULL, tariff_id INTEGER NOT NULL,
 title TEXT NOT NULL, started_at TEXT NOT NULL, expires_at TEXT,
 expiry_notice_sent INTEGER NOT NULL DEFAULT 0,
 revoked_by_admin INTEGER NOT NULL DEFAULT 0,
 post_join_upsell_sent INTEGER NOT NULL DEFAULT 0,
 pre_expiry_upsell_sent INTEGER NOT NULL DEFAULT 0,
 FOREIGN KEY(order_id) REFERENCES orders(id)
);
CREATE INDEX IF NOT EXISTS purchases_user ON purchases(user_id,expires_at);
CREATE TABLE IF NOT EXISTS grants(
 id INTEGER PRIMARY KEY AUTOINCREMENT, purchase_id INTEGER NOT NULL,
 user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
 invite_url TEXT NOT NULL DEFAULT '', invite_expires_at TEXT,
 state TEXT NOT NULL DEFAULT 'need_link',
 last_error TEXT NOT NULL DEFAULT '', joined_at TEXT, processing_at TEXT,
 UNIQUE(purchase_id,chat_id),
 FOREIGN KEY(purchase_id) REFERENCES purchases(id)
);
CREATE INDEX IF NOT EXISTS grants_user ON grants(user_id,state);
CREATE INDEX IF NOT EXISTS grants_chat ON grants(chat_id);
CREATE TABLE IF NOT EXISTS audit(
 id INTEGER PRIMARY KEY AUTOINCREMENT, actor INTEGER NOT NULL, action TEXT NOT NULL,
 subject TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS simulated_sales(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 title TEXT NOT NULL,
 timestamp INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS simulated_sales_ts ON simulated_sales(timestamp DESC);
CREATE TABLE IF NOT EXISTS reviews(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 media_id TEXT NOT NULL,
 media_type TEXT NOT NULL DEFAULT 'photo',
 caption TEXT NOT NULL DEFAULT '',
 active INTEGER NOT NULL DEFAULT 1,
 sort_order INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_review_pushes(
 user_id INTEGER NOT NULL,
 review_id INTEGER NOT NULL,
 sent_at TEXT NOT NULL,
 PRIMARY KEY(user_id, review_id),
 FOREIGN KEY(user_id) REFERENCES users(user_id),
 FOREIGN KEY(review_id) REFERENCES reviews(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS user_review_pushes_time ON user_review_pushes(user_id, sent_at);
'''

DEFAULT_SETTINGS = {
    'project_name': DEFAULT_PROJECT_NAME,
    'menu_revision': '2',
    'join_welcome': (
        "<b>{name}, заявка в «{channel}» принята</b> ✅\n\n"
        "Сами фуллы в открытый канал не заливаем — их сносят за пару часов. "
        "Все полные ролики со звуком (3–15 мин, без обрезок и блюра) лежат прямо здесь, в закрытом резерве.\n\n"
        "👇 <b>Жми ниже, чтобы выбрать раздел и забрать доступ:</b>"
    ),
    'welcome': (
        "<b>Резервный архив {project_name} открыт</b> 🔓\n\n"
        "Основной поток часто ловят по жалобам, поэтому все полные версии из TikTok, записи стримов и закрытые 18+ подборки храним здесь.\n"
        "• Оригиналы 3–15 мин в HD со звуком\n"
        "• Ссылка в закрытый канал приходит в чат сразу после оплаты\n"
        "• Разовый вход — без подписок и скрытых списаний\n\n"
        "👇 <b>Выбери раздел (при апгрейде на «Всё сразу» первая оплата зачтётся 100%):</b>"
    ),
    'welcome_back': (
        "<b>С возвращением, {name}</b> 👋\n\n"
        "В архив {project_name} уже догрузили свежие фуллы из TikTok и новые подборки за последние 24 часа.\n\n"
        "👇 <b>Выбирай нужный раздел ниже:</b>"
    ),
    'catalog': (
        "<b>Резервный архив {project_name} открыт</b> 🔓\n\n"
        "Основной поток часто ловят по жалобам, поэтому все полные версии из TikTok, записи стримов и закрытые 18+ подборки храним здесь.\n"
        "• Оригиналы 3–15 мин в HD со звуком\n"
        "• Ссылка в закрытый канал приходит в чат сразу после оплаты\n"
        "• Разовый вход — без подписок и скрытых списаний\n\n"
        "👇 <b>Выбери раздел (при апгрейде на «Всё сразу» первая оплата зачтётся 100%):</b>"
    ),
    'help': (
        "❓ <b>Частые вопросы · {project_name}</b>\n\n"
        "• <b>Когда придёт ссылка на канал?</b>\n"
        "Моментально после оплаты прямо в этот чат. Ссылка также навсегда сохранится в «🔓 Мои доступы».\n\n"
        "• <b>Почему это платно, а не в открытом канале?</b>\n"
        "Открытые каналы сносят по жалобам за пару часов. Здесь закрытый резерв с защищёнными зеркалами и автовыдачей.\n\n"
        "• <b>Будут ли повторные списания?</b>\n"
        "Нет. Оплата строго разовая — без подписок и скрытых списаний.\n\n"
        "• <b>Можно ли обновиться до «Всё сразу» с доплатой?</b>\n"
        "Да! При покупке любого тарифа вся оплаченная сумма зачтётся как 100% скидка на «ВСЁ СРАЗУ».\n\n"
        "По оплате: /paysupport\n"
        "📄 <a href=\"https://t.me/{bot_username}?start=agreement\">Условия покупки</a> · <a href=\"https://t.me/{bot_username}?start=privacy\">Конфиденциальность</a>"
    ),
    'info': (
        "ℹ️ <b>О закрытом резерве {project_name}</b>\n\n"
        "Приватный архив с ежедневными пополнениями — полные оригиналы в HD со звуком, без блюра и вырезанных концовок.\n\n"
        "• Вирусные фуллы из TikTok и записи стримов\n"
        "• Закрытые подборки и авторские паки\n"
        "• Мгновенная ссылка в чат сразу после оплаты\n"
        "• Разовый вход без подписок\n\n"
        "Выбирай нужный раздел через «📂 Каталог фуллов» в меню."
    ),
    'support_url': '',
    'contacts_visible': '1',
    'reviews_url': '',
    'reviews_visible': '0',
    'help_visible': '1',
    'partnership_contact': '',
    'sbp_details': '',
    'auto_approve_public_join': '0',
    'greeting_enabled': '0',
    'greeting_auto_new': '0',
    'project_started_at': '',
    'stars_ledger_offset': '0',
    'promo_push_enabled': '0',
    'promo_push_mode': 'personal',
    'promo_push_code': 'VIP2026',
    'promo_push_prefix': 'VIP',
    'promo_push_discount': '20%',
    'promo_push_ttl_hours': '3',
    'promo_push_delay_min': '240',
    'promo_push_text': (
        "<b>{name}, оставили для тебя закрытую скидку {discount} ₽</b> 🔑\n\n"
        "Вижу, ты заглядывал в {project_name}, но доступ так и не забрал. "
        "Держи личный код <code>{code}</code>{ttl_note} — "
        "он снизит цену доступа. Итоговую цену увидишь в карточке тарифа.\n\n"
        "👇 <b>Жми ниже — скидка применится сама в 1 клик:</b>"
    ),
    'review_push_enabled': '0',
    'review_push_audience': 'cold',
    'review_push_delay_min': '90',
    'review_push_interval_hours': '24',
    'review_push_max_per_user': '2',
    'review_push_order': 'seq',
    'review_push_text': (
        "📸 <b>Скрин от участника, который зашёл сегодня</b>\n\n"
        "Главный вопрос перед входом — «а не пустой ли там канал?». "
        "Выше — реальный фидбек из лички. Внутри уже сотни полных роликов со звуком, новые заливаем каждый день.\n\n"
        "👇 <b>Можно зайти с пробного пропуска за 190 ₽ и проверить лично:</b>"
    ),
}

# Upgrade only exact bundled text. Custom storefront settings remain editable.
LEGACY_SECTION_DIGESTS = {
    'catalog': '315683ab27f5a84a64d736f20569d93945f37d1e4ee501652556ab689475fde2',
    'help': 'b9d807788097596f4af821f8cfb7c552788c49a416b5c113b1b4a17a7c8b0733',
    'info': '1a1a189cf174b8f1917e36e472ac6ccb952b530ab7795968a19cb491c3441346',
}

V4_SECTION_DIGESTS = {
    'join_welcome': '9fc8abfb9306821435c50877034b3bd9d0ed64e3c850d335d7be7f07441bfa39',
    'welcome': 'fafa7cac09b9b8710e923b01362bdb597bf2a8f2f5896e0035532c1339066b10',
    'welcome_back': 'b275a9c6e29e13900617c340ad2bdc1e83e1f955e0793c67ee976200e31d0f19',
    'catalog': '085d1588c8ba4566c81406c0a3f4500b07b442be976275c0ebc2c59ecb8ac91d',
    'help': 'c7584a609e8e6c11987bf1a48cff3a2be3eb9cb76feaf2d57006f35a6fb5351d',
    'info': 'c51f8d1818ac6aae257c6f15d0115ae259e3e16e103ceffea14b28646aee4864',
}

V10_SECTION_DIGESTS = {
    'join_welcome': '317d901bed81a75e4479b53a6da490dd9776b5e2ce4a906a2f0eee3e29206777',
    'welcome': 'fd5dea313e4cee19e35eca4cebb0ee72bb083fa5e306338aeba6463cf08eb57c',
    'welcome_back': '916de3578d69046518dfc5a286ac73699a9c560dab4f73d2530186b5fcb71814',
    'catalog': 'dc18941320c99290e0d52328104b1031566a233cc42f7eee5bebc7c8ca43381d',
    'help': '6718e1150f585b1632f7564b3098f0b529f8315c43459c7dd49ea001bdcc2a82',
    'info': '02342aeaeb0097e90c0ac6b61b8387d369a0a64e41099f088e6ae58d93b949c6',
}

V11_SECTION_DIGESTS = {
    'join_welcome': '90927479630eabd56d6d53d0d0e2ff88eb997658057380a5e0040c72ad2f8d2a',
    'welcome': '5bf727a9923afbfbae74e6817f08dc997b6265c4d04d58c8d33f4ba765d2ff0b',
    'welcome_back': 'e3dffd3a57f75f761af42cb42ad88038711458899db3f3a4fbed314962c9a3de',
    'catalog': '8efcb5202681b7e25051ad0e0b9150419b8c812d6b846b0b722a0b56124a2c10',
}

V11_SEED_PRICES = (
    ('🔥 ТОТ САМЫЙ ФУЛЛ ИЗ TIKTOK + ХИТЫ [24 ЧАСА] 🔞', 440, 230, 350, 180),
    ('🎀 СОЧНЫЕ АЛЬТУШКИ, СТРИМЕРШИ & TIKTOK [18+]', 790, 410, 690, 360),
    ('🍑 ONLYFANS, FANSLY & БЛОГЕРШИ СНГ [FULL HD]', 790, 410, 690, 360),
    ('😈 СКРЫТЫЙ АРХИВ: ДОМАШНЕЕ & СЛИВЫ ЛИЧЕК [18+]', 990, 510, 890, 460),
    ('⭐️ 🏆 ULTRA VIP: ВСЕ 5 КАНАЛОВ СРАЗУ [ТОП]', 1190, 620, 1090, 570),
    ('👑 DIAMOND CLUB: ВСЕ БАЗЫ + ПОИСК ПО ИМЕНАМ [ТОП]', 2140, 1100, 1990, 1030),
)

V12_SECTION_DIGESTS = {
    'join_welcome': '4d2e8d12a7e69ed5c2bcc6bf617a0e15bd6c644f050a41df3214c82d4d764c5f',
    'welcome': 'bf083de3a8d7fe7e760e8f1148d644d0b6cbcaa179ccdec39c00bd4fd999c2a7',
    'welcome_back': '5aa285797e78bfd60b0593eea556560f8a07342b437f2c47ac1263efd2f95ab9',
    'catalog': '9c005da91952b6a4dbaabe089b8d1d0d4b3ff00ea21cf5c7958729704ff605d7',
    'help': '548b9ebf2cc01f150a5cf2437749f01090430d2cb31c65d2068b2c092d676c4c',
}

V16_SECTION_DIGESTS = {
    'join_welcome': 'c99dd1af1e40a6b54fcf0a2f83c621a938e268d6bb0b589d77f38935e6755fbe',
    'welcome': '4d6544fe48ee283a02d182af8a4ed3851762669af0ffbe9786785cbefac4bd5e',
    'welcome_back': '98040ec32020e5657c97616463fdc9d2d4365ccb7144b07ff4bde362cb2d9195',
    'catalog': '6f2c8cfff8e34ebad7884540443189394433054f2831fe80b71dd36491cfeab6',
    'help': '3930b21a4e21864b07f94ae475f9a6da82d3d2652eff41202b41fa73ebd1ff24',
    'info': 'fa6985270f3e86b8f026ace680272e640922e9acbb6be9874abafcc21bb8a132',
}

V18_SECTION_DIGESTS = {
    'catalog': 'c81b6ac959949c5ee844b2f543d8baea3a108add49d1044766344c1ebfd21318',
}

V19_SECTION_DIGESTS = {
    'join_welcome': '74e09cce88565f1525a3920b9c3840c6c031be74ac5cf2cc7077a33d034d2c37',
    'welcome': '106b33ca4c45a2816282d34794a5d12ac0cda7c974361a59de9420f2ccc17e39',
    'welcome_back': '9dbcf05d0e1d71d8950deef49cadf2dec6a2b1074524c8b37e5e95f5cf6753af',
    'catalog': '0e702b1470356c76a03a5ba54ec26bfc8e3f0b5a37919ecfa1aefb05380fc926',
    'info': '2456b5f8b07c39455f7d9ab9cc9c62e0baa561858f5078b8bda55438efa54c4d',
}

V20_SECTION_DIGESTS = {
    'catalog': '877a6f35255a7116dbe916a72d561103deb54b0d2a84aeda4e51a1f4e6775fe3',
}

V21_SECTION_DIGESTS = {
    'join_welcome': '0136915e282ceea4c3b00882c62ad7880f3a0cd4460ef9de7868cc2c4cf7d29e',
}

V25_SECTION_DIGESTS = {
    'join_welcome': 'f39aa08f26bba89e3c7d5f213a207f6637e4accaccf02f7fd3e844f6c81d237e',
    'welcome': '016a65fa3eb24d95b3b4ed300aaf9580a35be256bfba4718d4394caf0e0c7e0d',
    'welcome_back': 'e75ec6c8f3fc97070c43f1c015ff52e66f4a8fafb7c5ab0fe432daab4c0a2bc3',
    'catalog': '43be014dcc06efe627f537beab08c31c11e035886fc4876dfaad0247edca7da1',
    'help': 'b33f7e04be771cda1de88a2c9721c4917ff80879117e869d029c4df397bbe299',
    'info': '1bebf2752dd785fe4915f10571fbdc45572c5c54142c58a7ea0cf5e000dc265c',
    'promo_push_text': '42e250cfbb7b414c63ae6a8e8c9bed8e2f4ddc22b9eb97070a3196acc9e9c733',
    'review_push_text': '56896c34595c0883e02c5aa17128f50ba1690259d91d420fb9139640adb12af1',
}

V26_SECTION_DIGESTS = {
    'catalog': '54e2be454dcafe06f901eed1db9007724d03d89cf69b2025e556a66ddfd286b0',
    'help': '9499093c75f812694ebfae7189431996090d54601fc0a66e08037aeb484c3a29',
}

V27_SECTION_DIGESTS = {
    'join_welcome': '03f7a043041d374fa91124363dc7a332e2b059086df4c98a40872e4a15c90116',
}

V28_SETTINGS_DIGESTS = {
    'join_welcome': '0e3d0ac97dc118e438b5405040be011fec9e45de460ae19d37ca7cd75a8550ef',
    'welcome': '14cd5393b046df148f4f324f7d499b1e5933dcff5ac91145a190c7e68b3273e5',
    'welcome_back': '3859a8afff91a75bc0c751f17452de8c57c980c1f0675ec517c52b9f1eca9b5d',
    'catalog': 'fc5f676b405da7ffee1cbed6eeb23e8aedc1c79bc73f469f1d8250933eab4ec9',
    'help': '1c6dc35651d584f1d46ed1b750d4fdb7de27b3c6be37b67c40400ff778eda8d6',
    'info': 'fcf725c419fcaa2228b59f249e82fd2ba89de5e55caf3f51651175eacd54f961',
    'promo_push_text': 'd99cd73a0b4ea29693e435da095ca5125cda3f2005e55c1bdda3021592c93a9e',
    'review_push_text': 'fde92e335f48a5869a47f7a564c0917fedcdca9894522db0ddecf6f8d2eaec73',
    'promo_push_delay_min': '624b60c58c9d8bfb6ff1886c2fd605d2adeb6ea4da576068201b6c6958ce93f4',
    'promo_push_discount': '9ae2bdd7beedc2e766c6b76585530e16925115707dc7a06ab5ee4aa2776b2c7b',
    'promo_push_ttl_hours': '5feceb66ffc86f38d952786c6d696c79c2dbc239dd4e91b46729d73a27fb57e9',
    'review_push_delay_min': '2abaca4911e68fa9bfbf3482ee797fd5b9045b841fdff7253557c5fe15de6477',
    'review_push_max_per_user': '4e07408562bedb8b60ce05c1decfe3ad16b72230967de01f640b7e4729b49fce',
}
V29_PROMO_TEXT_DIGEST = 'a384c35bf18189e23ec8b1e47b35d2cc767b0e6aca052be092045ab1866be097'

SEED_TARIFFS = [
    (
        '⚡ Пропуск: Фуллы TikTok (3 дн.)',
        'Если нужен конкретный вирусный фулл из TikTok без лишних переплат.\n\n'
        '• Полные версии трендовых роликов, драм и треш-стримов (3–15 мин со звуком)\n'
        '• Без блюра, смайлов и вырезанных концовок\n'
        '• Мгновенная ссылка в закрытый канал на 3 дня\n'
        '💡 Если зайдёт — все 190 ₽ останутся у тебя на балансе как скидка на «Всё сразу».',
        190,
        99,
        3,
        '📂 Что лежит в канале «Архив TikTok»:\n'
        '• Ежедневные заливы по свежим вирусным инфоповодам и стримам\n'
        '• Удобный поиск по тегам и датам — не нужно листать 1000 постов\n'
        '• Полные ролики в исходном качестве со звуком (без переходов по сторонним сайтам)',
    ),
    (
        '🎀 Альтушки и стримерши (30 дн.)',
        'Приватные видео тиктокерш, альтушек, косплеерш и треш-стримерш Рунета.\n\n'
        '• Сотни полных видео, закрытых эфиров и кружочков со звуком (3–15 мин)\n'
        '• Без цензуры, смайлов и рекламы — удобная навигация по именам и типажам\n'
        '• Мгновенная ссылка в закрытый канал на 30 дней\n'
        '⚡ Совет: за +400 ₽ в «ВСЁ СРАЗУ» откроются ВСЕ 4 канала и НАВСЕГДА.',
        590,
        299,
        30,
        '📂 Внутри канала «Альтушки и стримерши» (видео и фото со звуком):\n'
        '• Записи приватных эфиров, кружочки и домашние архивы без цензуры\n'
        '• Удобная навигация по именам тиктокерш, альтушек и стримерш\n'
        '• Вход на 30 дней — при апгрейде на «ВСЁ СРАЗУ» вся оплата зачтётся 100%',
    ),
    (
        '🍑 OnlyFans & Fansly СНГ (30 дн.)',
        'Выкупленные приватные паки моделей OnlyFans, Fansly и блогерш СНГ.\n\n'
        '• Полные видео 20–45 мин в HD со звуком без водяных знаков\n'
        '• Удобный каталог и алфавитный указатель по именам моделей\n'
        '• Мгновенная ссылка в закрытый канал на 30 дней\n'
        '⚡ Совет: за +200 ₽ в «ВСЁ СРАЗУ» откроются ВСЕ 4 канала и НАВСЕГДА.',
        790,
        410,
        30,
        '📂 Внутри раздела OnlyFans & Fansly (HD со звуком):\n'
        '• Эксклюзивные фотосеты и полные видеоролики без цензуры\n'
        '• Регулярные пополнения новых паков каждую неделю\n'
        '• Быстрый поиск по тегам и именам моделей',
    ),
    (
        '😈 Скрытый архив: Лички (30 дн.)',
        'Закрытый архив редких домашних подборок, приватных эфиров и кружочков.\n\n'
        '• Полные ролики со звуком, которые моментально удаляют из сети\n'
        '• Удобная сортировка по датам и коллекциям\n'
        '• Мгновенная ссылка в закрытый канал на 30 дней\n'
        '⚡ Совет: за +100 ₽ в «ВСЁ СРАЗУ» откроются ВСЕ 4 канала и НАВСЕГДА.',
        890,
        460,
        30,
        '📂 Внутри скрытого архива (HD со звуком):\n'
        '• Редкие приватные кружочки и домашние записи со звуком\n'
        '• Эксклюзивные материалы, которых нет в открытых каналах\n'
        '• Вход на 30 дней — при апгрейде на «ВСЁ СРАЗУ» вся оплата зачтётся 100%',
    ),
    (
        '🔥 ВСЁ СРАЗУ · 4 канала навсегда',
        'Главный доступ {project_name} — все 4 закрытых канала одним платежом и навсегда:\n\n'
        '1. Фуллы и вирусные инфоповоды TikTok (полные версии со звуком)\n'
        '2. Альтушки, косплей и стримерши\n'
        '3. OnlyFans, Fansly и блогерши СНГ\n'
        '4. Скрытый архив домашних съёмок и личек\n'
        '✅ Разовая оплата — без подписок, продлений и доплат за обновления.',
        990,
        499,
        0,
        '👑 Полная сетка резерва из 4 каналов навсегда:\n'
        '• Моментально приходят персональные приглашения во все 4 закрытых канала\n'
        '• Все будущие ежедневные заливы открываются автоматически\n'
        '• Резервные зеркала — доступ сохраняется в «🔓 Мои доступы» навсегда',
    ),
    (
        '⭐️ Ultra VIP: 5 каналов [ХИТ]',
        '👑 Главный абонемент клуба: доступ ко всей сетке резерва навсегда.',
        1190,
        590,
        0,
        '📂 Внутри Ultra VIP: доступ ко всей сетке резерва навсегда.',
    ),
    (
        '👑 Diamond Club: Все базы',
        '💎 Закрытые облачные базы и архивы вне общего каталога.',
        2490,
        1290,
        0,
        '📂 Внутри Diamond Club: все MEGA-архивы и приватные коллекции.',
    ),
    (
        '🖤 Black VIP: Все архивы [4K]',
        '🖤 Персональный VIP-статус и максимальный архив.',
        4990,
        2590,
        0,
        '📂 Внутри Black VIP: все 4K-архивы и приватные коллекции.',
    ),
]

V26_SEED_UPGRADES = [
    (
        '⚡ Базовый · Фуллы из TikTok',
        '⚡️ Ищешь тот самый треш-фулл или слив из TikTok? Оригинал уже в закрытом резерве.\n\n'
        '• Полные ролики 3–15 мин со звуком — без блюра, смайлов и цензуры\n'
        '• Бессрочный вход в 1 базовый канал без подписок\n'
        '• Моментальная автовыдача 24/7 + доступ к «Столу заказов»',
        240,
        125,
        0,
        '📂 Внутри базового резерва (4 200+ фулл-видео):\n'
        '• Поиск по горячим инфоповодам, дракам и треш-стримам из TikTok\n'
        '• Оригиналы 3–15 мин в Full HD со звуком без вырезанных моментов\n'
        '• Ссылка прилетает сразу + открывается личный «Стол заказов»',
    ),
    (
        '🎀 Альтушки и стримерши [30д]',
        '🎀 Приватные видео тиктокерш, косплеерш и треш-стримерш Рунета.\n\n'
        '• 18 000+ домашних роликов, сторис и закрытых кружочков со звуком\n'
        '• Быстрый поиск по никам и именам без рекламы\n'
        '• 1 канал на 30 дней (все 5 каналов навсегда в Ultra VIP = 1 190 ₽)',
        690,
        360,
        30,
        '📂 Внутри раздела (18 400+ видео и фото со звуком):\n'
        '• Записи приватных эфиров, интим-кружочки и домашние архивы\n'
        '• Удобная навигация по никам тиктокерш, альтушек и стримерш\n'
        '• Вход на 30 дней — при апгрейде на Ultra VIP зачтём всю сумму',
    ),
    (
        '🍑 OnlyFans & Fansly СНГ [30д]',
        '🔥 Выкупленные платные паки моделей OnlyFans, Fansly и блогерш СНГ.\n\n'
        '• 25 000+ видео в Full HD / 4K, полные ролики 20–45 мин со звуком\n'
        '• Алфавитный поиск по именам и никам моделей\n'
        '• 1 канал на 30 дней (до вечного Ultra VIP всего +400 ₽)',
        790,
        410,
        30,
        '📂 Внутри раздела (25 000+ медиа в Full HD / 4K):\n'
        '• Платные PPV-видео из личек моделей OnlyFans, Fansly и СНГ\n'
        '• Полные 20–45 мин ролики без склеек и водяных знаков\n'
        '• В Ultra VIP (1 190 ₽) этот архив и ещё 4 канала доступны навсегда',
    ),
    (
        '😈 Скрытый архив: Лички [30д]',
        '🗄 Самый горячий раздел: реальные домашние сливы и закрытые лички.\n\n'
        '• 20 000+ домашних видео, голосовых и приватных кружочков\n'
        '• Материалы, которые сносят на всех открытых каналах\n'
        '• 1 канал на 30 дней (⚡️ за +300 ₽ в Ultra VIP откроешь ВСЕ 5 навсегда!)',
        890,
        460,
        30,
        '📂 Внутри закрытого архива (20 500+ эксклюзивов):\n'
        '• Приватные переписки с интим-кружочками и любительскими съёмками\n'
        '• Редкие домашние видео со звуком «только для парня»\n'
        '• ⚡️ За +300 ₽ вместо 1 канала на месяц берут Ultra VIP (все 5 навсегда)',
    ),
    (
        '⭐️ Ultra VIP: 5 каналов [ХИТ]',
        '👑 Главный пакет резерва — все 5 закрытых каналов сразу навсегда (~238 ₽/канал):\n\n'
        '1️⃣ Фуллы и инфоповоды TikTok\n'
        '2️⃣ Альтушки и стримерши\n'
        '3️⃣ OnlyFans, Fansly и СНГ\n'
        '4️⃣ Скрытый архив: Лички\n'
        '5️⃣ Бонусный VIP-канал с редкими подборками\n'
        '✅ Платишь 1 раз — 65 000+ видео навсегда + «Стол заказов».',
        1190,
        620,
        0,
        '👑 Безлимит сразу на все 5 закрытых каналов (65 000+ видео):\n'
        '• TikTok, Альтушки, OnlyFans/СНГ, Лички + бонусный VIP-канал\n'
        '• Всего ~238 ₽ за канал навсегда без ежемесячных доплат\n'
        '• Включает закрытый «Стол заказов» для поиска нужных моделей',
    ),
    (
        '👑 Diamond Club: Все базы',
        '💎 Всё из Ultra VIP + прямой поиск сливов по никам (в 2 раза дешевле Black VIP):\n\n'
        '• Все 5 каналов Ultra VIP навсегда (65 000+ видео и фото)\n'
        '• Закрытая картотека — поиск любых моделей по имени и нику\n'
        '• Облачные MEGA-архивы для скачивания целыми папками\n'
        '• Приоритетный «Стол заказов» под твои запросы',
        2490,
        1290,
        0,
        '💎 Всё из Ultra VIP навсегда + приватная картотека поиска:\n'
        '• Все 5 каналов (65 000+ медиа) + MEGA-архивы целыми папками\n'
        '• Прямой поиск сливов по имени, нику и городу модели\n'
        '• Приоритетный поиск конкретных моделей через Стол заказов',
    ),
    (
        '🖤 Black VIP: Всё + Личный поиск',
        '🖤 Закрытый максимум: всё сразу + находим любую модель под тебя 24/7:\n\n'
        '• Все каналы, MEGA-архивы и скрытые коллекции без ограничений\n'
        '• Личный поиск и выгрузка фуллов конкретной девушки без очереди\n'
        '• Выгрузки в оригинальном качестве без сжатия и урезания\n'
        '• Пожизненные резервные зеркала и приоритетная выдача',
        4990,
        2590,
        0,
        '🖤 Абсолютный максимум резерва (для тех, кому нужно всё и сразу):\n'
        '• Полный доступ ко всем закрытым каналам, архивам и MEGA навсегда\n'
        '• Личный поиск и сборка паков любой нужной модели по запросу 24/7\n'
        '• Выдача в оригинале без сжатия + персональные резервные зеркала',
    ),
]

V25_SEED_UPGRADES = [
    (
        '🔥 Базовый: Фуллы из TikTok',
        '⚡️ Ищешь конкретный фулл из комментариев TikTok? Он уже здесь.\n\n'
        '• Оригиналы 3–12 мин со звуком — без цензуры, смайлов и вотермарок\n'
        '• Доступ к 1 базовому каналу навсегда (без подписок)\n'
        '• Все 490 ₽ сохраняются как твоя скидка на Ultra VIP',
        490,
        250,
        0,
        '📂 Внутри базового канала (3 840+ видео):\n'
        '• Закреплённый поиск по трендам и фулл-роликам из TikTok\n'
        '• Полные оригиналы 3–12 мин в Full HD со звуком без склеек\n'
        '• 1 канал твой навсегда — без разделов Альтушки, OnlyFans и Лички',
    ),
    (
        '🎀 Альтушки и стримерши [30д]',
        '🎀 Приватные видео тиктокерш, косплеерш и стримерш Рунета.\n\n'
        '• 18 000+ домашних роликов, сторис и интим-кружочков со звуком\n'
        '• Быстрый поиск по никам и типажам\n'
        '• 1 канал на 30 дней (все 5 навсегда в Ultra VIP = 1 190 ₽)',
        690,
        360,
        30,
        '📂 Внутри (18 400+ видео и фото со звуком):\n'
        '• Записи приватных эфиров, интим-кружочки и домашние архивы\n'
        '• Быстрый поиск по никам тиктокерш, альтушек и стримерш\n'
        '• Вход в 1 канал на 30 дней (при переходе на Ultra зачтём всю сумму)',
    ),
    (
        '🍑 OnlyFans & Fansly [30д]',
        '🔥 Выкупленные платные паки топовых моделей OnlyFans, Fansly и СНГ.\n\n'
        '• 25 000+ видео в Full HD / 4K, полные ролики 20–40 мин со звуком\n'
        '• Алфавитный поиск по именам и никам моделей\n'
        '• 1 канал на 30 дней (до вечного Ultra VIP всего +400 ₽)',
        790,
        410,
        30,
        '📂 Внутри (25 000+ медиа в Full HD / 4K):\n'
        '• Платные PPV-видео из личек моделей OnlyFans и Fansly\n'
        '• Полные 20–45 мин ролики без склеек и водяных знаков\n'
        '• В Ultra VIP (1 190 ₽) этот архив и ещё 4 канала доступны навсегда',
    ),
    (
        '😈 Скрытый архив: Лички [30д]',
        '🗄 Самый горячий раздел: реальные домашние сливы и лички.\n\n'
        '• 20 000+ домашних видео, голосовых и закрытых кружочков\n'
        '• Материалы, которые сносят на всех открытых сайтах\n'
        '• 1 канал на 30 дней (⚡️ за +300 ₽ в Ultra VIP откроешь ВСЕ 5 навсегда!)',
        890,
        460,
        30,
        '📂 Внутри (20 500+ закрытых сливов):\n'
        '• Приватные переписки с интим-кружочками и любительскими съёмками\n'
        '• Редкие домашние видео со звуком «только для парня»\n'
        '• ⚡️ За +300 ₽ вместо 1 канала на месяц берут Ultra VIP (все 5 навсегда)',
    ),
    (
        '⭐️ Ultra VIP: 5 каналов [ХИТ]',
        '👑 Главный пакет клуба — все 5 каналов сразу навсегда (~238 ₽/канал):\n\n'
        '1️⃣ Фуллы из TikTok\n'
        '2️⃣ Альтушки и стримерши\n'
        '3️⃣ OnlyFans, Fansly и СНГ\n'
        '4️⃣ Скрытый архив: Лички\n'
        '5️⃣ Бонусный VIP-канал с редкими подборками\n'
        '✅ Платишь 1 раз — все 65 000+ видео остаются у тебя навсегда.',
        1190,
        620,
        0,
        '👑 Безлимит на все 5 закрытых каналов сразу (65 000+ видео):\n'
        '• TikTok, Альтушки, OnlyFans/СНГ, Лички + бонусный VIP-канал\n'
        '• Всего ~238 ₽ за канал навсегда без ежемесячных доплат\n'
        '• Включает «Стол заказов» для поиска нужных моделей и сливов',
    ),
    (
        '👑 Diamond Club: Все базы',
        '💎 Всё из Ultra VIP + прямой поиск сливов по никам (в 2 раза дешевле Black VIP):\n\n'
        '• Все 5 каналов Ultra VIP навсегда (65 000+ видео и фото)\n'
        '• Закрытая картотека — поиск любых моделей по имени и нику\n'
        '• Облачные MEGA-архивы для скачивания целыми папками\n'
        '• Приоритетный «Стол заказов» под твои запросы',
        2490,
        1290,
        0,
        '💎 Всё из Ultra VIP навсегда + приватная база поиска:\n'
        '• Все 5 каналов (65 000+ медиа) + MEGA-архивы целыми папками\n'
        '• Прямой поиск сливов по имени, нику и городу модели\n'
        '• Приоритетный поиск конкретных моделей через Стол заказов',
    ),
    (
        '🖤 Black VIP: Всё + Личный поиск',
        '🖤 Закрытый максимум: всё сразу + находим любую модель под тебя 24/7:\n\n'
        '• Все каналы, MEGA-архивы и скрытые коллекции без ограничений\n'
        '• Личный поиск и выгрузка фуллов конкретной девушки без очереди\n'
        '• Выгрузки в оригинальном качестве без сжатия и урезания\n'
        '• Пожизненные зеркала доступа и моментальный приоритет',
        4990,
        2590,
        0,
        '🖤 Абсолютный максимум клуба (для тех, кому нужно всё и сразу):\n'
        '• Полный доступ ко всем закрытым каналам, архивам и MEGA навсегда\n'
        '• Личный поиск и сборка паков любой нужной модели по запросу 24/7\n'
        '• Выдача в оригинале без сжатия + персональные резервные зеркала',
    ),
]

V19_SEED_UPGRADES = [
    (
        '🔥 Базовый: Фуллы из TikTok',
        '⚡️ Ищешь конкретное вирусное видео из комментариев TikTok? Оно уже здесь.\n\n'
        '• Оригиналы 3–12 мин из трендов TikTok со звуком без блюра и водяных знаков\n'
        '• Пожизненный доступ к 1 базовому каналу без ежемесячных списаний\n'
        '• Без разделов «Альтушки», «OnlyFans», «Лички» и приватного поиска\n'
        '• 🎁 Все 490 ₽ за этот доступ навсегда сохраняются в скидку при апгрейде на Ultra VIP',
        '📂 Внутри базового канала сейчас (3 840+ медиа):\n'
        '• Навигатор по вирусным роликам и фулл-версиям из комментариев TikTok\n'
        '• Оригиналы 3–12 минут в Full HD без смайликов и склеек\n'
        '• Доступ к каналу остаётся с тобой навсегда, а 490 ₽ зачтутся при переходе на Ultra VIP',
    ),
    (
        '🎀 Альтушки и стримерши [30д]',
        '🎀 Приватные домашние архивы тиктокерш, косплеерш и стримерш Рунета.\n\n'
        '• 18 000+ домашних видео, закрытых сторис и кружочков «для своих»\n'
        '• Оригинальный звук, удобные хештеги по типажам и никам\n'
        '• Доступ к 1 каналу на 30 дней с ежедневным пополнением\n'
        '• 💡 1 канал на месяц = 690 ₽, а «Ultra VIP» открывает все 5 каналов навсегда за 1 190 ₽',
        '📂 Внутри архива (18 400+ видео и фото со звуком):\n'
        '• Поиск по хештегам и никам популярных тиктокерш, альтушек и стримерш\n'
        '• Записи приватных эфиров, интим-кружочки и домашние съёмки без цензуры\n'
        '• Совет: разница до вечного Ultra VIP (5 каналов сразу) минимальна — зачтём оплату при апгрейде',
    ),
    (
        '🍑 OnlyFans & Fansly [30д]',
        '🔥 Выкупленные платные паки топовых моделей OnlyFans, Fansly и блогерш СНГ.\n\n'
        '• 25 000+ медиа в Full HD / 4K, полные 20–40 мин ролики со звуком\n'
        '• Алфавитный указатель по никам без водяных знаков\n'
        '• Доступ к 1 каналу на 30 дней без автосписаний\n'
        '• 💡 Доплатив всего +400 ₽ в «Ultra VIP», заберёшь сразу все 5 каналов навсегда',
        '📂 Внутри архива (25 000+ медиа в Full HD / 4K):\n'
        '• Платные PPV-видео из личек моделей OnlyFans и Fansly\n'
        '• Полные 20–45 минутные ролики со звуком и алфавитный указатель\n'
        '• Подсказка: в Ultra VIP (1 190 ₽) этот архив + ещё 4 канала доступны навсегда',
    ),
    (
        '😈 Скрытый архив: Лички [30д]',
        '🗄 Редкий закрытый резерв: любительские съёмки и реальные сливы личек.\n\n'
        '• 20 000+ домашних видео, голосовых кружочков и закрытых переписок\n'
        '• Эксклюзивы, которые банят на всех открытых площадках\n'
        '• Доступ только к 1 каналу и ровно на 30 дней\n'
        '• ⚡️ В «Ultra VIP» (1 190 ₽) всего за +300 ₽ ты получаешь ЭТОТ + ещё 4 канала НАВСЕГДА!',
        '📂 Внутри закрытого резерва (20 500+ эксклюзивов):\n'
        '• Приватные переписки с голосовыми, интим-кружочками и домашним видео\n'
        '• Редкие любительские съёмки «только для парня» без цензуры\n'
        '• 🔥 Выбор 8 из 10: за +300 ₽ вместо 1 канала на месяц берут Ultra VIP (5 каналов навсегда)',
    ),
    (
        '⭐️ Ultra VIP: 5 каналов [ХИТ]',
        '👑 Самый выгодный пакет клуба — его выбирают 8 из 10 участников (~238 ₽ за канал!):\n\n'
        '1️⃣ Фуллы из TikTok без цензуры\n'
        '2️⃣ Альтушки и стримерши\n'
        '3️⃣ OnlyFans, Fansly и модели СНГ\n'
        '4️⃣ Скрытый архив: Лички\n'
        '5️⃣ Бонусный VIP-канал с редкими подборками\n'
        '✅ Разовый платёж — бессрочный доступ ко всем 5 каналам навсегда!',
        '👑 Полный безлимит сразу ко всем 5 закрытым каналам клуба (65 000+ медиа):\n'
        '• Все разделы: TikTok, Альтушки, OnlyFans/СНГ, Лички + бонусный VIP-канал\n'
        '• Выгода ×5: стоимость 1 канала внутри пакета — всего ~238 ₽ навсегда\n'
        '• Разовый вход без подписок + доступ к Столу заказов и обновлениям 24/7',
    ),
    (
        '👑 Diamond Club: Все базы',
        '💎 Элитный доступ ко всем базам и инструментам поиска (в 2 раза дешевле Black VIP!):\n\n'
        '• Все 5 каналов Ultra VIP навсегда (65 000+ видео и фото)\n'
        '• Закрытая картотека с прямым поиском сливов по имени и нику\n'
        '• Резервные облачные MEGA-архивы для скачивания целыми папками\n'
        '• Приоритетный Стол заказов — ищем нужную модель под тебя',
        '💎 Премиальный статус клуба (всё из Ultra VIP навсегда + закрытые базы):\n'
        '• Мгновенный вход во все 5 приватных каналов (65 000+ видео и фото)\n'
        '• Картотека с поиском сливов по имени, нику и городу + облачные MEGA-архивы\n'
        '• Приоритетный поиск конкретных моделей через Стол заказов (дешевле Black VIP в 2 раза)',
    ),
    (
        '🖤 Black VIP: Консьерж 24/7',
        '🖤 Максимальный приватный статус клуба с личным сопровождением (лимит мест):\n\n'
        '• Всё из Ultra VIP и Diamond Club + скрытые закрытые коллекции и зеркала\n'
        '• Личный консьерж-поиск нужных моделей и редких архивов 24/7 без очереди\n'
        '• Приватные облачные выгрузки в оригинале без сжатия и урезания качества\n'
        '• Пожизненная VIP-гарантия замены ссылок и приоритетная поддержка',
        '🖤 Закрытый консьерж-статус для тех, кто забирает всё и без ожидания:\n'
        '• Полный безлимит ко всем приватным каналам, архивам и MEGA-базам навсегда\n'
        '• Индивидуальный поиск и выгрузка паков конкретных моделей под ключ 24/7\n'
        '• Высший приоритет обслуживания и личные зеркала доступа',
    ),
]

V18_SEED_UPGRADES = [
    (
        '🔥 Фуллы из TikTok [24ч · Пробный]',
        '⚡️ Ищешь конкретное вирусное видео из комментариев TikTok? Оно уже здесь.\n\n'
        '• Нашумевшие фуллы из трендов TikTok за последний месяц\n'
        '• Оригиналы 3–12 минут без блюра, смайликов и склеек со звуком\n'
        '• Суточный доступ — удобно оценить формат клуба и навигацию',
        350,
        180,
        1,
        '📂 Внутри закрытого канала сейчас (3 840+ медиа):\n'
        '• Закреплённый навигатор по всем вирусным трендам TikTok этого месяца\n'
        '• Оригинальные ролики 3–12 минут со звуком без блюра и водяных знаков\n'
        '• Бонус: все 350 ₽ за этот доступ автоматически зачтутся при переходе на Ultra VIP',
    ),
    (
        '🎀 Альтушки и стримерши [30 дней]',
        '🎀 Приватные домашние архивы популярных тиктокерш, косплеерш и стримерш Рунета.\n\n'
        '• 18 000+ домашних видео и фотосетов (строго 18+)\n'
        '• Закрытые сторис, кружочки и съёмки «для своих»\n'
        '• Оригинальный звук без рекламы и пересылок\n'
        '• Доступ на 30 дней с ежедневным пополнением',
        690,
        360,
        30,
        '📂 Внутри архива (18 400+ видео и фото со звуком):\n'
        '• Поиск по хештегам и никам популярных тиктокерш, косплеерш и стримерш\n'
        '• Записи закрытых эфиров, приватные кружочки и домашние съёмки\n'
        '• Ежедневное пополнение свежими паками без рекламы и сторонних ботов',
    ),
    (
        '🍑 OnlyFans, Fansly & СНГ [30 дней]',
        '🔥 Полные платные паки топовых моделей OnlyFans, Fansly и известных блогерш.\n\n'
        '• 25 000+ медиа в Full HD и 4K\n'
        '• Полные 20–40 минутные приватные ролики со звуком\n'
        '• Алфавитный указатель по никам моделей',
        690,
        360,
        30,
        '📂 Внутри архива (25 000+ медиа в Full HD / 4K):\n'
        '• Выкупленные платные PPV-видео из личек моделей OnlyFans и Fansly\n'
        '• Полные 20–45 минутные ролики со звуком\n'
        '• Алфавитный указатель по именам моделей + доступ к Столу заказов',
    ),
    (
        '😈 Скрытый архив: Лички [30 дней]',
        '🗄 Самый редкий закрытый резерв: любительские съёмки, сливы личек и приватные переписки.\n\n'
        '• 20 000+ редких домашних видео и голосовых кружочков\n'
        '• Живые эмоции и настоящая домашняя атмосфера\n'
        '• Эксклюзивы, которых нет на открытых площадках',
        890,
        460,
        30,
        '📂 Внутри закрытого резерва (20 500+ эксклюзивов):\n'
        '• Приватные переписки с голосовыми, интим-кружочками и домашним видео\n'
        '• Редкие любительские съёмки без цензуры\n'
        '• Совет: разница до Ultra VIP всего 200 ₽ — там этот канал и ещё 4 базы идут навсегда',
    ),
    (
        '⭐️ Ultra VIP: 5 каналов [Навсегда]',
        '👑 Главный абонемент клуба: платишь один раз и получаешь бессрочный доступ сразу ко всем 5 закрытым каналам.\n\n'
        '• Канал «Фуллы из TikTok»\n'
        '• Канал «Альтушки и стримерши»\n'
        '• Канал «OnlyFans, Fansly & СНГ»\n'
        '• Канал «Скрытый архив: Лички»\n'
        '• Бонусный VIP-канал с редкими подборками\n'
        '• Доступ навсегда без продлений и доплат',
        1090,
        570,
        0,
        '👑 Полный безлимит сразу ко всем 5 закрытым каналам клуба (65 000+ медиа):\n'
        '1️⃣ Канал «Фуллы из TikTok + Хиты»\n'
        '2️⃣ Канал «Альтушки и стримерши»\n'
        '3️⃣ Канал «OnlyFans, Fansly & СНГ»\n'
        '4️⃣ Канал «Скрытый архив: Лички»\n'
        '5️⃣ Бонусный VIP-канал с редкими подборками\n'
        '✅ Разовый вход — доступ навсегда + Стол заказов',
    ),
    (
        '👑 Diamond Club: Все базы + Поиск',
        '💎 Закрытый клубный статус: всё из Ultra VIP навсегда плюс приватные инструменты поиска.\n\n'
        '• Бессрочный доступ ко всем 5 приватным каналам (65 000+ медиа)\n'
        '• Приватная картотека с прямым поиском по имени и нику\n'
        '• Доступ к Столу заказов — ищем нужные фуллы по твоему запросу\n'
        '• Резервная облачная база с загрузкой целыми папками',
        1990,
        1030,
        0,
        '💎 Максимальный статус клуба (всё из Ultra VIP + закрытые базы):\n'
        '• Мгновенный вход во все 5 приватных каналов (65 000+ медиа)\n'
        '• Картотека с поиском сливов по имени, нику и городу\n'
        '• Облачные MEGA-архивы — удобно скачивать папками\n'
        '• Приоритетный поиск нужной модели через Стол заказов',
    ),
]

V16_SEED_UPGRADES = [
    (
        '🔥 ТОТ САМЫЙ ФУЛЛ ИЗ TIKTOK [24 ЧАСА · ТЕСТ] 🔞',
        '⚡️ Ищешь конкретное вирусное видео из комментариев TikTok? Оно уже залито сюда!\n\n'
        '• Все нашумевшие фуллы из трендов TikTok за последний месяц\n'
        '• Полные версии без блюра, смайликов и склеек со звуком\n'
        '• Доступ на 24 часа — идеально, чтобы проверить честность бота за копейки!',
        '📂 Внутри закрытого канала прямо сейчас (3 840+ медиа):\n'
        '• Закреплённый навигатор по всем вирусным трендам TikTok этого месяца\n'
        '• Оригиналы роликов по 3–12 минут без блюра, смайликов и водяных знаков\n'
        '• 🎁 Бонус: все 350 ₽ за этот тариф автоматически зачтутся как скидка, если решишь перейти на вечный ULTRA VIP!',
    ),
    (
        '🎀 СОЧНЫЕ АЛЬТУШКИ, СТРИМЕРШИ & TIKTOK [30 ДНЕЙ]',
        '🎀 Приватные домашние архивы популярных тиктокерш, косплеерш, альтушек и стримерш Рунета!\n\n'
        '• 18 000+ домашних видео и фотосетов (строго 18+)\n'
        '• То, что девушки снимали «только для парня» и в закрытые сторис\n'
        '• Оригинальный звук, стоны и полное отсутствие цензуры\n'
        '• Доступ на 30 дней с ежедневным пополнением',
        '📂 Внутри архива (18 400+ видео и фото со звуком):\n'
        '• Именной поиск по хештегам популярных тиктокерш, косплеерш и стримерш\n'
        '• Записи закрытых эфиров, приватные кружочки из личек и домашние видео\n'
        '• Ежедневное пополнение свежими паками без рекламы и пересылок',
    ),
    (
        '🍑 ONLYFANS, FANSLY & БЛОГЕРШИ СНГ [30 ДНЕЙ]',
        '🔥 Полные платные паки топовых моделей OnlyFans, Fansly и известных инста-блогерш (стоимость подписок которых превышает $5 000)!\n\n'
        '• 25 000+ откровенных медиа в 4K / Full HD\n'
        '• Полные 20–40 минутные приватные видео со звуком\n'
        '• Удобный алфавитный указатель по именам моделей',
        '📂 Внутри архива (25 000+ медиа в Full HD / 4K):\n'
        '• Выкупленные платные PPV-видео из личных сообщений моделей OnlyFans и Fansly\n'
        '• Полные 20–45 минутные ролики со звуком (стоимость подписок более $5 000)\n'
        '• Алфавитный указатель по никам моделей + доступ к Столу заказов',
    ),
    (
        '😈 СКРЫТЫЙ АРХИВ: ДОМАШНЕЕ & СЛИВЫ ЛИЧЕК [30 ДНЕЙ]',
        '🗄 Самый откровенный закрытый архив редких любительских съёмок, слитых переписок и жёсткого эксклюзива без тормозов!\n\n'
        '• 20 000+ редких домашних материалов и кружочков\n'
        '• Настоящие эмоции, живой звук и домашняя атмосфера\n'
        '• Материалы, которые моментально банят на обычных сайтах',
        '📂 Внутри закрытого резерва (20 500+ эксклюзивов):\n'
        '• Реальные сливы переписок с голосовыми, интим-кружочками и домашним видео\n'
        '• Любительские съёмки «только для парня», которые банят на открытых сайтах\n'
        '• 💡 Совет: доплати всего 200 ₽ и забери «ULTRA VIP» — получишь этот канал + ещё 4 канала НАВСЕГДА!',
    ),
    (
        '⭐️ 🏆 ULTRA VIP: ВСЕ 5 КАНАЛОВ НАВСЕГДА [ТОП]',
        '👑 САМЫЙ ВЫГОДНЫЙ ТАРИФ! Заплати один раз и получи БЕЗЛИМИТНЫЙ ДОСТУП НАВСЕГДА сразу ко всем закрытым каналам проекта!\n\n'
        '• Канал «Тот самый фулл из TikTok»\n'
        '• Канал «Альтушки, Стримерши & TikTok»\n'
        '• Канал «OnlyFans, Fansly & Блогерши СНГ»\n'
        '• Канал «Скрытый архив: Домашнее & Лички»\n'
        '• Бонусный VIP-канал с редкими фетиш-паками\n'
        '• Доступ НАВСЕГДА без доплат!',
        '👑 Полный безлимит сразу ко всем 5 закрытым каналам клуба (65 000+ медиа):\n'
        '1️⃣ Канал «Тот самый фулл из TikTok + Хиты»\n'
        '2️⃣ Канал «Сочные Альтушки, Стримерши & TikTok»\n'
        '3️⃣ Канал «OnlyFans, Fansly & Блогерши СНГ»\n'
        '4️⃣ Канал «Скрытый архив: Домашнее & Сливы личек»\n'
        '5️⃣ Бонусный VIP-канал с редкими фетиш-паками\n'
        '✅ Платишь 1 раз — доступ НАВСЕГДА + Стол заказов!',
    ),
    (
        '👑 DIAMOND CLUB: ВСЕ БАЗЫ + ПОИСК ПО ИМЕНАМ [ТОП]',
        '💎 ЭЛИТНЫЙ ДОСТУП ДЛЯ ТЕХ, КТО ХОЧЕТ МАКСИМУМ! Включает всё из ULTRA VIP навсегда + закрытые инструменты:\n\n'
        '• Вечный доступ ко всем 5 приватным каналам (65 000+ медиа)\n'
        '• Приватный канал-картотека с поиском сливов по имени/нику\n'
        '• Доступ к закрытому столу заказов (находим фуллы по твоему запросу)\n'
        '• Приоритетная резервная база в облаке (можно скачивать целыми папками)',
        '💎 Максимальный статус клуба (всё из ULTRA VIP навсегда + закрытые базы):\n'
        '• Мгновенный вход во все 5 приватных каналов (65 000+ видео и фото)\n'
        '• Закрытый канал-картотека с прямым поиском сливов по имени, нику и городу\n'
        '• Прямые ссылки на облачные MEGA-архивы (можно скачивать целыми папками)\n'
        '• Приоритетный поиск нужной модели или тиктокерши через Стол заказов',
    ),
]

V12_SEED_UPGRADES = (
    (
        '🔥 ТОТ САМЫЙ ФУЛЛ ИЗ TIKTOK + ХИТЫ [24 ЧАСА] 🔞',
        '👀 Что внутри канала «Хиты TikTok (24 часа)»:\n\n'
        '• Закреплённый пост с быстрым поиском по вирусным трендам\n'
        '• Более 3 500 отборных роликов Full HD со звуком\n'
        '• Мгновенный вход сразу после оплаты без скрытых списаний',
    ),
    (
        '🎀 СОЧНЫЕ АЛЬТУШКИ, СТРИМЕРШИ & TIKTOK [18+]',
        '👀 Что внутри архива «Альтушки & Стримерши»:\n\n'
        '• Удобные хештеги по типажам и никам популярных тиктокерш\n'
        '• Домашний интим, кружочки из личек и закрытые эфиры\n'
        '• Доступ к скачиванию и просмотру в Full HD 24/7',
    ),
    (
        '🍑 ONLYFANS, FANSLY & БЛОГЕРШИ СНГ [FULL HD]',
        '👀 Что внутри архива «OnlyFans & Блогерши»:\n\n'
        '• Выкупленные платные PPV-видео из личных сообщений OnlyFans\n'
        '• Никаких водяных знаков на пол-экрана\n'
        '• Обновления свежими паками каждые 24 часа',
    ),
    (
        '😈 СКРЫТЫЙ АРХИВ: ДОМАШНЕЕ & СЛИВЫ ЛИЧЕК [18+]',
        '👀 Что внутри «Скрытого Архива»:\n\n'
        '• Реальные сливы переписок с голосовыми, кружочками и видео\n'
        '• Полная анонимность просмотра в закрытом канале\n'
        '• Ежедневный отбор самых жёстких новинок',
    ),
    (
        '⭐️ 🏆 ULTRA VIP: ВСЕ 5 КАНАЛОВ СРАЗУ [ТОП]',
        '👑 Почему 80% участников берут ULTRA VIP:\n\n'
        '1️⃣ Доплата всего 200 ₽ по сравнению с одним каналом, но доступ открывается КО ВСЕМ 5 КАНАЛАМ и НАВСЕГДА!\n'
        '2️⃣ Суммарно более 65 000 фото и видео со звуком\n'
        '3️⃣ Вечная гарантия обновления ссылок при переезде каналов',
    ),
    (
        '👑 DIAMOND CLUB: ВСЕ БАЗЫ + ПОИСК ПО ИМЕНАМ [ТОП]',
        '💎 Что даёт статус DIAMOND CLUB:\n\n'
        '• Полный анлим на все существующие и будущие каналы проекта\n'
        '• Персональный поиск нужной модели или тиктокерши через стол заказов\n'
        '• Прямые ссылки на облачные MEGA/Telegram-паки без сжатия качества',
    ),
)

LEGACY_TARIFF_DESCRIPTION_DIGESTS = {
    '💎 LUXURY: ЛАЙТ 😏🔞': 'a757f624da4e808e39b403002c310fa8ea8bff41caf9ac4d433860ae68a7c853',
    '💎 СОЧНЫЕ АЛЬТУШКИ 👧🏻🔞': '8d70cb624267c9d083e7210734b9f7568df51a1c77dce20f4a18a443a176a9ee',
    '💎 АРХИВ ТИКТОК 📱🔞💜': 'e521493607e9810e323579a885d5a301e711baecdf815760cf2fb06d8fc614c5',
    '💎 СКРЫТЫЙ АРХИВ 📱🔞': 'b3b9259a21f2f75db3928f5b8ff5bf1d9d015fb5258f465e6520945f8eca1f7a',
    '⭐️ 🏆 ВСЕ СРАЗУ (4 В 1) 🔞 [ТОП]': '769e112175b7195076ec23891894a6a0d9cd1cfbf72af49163bbbca0645a66ae',
}


class Database:
    def __init__(self, path: str):
        self.path = path

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=20)
        try:
            con.row_factory = sqlite3.Row
            con.create_function('UNICODE_UPPER', 1, str.upper, deterministic=True)
            con.execute('PRAGMA foreign_keys=ON')
            con.execute('PRAGMA busy_timeout=20000')
            yield con
        finally:
            con.close()

    def _transaction(self, work: Callable[[sqlite3.Connection], T]) -> T:
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                result = work(con)
                con.commit()
                return result
            except Exception:
                con.rollback()
                raise

    async def atomic(self, work: Callable[[sqlite3.Connection], T]) -> T:
        return await asyncio.to_thread(self._transaction, work)

    async def read(self, sql: str, args: tuple | dict = (), *, many: bool = False):
        def work():
            with self.connect() as con:
                result = con.execute(sql, args)
                rows = result.fetchall() if many else result.fetchone()
                return [dict(row) for row in rows] if many else dict(rows) if rows else None
        return await asyncio.to_thread(work)

    async def execute(self, sql: str, args: tuple | dict = ()) -> int:
        return await self.atomic(lambda con: con.execute(sql, args).rowcount)

    async def init(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        def setup():
            with self.connect() as con:
                con.execute('PRAGMA journal_mode=WAL')
                con.execute('PRAGMA foreign_keys=OFF')
                try:
                    con.executescript(SCHEMA)
                    con.execute('BEGIN IMMEDIATE')
                    for key, value in DEFAULT_SETTINGS.items():
                        con.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)', (key, value))

                    version = con.execute('PRAGMA user_version').fetchone()[0]
                    for table, additions in {
                        'users': {
                            'broadcast_opt_in': 'INTEGER NOT NULL DEFAULT 0',
                            'broadcast_opt_out': 'INTEGER NOT NULL DEFAULT 0',
                            'menu_revision': 'INTEGER NOT NULL DEFAULT 0',
                            'promo_expiry_reminder_sent': 'INTEGER NOT NULL DEFAULT 0',
                            'cold_nudge_sent': 'INTEGER NOT NULL DEFAULT 0',
                            'winback_push_sent': 'INTEGER NOT NULL DEFAULT 0',
                            'view_reminder_sent_at': 'TEXT',
                        },
                        'greeting_events': {'touched_at': 'TEXT'},
                        'tariffs': {'allow_no_channel': 'INTEGER NOT NULL DEFAULT 0',
                                    'delivery_urls': "TEXT NOT NULL DEFAULT '[]'",
                                    'preview_description': "TEXT NOT NULL DEFAULT ''",
                                    'preview_media_id': "TEXT NOT NULL DEFAULT ''",
                                    'preview_media_type': "TEXT NOT NULL DEFAULT ''"},
                        'purchases': {'expiry_notice_sent': 'INTEGER NOT NULL DEFAULT 0'},
                        'orders': {
                            'checkout_id': "TEXT NOT NULL DEFAULT ''",
                            'checkout_until': 'TEXT',
                            'invoice_sent_at': 'TEXT',
                            'invoice_sending_at': 'TEXT',
                            'refund_requested_at': 'TEXT',
                            'notice_retry_at': 'TEXT',
                            'reconcile_at': 'TEXT',
                            'delivery_urls': "TEXT NOT NULL DEFAULT '[]'",
                            'greeting_event_id': 'TEXT',
                            'order_kind': "TEXT NOT NULL DEFAULT 'tariff'",
                            'whale_alert_sent': 'INTEGER NOT NULL DEFAULT 0',
                        },
                        'grants': {
                            'revoke_retry_at': 'TEXT',
                            'unban_pending': 'INTEGER NOT NULL DEFAULT 0',
                        },
                    }.items():
                        columns = {row['name'] for row in con.execute(f'PRAGMA table_info({table})')}
                        for name, definition in additions.items():
                            if name not in columns:
                                con.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
                    con.execute('CREATE INDEX IF NOT EXISTS orders_greeting_event ON orders(greeting_event_id,status)')
                    con.execute('CREATE INDEX IF NOT EXISTS orders_user_created ON orders(user_id,created_at)')
                    con.execute("UPDATE settings SET value=COALESCE((SELECT MIN(created_at) FROM users),?) "
                                "WHERE key='project_started_at' AND value=''", (now(),))
                    if version < 1:
                        for key, digest in LEGACY_SECTION_DIGESTS.items():
                            value = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()[0]
                            if hashlib.sha256(value.encode('utf8')).hexdigest() == digest:
                                con.execute('UPDATE settings SET value=? WHERE key=?', (DEFAULT_SETTINGS[key], key))
                        con.execute("""UPDATE grants SET state='ready' WHERE state='expired' AND invite_url!=''
                            AND EXISTS (SELECT 1 FROM purchases p WHERE p.id=grants.purchase_id
                                AND strftime('%s',p.expires_at)-strftime('%s',grants.processing_at) BETWEEN 0 AND 15)""")
                    if version < 2:
                        legacy = con.execute("SELECT value FROM settings WHERE key='public_join_channel_id'").fetchone()
                        if legacy and legacy[0].lstrip('-').isdigit() and -(2 ** 52) < int(legacy[0]) < 0:
                            con.execute("""INSERT OR IGNORE INTO welcome_channels(chat_id,auto_approve)
                                VALUES (?,(SELECT value='1' FROM settings WHERE key='auto_approve_public_join'))""",
                                (int(legacy[0]),))
                    if version < 3:
                        for title, description, *_ in SEED_TARIFFS:
                            digest = LEGACY_TARIFF_DESCRIPTION_DIGESTS.get(title)
                            if not digest:
                                continue
                            for row in con.execute('SELECT id,description FROM tariffs WHERE title=?', (title,)).fetchall():
                                if hashlib.sha256(row['description'].encode('utf8')).hexdigest() == digest:
                                    con.execute('UPDATE tariffs SET description=? WHERE id=?', (description, row['id']))
                        con.execute('PRAGMA user_version=3')
                    if version < 4:
                        # Seed content belongs only in a new database. Existing product IDs
                        # may represent custom goods, prices and subscription durations.
                        con.execute('PRAGMA user_version=4')
                    if version < 5:
                        for key, digest in V4_SECTION_DIGESTS.items():
                            value = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()[0]
                            if hashlib.sha256(value.encode('utf8')).hexdigest() == digest:
                                con.execute('UPDATE settings SET value=? WHERE key=?', (DEFAULT_SETTINGS[key], key))
                        # Replace only the unedited demo promise; preserve all custom product fields.
                        for row in con.execute('SELECT id,preview_description FROM tariffs').fetchall():
                            if hashlib.sha256(row['preview_description'].encode('utf8')).hexdigest() == (
                                'e076fd6035d70b9ce717cc7e8a0956c1d04167a8f1e728c63298c63b9942cff7'
                            ):
                                con.execute('UPDATE tariffs SET preview_description=? WHERE id=?',
                                            (SEED_TARIFFS[2][5], row['id']))
                        con.execute('PRAGMA user_version=5')
                    if version < 7:
                        known_digests = {
                            'join_welcome': {
                                '9fc8abfb9306821435c50877034b3bd9d0ed64e3c850d335d7be7f07441bfa39',
                            },
                            'welcome': {
                                'fafa7cac09b9b8710e923b01362bdb597bf2a8f2f5896e0035532c1339066b10',
                                '8f37b9f4a31b2ffbed6e89aaf7849e7c744ff609bdeb04aa3adcdfb7725c4aaf',
                            },
                            'welcome_back': {
                                'b275a9c6e29e13900617c340ad2bdc1e83e1f955e0793c67ee976200e31d0f19',
                            },
                            'catalog': {
                                '085d1588c8ba4566c81406c0a3f4500b07b442be976275c0ebc2c59ecb8ac91d',
                                '315683ab27f5a84a64d736f20569d93945f37d1e4ee501652556ab689475fde2',
                            },
                            'help': {
                                'c7584a609e8e6c11987bf1a48cff3a2be3eb9cb76feaf2d57006f35a6fb5351d',
                                'b9d807788097596f4af821f8cfb7c552788c49a416b5c113b1b4a17a7c8b0733',
                                '277af6c4baf1ce952484b352a06ccc7f7614b54b0ca9177c73830449d49c1b3f',
                            },
                            'info': {
                                'c51f8d1818ac6aae257c6f15d0115ae259e3e16e103ceffea14b28646aee4864',
                                '1a1a189cf174b8f1917e36e472ac6ccb952b530ab7795968a19cb491c3441346',
                            },
                        }
                        for k, digests in known_digests.items():
                            row = con.execute('SELECT value FROM settings WHERE key=?', (k,)).fetchone()
                            if row:
                                val_hash = hashlib.sha256(row[0].encode('utf8')).hexdigest()
                                if val_hash in digests:
                                    con.execute('UPDATE settings SET value=? WHERE key=?', (DEFAULT_SETTINGS[k], k))

                        for title, desc, rub, stars, days, preview in SEED_TARIFFS:
                            con.execute("""UPDATE tariffs SET description=?, preview_description=?
                                           WHERE title=? AND (description='' OR description LIKE '%архив%' OR description LIKE '%эксклюзив%' OR description LIKE '%OnlyFans%')""",
                                        (desc, preview, title))
                        con.execute('PRAGMA user_version=7')

                    if version < 8:
                        v7_digests = {
                            'join_welcome': {
                                '9fc8abfb9306821435c50877034b3bd9d0ed64e3c850d335d7be7f07441bfa39',
                                '158aa0b68fa115a99607e9307aa3213b4c457f2d967f74aafb7b12fdfe82a891',
                            },
                            'welcome': {
                                'fafa7cac09b9b8710e923b01362bdb597bf2a8f2f5896e0035532c1339066b10',
                                '8f37b9f4a31b2ffbed6e89aaf7849e7c744ff609bdeb04aa3adcdfb7725c4aaf',
                                '978a328734935ae96e9e1364488a39cbee708b1bf88ccb56babb4e1ed5e8d0b8',
                            },
                            'welcome_back': {
                                'b275a9c6e29e13900617c340ad2bdc1e83e1f955e0793c67ee976200e31d0f19',
                                '1a623b27588d988059c0eb9d44caf2eec1118535ae9840ca36cab40086ac8aaf',
                            },
                            'catalog': {
                                '253765fc1e100465f04887b63fd4626a1163cf3b2ea95411b62c9529eb70b2c8',
                            },
                        }
                        for k, digests in v7_digests.items():
                            row = con.execute('SELECT value FROM settings WHERE key=?', (k,)).fetchone()
                            if row and hashlib.sha256(row[0].encode('utf8')).hexdigest() in digests:
                                con.execute('UPDATE settings SET value=? WHERE key=?', (DEFAULT_SETTINGS[k], k))
                        con.execute('PRAGMA user_version=8')

                    if version < 9:
                        help_digests = {
                            'c7584a609e8e6c11987bf1a48cff3a2be3eb9cb76feaf2d57006f35a6fb5351d',
                            'b9d807788097596f4af821f8cfb7c552788c49a416b5c113b1b4a17a7c8b0733',
                            '277af6c4baf1ce952484b352a06ccc7f7614b54b0ca9177c73830449d49c1b3f',
                            'e8cf7f8f096d0bf697cfde7f6b18027e0df9e59f132e2d51c3c6112db8114abf',
                        }
                        help_row = con.execute("SELECT value FROM settings WHERE key='help'").fetchone()
                        if help_row and hashlib.sha256(help_row[0].encode('utf8')).hexdigest() in help_digests:
                            con.execute("UPDATE settings SET value=? WHERE key='help'", (DEFAULT_SETTINGS['help'],))
                        for row in con.execute('SELECT id,description,preview_description FROM tariffs').fetchall():
                            if hashlib.sha256(row['description'].encode('utf8')).hexdigest() == (
                                'bce294e8fa4cd172dcb383e39f72d1304fcb25faa61c86746525d8af605d04ee'
                            ):
                                con.execute('UPDATE tariffs SET description=? WHERE id=?', (SEED_TARIFFS[0][1], row['id']))
                            if hashlib.sha256(row['preview_description'].encode('utf8')).hexdigest() == (
                                'f101ab22b37cbeaf713b4bd3d7e5323aaf1ac8fe155d65aeed32bd0561108f89'
                            ):
                                con.execute('UPDATE tariffs SET preview_description=? WHERE id=?',
                                            (SEED_TARIFFS[2][5], row['id']))
                        con.execute('PRAGMA user_version=9')

                    if version < 10:
                        welcome_row = con.execute("SELECT value FROM settings WHERE key='welcome'").fetchone()
                        if welcome_row and hashlib.sha256(welcome_row[0].encode('utf8')).hexdigest() == (
                            '6563d9af5e4719e5b68dc05b08334598bb9c1df5c5a0cd89e9b8584f617d87e6'
                        ):
                            con.execute("UPDATE settings SET value=? WHERE key='welcome'", (DEFAULT_SETTINGS['welcome'],))
                        for row in con.execute('SELECT id,description FROM tariffs').fetchall():
                            digest = hashlib.sha256(row['description'].encode('utf8')).hexdigest()
                            if digest == '32eb4fe6ecb0afcd7105d6160f33a72653f2297e2a2156880ebd1a66697015b3':
                                con.execute('UPDATE tariffs SET description=? WHERE id=?', (SEED_TARIFFS[1][1], row['id']))
                            elif digest == '37d099677d65f508b6c74514333ab1e53468bcbf24eb71702d4670b5b6261a94':
                                con.execute('UPDATE tariffs SET description=? WHERE id=?', (SEED_TARIFFS[3][1], row['id']))
                        con.execute('PRAGMA user_version=10')

                    if version < 11:
                        for key, digest in V10_SECTION_DIGESTS.items():
                            row = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
                            if row and hashlib.sha256(row[0].encode('utf8')).hexdigest() == digest:
                                con.execute('UPDATE settings SET value=? WHERE key=?', (DEFAULT_SETTINGS[key], key))
                        con.execute("UPDATE promos SET discount=150 WHERE code='LUXURYNEW' AND discount=200")
                        con.execute('PRAGMA user_version=11')

                    if version < 12:
                        for key, digest in V11_SECTION_DIGESTS.items():
                            row = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
                            if row and hashlib.sha256(row[0].encode('utf8')).hexdigest() == digest:
                                con.execute('UPDATE settings SET value=? WHERE key=?', (DEFAULT_SETTINGS[key], key))
                        for title, old_rub, old_stars, new_rub, new_stars in V11_SEED_PRICES:
                            con.execute('UPDATE tariffs SET rub=?,stars=? WHERE title=? AND rub=? AND stars=?',
                                        (new_rub, new_stars, title, old_rub, old_stars))
                        con.execute("""UPDATE promos SET enabled=0
                                       WHERE code='LUXURYNEW' AND discount=150 AND max_uses=1000 AND enabled=1""")
                        con.execute('PRAGMA user_version=12')

                    if version < 13:
                        cols = {row['name']: row for row in con.execute('PRAGMA table_info(orders)').fetchall()}
                        if cols['tariff_id']['notnull']:
                            con.execute('''CREATE TABLE orders_v13(
                                id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, tariff_id INTEGER,
                                title TEXT NOT NULL, amount_rub INTEGER NOT NULL, amount_stars INTEGER NOT NULL,
                                duration_days INTEGER NOT NULL, channels TEXT NOT NULL,
                                promo_code TEXT NOT NULL DEFAULT '',
                                provider TEXT NOT NULL, external_id TEXT NOT NULL DEFAULT '',
                                payment_url TEXT NOT NULL DEFAULT '', charge_id TEXT NOT NULL DEFAULT '',
                                status TEXT NOT NULL DEFAULT 'pending',
                                created_at TEXT NOT NULL, expires_at TEXT NOT NULL, paid_at TEXT,
                                notice_sent INTEGER NOT NULL DEFAULT 0,
                                checkout_id TEXT NOT NULL DEFAULT '',
                                checkout_until TEXT,
                                invoice_sent_at TEXT,
                                invoice_sending_at TEXT,
                                refund_requested_at TEXT,
                                notice_retry_at TEXT,
                                reconcile_at TEXT,
                                delivery_urls TEXT NOT NULL DEFAULT '[]',
                                greeting_event_id TEXT,
                                order_kind TEXT NOT NULL DEFAULT 'tariff',
                                whale_alert_sent INTEGER NOT NULL DEFAULT 0,
                                FOREIGN KEY(user_id) REFERENCES users(user_id),
                                FOREIGN KEY(tariff_id) REFERENCES tariffs(id) ON DELETE SET NULL
                            )''')
                            order_cols = (
                                'id, user_id, tariff_id, title, amount_rub, amount_stars, duration_days, channels, '
                                'promo_code, provider, external_id, payment_url, charge_id, status, created_at, '
                                'expires_at, paid_at, notice_sent, checkout_id, checkout_until, invoice_sent_at, '
                                'invoice_sending_at, refund_requested_at, notice_retry_at, reconcile_at, '
                                'delivery_urls, greeting_event_id, order_kind, whale_alert_sent'
                            )
                            con.execute(f'INSERT INTO orders_v13({order_cols}) SELECT {order_cols} FROM orders')
                            con.execute('DROP TABLE orders')
                            con.execute('ALTER TABLE orders_v13 RENAME TO orders')
                            con.execute('CREATE INDEX IF NOT EXISTS orders_pending ON orders(status,provider,created_at)')
                            con.execute('CREATE INDEX IF NOT EXISTS orders_promo ON orders(promo_code,status)')
                            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS orders_charge_unique ON orders(provider,charge_id) WHERE charge_id != ''")
                            con.execute('CREATE INDEX IF NOT EXISTS orders_greeting_event ON orders(greeting_event_id,status)')
                            con.execute('CREATE INDEX IF NOT EXISTS orders_user_created ON orders(user_id,created_at)')
                        # Do not retroactively emit Whale Radar or expiry upsell DMs for historical pre-v13 rows.
                        con.execute("UPDATE orders SET whale_alert_sent=1 WHERE status!='pending'")
                        con.execute("""UPDATE purchases SET expiry_notice_sent=1
                            WHERE expires_at IS NOT NULL AND expires_at<=?""", (now(),))
                        con.execute('PRAGMA user_version=13')

                    if version < 14:
                        tariff_cols = {row['name'] for row in con.execute('PRAGMA table_info(tariffs)').fetchall()}
                        if 'tier' not in tariff_cols:
                            con.execute("ALTER TABLE tariffs ADD COLUMN tier TEXT NOT NULL DEFAULT ''")
                        con.execute("UPDATE tariffs SET tier='diamond' WHERE tier='' AND UPPER(title) LIKE '%DIAMOND%'")
                        con.execute("UPDATE tariffs SET tier='ultra' WHERE tier='' AND UPPER(title) LIKE '%ULTRA%'")

                        order_cols = {row['name'] for row in con.execute('PRAGMA table_info(orders)').fetchall()}
                        if 'cart_reminder_sent' not in order_cols:
                            con.execute('ALTER TABLE orders ADD COLUMN cart_reminder_sent INTEGER NOT NULL DEFAULT 0')
                        con.execute(
                            "UPDATE orders SET cart_reminder_sent=2 WHERE status!='pending' OR created_at<=?",
                            (in_seconds(-4 * 3600),),
                        )
                        con.execute('PRAGMA user_version=14')

                    if version < 15:
                        for key, digest in V12_SECTION_DIGESTS.items():
                            row = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
                            if row and hashlib.sha256(row[0].encode('utf8')).hexdigest() == digest:
                                con.execute('UPDATE settings SET value=? WHERE key=?', (DEFAULT_SETTINGS[key], key))
                        for idx, (old_title, old_preview) in enumerate(V12_SEED_UPGRADES):
                            new_title = SEED_TARIFFS[idx][0]
                            new_preview = SEED_TARIFFS[idx][5]
                            if old_title != new_title:
                                con.execute('UPDATE tariffs SET title=? WHERE title=?', (new_title, old_title))
                            con.execute(
                                """UPDATE tariffs SET preview_description=?
                                   WHERE title IN (?,?) AND preview_description IN ('',?)""",
                                (new_preview, old_title, new_title, old_preview),
                            )
                        con.execute("UPDATE tariffs SET tier='diamond' WHERE tier='' AND UPPER(title) LIKE '%DIAMOND%'")
                        con.execute("UPDATE tariffs SET tier='ultra' WHERE tier='' AND UPPER(title) LIKE '%ULTRA%'")
                        con.execute('PRAGMA user_version=15')

                    if version < 16:
                        user_cols = {row['name'] for row in con.execute('PRAGMA table_info(users)').fetchall()}
                        if 'last_viewed_tariff_id' not in user_cols:
                            con.execute('ALTER TABLE users ADD COLUMN last_viewed_tariff_id INTEGER')
                        if 'last_viewed_at' not in user_cols:
                            con.execute('ALTER TABLE users ADD COLUMN last_viewed_at TEXT')
                        if 'view_reminder_sent' not in user_cols:
                            con.execute('ALTER TABLE users ADD COLUMN view_reminder_sent INTEGER NOT NULL DEFAULT 0')
                        con.execute('CREATE INDEX IF NOT EXISTS users_view_reminder ON users(view_reminder_sent, last_viewed_at)')

                        purchase_cols = {row['name'] for row in con.execute('PRAGMA table_info(purchases)').fetchall()}
                        if 'revoked_by_admin' not in purchase_cols:
                            con.execute('ALTER TABLE purchases ADD COLUMN revoked_by_admin INTEGER NOT NULL DEFAULT 0')
                        if 'post_join_upsell_sent' not in purchase_cols:
                            con.execute('ALTER TABLE purchases ADD COLUMN post_join_upsell_sent INTEGER NOT NULL DEFAULT 0')
                        if 'pre_expiry_upsell_sent' not in purchase_cols:
                            con.execute('ALTER TABLE purchases ADD COLUMN pre_expiry_upsell_sent INTEGER NOT NULL DEFAULT 0')

                        # Do not retroactively emit trial touchpoints for historical pre-v16 rows.
                        con.execute(
                            'UPDATE purchases SET post_join_upsell_sent=1 WHERE started_at<=? OR expires_at IS NULL',
                            (in_seconds(-2 * 3600),),
                        )
                        con.execute(
                            'UPDATE purchases SET pre_expiry_upsell_sent=1 WHERE expires_at IS NULL OR expires_at<=? OR expiry_notice_sent=1',
                            (now(),),
                        )
                        con.execute('PRAGMA user_version=16')

                    if version < 17:
                        for key, digest in V16_SECTION_DIGESTS.items():
                            row = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
                            if row and hashlib.sha256(row[0].encode('utf8')).hexdigest() == digest:
                                con.execute('UPDATE settings SET value=? WHERE key=?', (DEFAULT_SETTINGS[key], key))
                        for idx, (old_title, old_desc, old_prev) in enumerate(V16_SEED_UPGRADES):
                            new_title, new_desc, _, _, _, new_prev = SEED_TARIFFS[idx]
                            if old_title != new_title:
                                con.execute('UPDATE tariffs SET title=? WHERE title=?', (new_title, old_title))
                            con.execute(
                                'UPDATE tariffs SET description=? WHERE title IN (?,?) AND description=?',
                                (new_desc, old_title, new_title, old_desc),
                            )
                            con.execute(
                                'UPDATE tariffs SET preview_description=? WHERE title IN (?,?) AND preview_description IN (?,?)',
                                (new_prev, old_title, new_title, '', old_prev),
                            )
                        con.execute("UPDATE tariffs SET tier='diamond' WHERE tier='' AND UPPER(title) LIKE '%DIAMOND%'")
                        con.execute("UPDATE tariffs SET tier='ultra' WHERE tier='' AND UPPER(title) LIKE '%ULTRA%'")
                        con.execute('PRAGMA user_version=17')

                    if version < 18:
                        old_diamond_prev = (
                            '💎 Максимальный статус клуба (всё из Ultra VIP навсегда + закрытые базы):\n'
                            '• Мгновенный вход во все 5 приватных каналов (65 000+ видео и фото)\n'
                            '• Закрытая картотека с поиском сливов по имени, нику и городу\n'
                            '• Прямые ссылки на облачные MEGA-архивы (удобно скачивать папками)\n'
                            '• Приоритетный поиск нужной модели или тиктокерши через Стол заказов'
                        )
                        con.execute(
                            'UPDATE tariffs SET preview_description=? WHERE title=? AND preview_description=?',
                            (SEED_TARIFFS[5][5], SEED_TARIFFS[5][0], old_diamond_prev),
                        )
                        con.execute('PRAGMA user_version=18')

                    if version < 19:
                        for key, digest in V18_SECTION_DIGESTS.items():
                            row = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
                            if row and hashlib.sha256(row[0].encode('utf8')).hexdigest() == digest:
                                con.execute('UPDATE settings SET value=? WHERE key=?', (DEFAULT_SETTINGS[key], key))
                        for idx, (old_title, old_desc, old_rub, old_stars, old_days, old_prev) in enumerate(V18_SEED_UPGRADES):
                            new_title, new_desc, new_rub, new_stars, new_days, new_prev = SEED_TARIFFS[idx]
                            if old_title != new_title:
                                con.execute('UPDATE tariffs SET title=? WHERE title=?', (new_title, old_title))
                            con.execute(
                                'UPDATE tariffs SET description=? WHERE title IN (?,?) AND description=?',
                                (new_desc, old_title, new_title, old_desc),
                            )
                            con.execute(
                                'UPDATE tariffs SET preview_description=? WHERE title IN (?,?) AND preview_description IN (?,?)',
                                (new_prev, old_title, new_title, '', old_prev),
                            )
                            con.execute(
                                'UPDATE tariffs SET rub=?, stars=? WHERE title IN (?,?) AND rub=? AND stars=?',
                                (new_rub, new_stars, old_title, new_title, old_rub, old_stars),
                            )
                            if old_days != new_days:
                                con.execute(
                                    """UPDATE tariffs SET duration_days=?
                                       WHERE title IN (?,?) AND duration_days=? AND (delivery_urls='[]' OR ?=0)""",
                                    (new_days, old_title, new_title, old_days, new_days),
                                )
                        seed_titles = tuple({t[0] for t in SEED_TARIFFS[:6]} | {u[0] for u in V18_SEED_UPGRADES})
                        seed_placeholders = ','.join('?' for _ in seed_titles)
                        seed_matches = con.execute(
                            f'SELECT COUNT(*) FROM tariffs WHERE title IN ({seed_placeholders})',
                            seed_titles,
                        ).fetchone()[0]
                        has_anchor = con.execute(
                            "SELECT 1 FROM tariffs WHERE UPPER(title) LIKE '%BLACK%' OR UPPER(title) LIKE '%КОНСЬЕРЖ%' LIMIT 1"
                        ).fetchone()
                        if not has_anchor and seed_matches >= 2:
                            con.execute(
                                'INSERT INTO tariffs(title,description,rub,stars,duration_days,preview_description) VALUES(?,?,?,?,?,?)',
                                SEED_TARIFFS[6],
                            )
                        con.execute("UPDATE tariffs SET tier='diamond' WHERE tier='' AND UPPER(title) LIKE '%DIAMOND%'")
                        con.execute("UPDATE tariffs SET tier='ultra' WHERE tier='' AND UPPER(title) LIKE '%ULTRA%'")
                        con.execute('UPDATE purchases SET post_join_upsell_sent=1 WHERE started_at<=?', (in_seconds(-2 * 3600),))
                        con.execute('PRAGMA user_version=19')

                    if version < 20:
                        for key, digest in V19_SECTION_DIGESTS.items():
                            row = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
                            if row and hashlib.sha256(row[0].encode('utf8')).hexdigest() == digest:
                                con.execute('UPDATE settings SET value=? WHERE key=?', (DEFAULT_SETTINGS[key], key))
                        for idx, (old_title, old_desc, old_prev) in enumerate(V19_SEED_UPGRADES):
                            new_title, new_desc, _, _, _, new_prev = SEED_TARIFFS[idx]
                            if old_title != new_title:
                                con.execute('UPDATE tariffs SET title=? WHERE title=?', (new_title, old_title))
                            con.execute(
                                'UPDATE tariffs SET description=? WHERE title IN (?,?) AND description=?',
                                (new_desc, old_title, new_title, old_desc),
                            )
                            con.execute(
                                'UPDATE tariffs SET preview_description=? WHERE title IN (?,?) AND preview_description IN (?,?)',
                                (new_prev, old_title, new_title, '', old_prev),
                            )
                        con.execute('PRAGMA user_version=20')

                    if version < 21:
                        for key, digest in V20_SECTION_DIGESTS.items():
                            row = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
                            if row and hashlib.sha256(row[0].encode('utf8')).hexdigest() == digest:
                                con.execute('UPDATE settings SET value=? WHERE key=?', (DEFAULT_SETTINGS[key], key))
                        con.execute('PRAGMA user_version=21')

                    if version < 22:
                        for key, digest in V21_SECTION_DIGESTS.items():
                            row = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
                            if row and hashlib.sha256(row[0].encode('utf8')).hexdigest() == digest:
                                con.execute('UPDATE settings SET value=? WHERE key=?', (DEFAULT_SETTINGS[key], key))
                        con.execute('PRAGMA user_version=22')

                    if version < 23:
                        user_cols = {row['name'] for row in con.execute('PRAGMA table_info(users)').fetchall()}
                        if 'promo_push_sent' not in user_cols:
                            con.execute('ALTER TABLE users ADD COLUMN promo_push_sent INTEGER NOT NULL DEFAULT 0')
                        if 'promo_push_code' not in user_cols:
                            con.execute("ALTER TABLE users ADD COLUMN promo_push_code TEXT NOT NULL DEFAULT ''")
                        if 'promo_push_sent_at' not in user_cols:
                            con.execute('ALTER TABLE users ADD COLUMN promo_push_sent_at TEXT')
                        if 'last_review_push_at' not in user_cols:
                            con.execute('ALTER TABLE users ADD COLUMN last_review_push_at TEXT')
                        con.execute('CREATE INDEX IF NOT EXISTS users_promo_push ON users(promo_push_sent, created_at)')
                        con.execute('CREATE INDEX IF NOT EXISTS users_review_push ON users(last_review_push_at, created_at)')

                        promo_cols = {row['name'] for row in con.execute('PRAGMA table_info(promos)').fetchall()}
                        if 'owner_user_id' not in promo_cols:
                            con.execute('ALTER TABLE promos ADD COLUMN owner_user_id INTEGER')
                        if 'expires_at' not in promo_cols:
                            con.execute('ALTER TABLE promos ADD COLUMN expires_at TEXT')
                        if 'is_personal' not in promo_cols:
                            con.execute('ALTER TABLE promos ADD COLUMN is_personal INTEGER NOT NULL DEFAULT 0')
                        con.execute('CREATE INDEX IF NOT EXISTS promos_owner ON promos(owner_user_id, is_personal)')
                        con.execute('PRAGMA user_version=23')

                    if version < 24:
                        user_cols = {row['name'] for row in con.execute('PRAGMA table_info(users)')}
                        if 'first_start_at' not in user_cols:
                            con.execute('ALTER TABLE users ADD COLUMN first_start_at TEXT')
                            # Earlier versions used registration time as the /start time.
                            con.execute('UPDATE users SET first_start_at=created_at')
                        for column in ('promo_push_retry_at', 'review_push_retry_at'):
                            if column not in user_cols:
                                con.execute(f'ALTER TABLE users ADD COLUMN {column} TEXT')
                        con.execute('CREATE INDEX IF NOT EXISTS users_promo_start ON users(promo_push_sent, first_start_at)')
                        con.execute('PRAGMA user_version=24')
                    if version < 25:
                        promo_cols = {row['name'] for row in con.execute('PRAGMA table_info(promos)')}
                        if 'discount_percent' not in promo_cols:
                            con.execute(
                                'ALTER TABLE promos ADD COLUMN discount_percent INTEGER NOT NULL DEFAULT 0 '
                                'CHECK(discount_percent>=0 AND discount_percent<=99)'
                            )
                        con.execute('PRAGMA user_version=25')

                    if version < 26:
                        for key, digest in V25_SECTION_DIGESTS.items():
                            row = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
                            if row and hashlib.sha256(row[0].encode('utf8')).hexdigest() == digest:
                                con.execute('UPDATE settings SET value=? WHERE key=?', (DEFAULT_SETTINGS[key], key))
                        for idx, (old_title, old_desc, old_rub, old_stars, old_days, old_prev) in enumerate(V25_SEED_UPGRADES):
                            new_title, new_desc, new_rub, new_stars, new_days, new_prev = SEED_TARIFFS[idx]
                            if old_title != new_title:
                                con.execute('UPDATE tariffs SET title=? WHERE title=?', (new_title, old_title))
                            con.execute(
                                'UPDATE tariffs SET description=? WHERE title IN (?,?) AND description=?',
                                (new_desc, old_title, new_title, old_desc),
                            )
                            con.execute(
                                'UPDATE tariffs SET preview_description=? WHERE title IN (?,?) AND preview_description IN (?,?)',
                                (new_prev, old_title, new_title, '', old_prev),
                            )
                            con.execute(
                                'UPDATE tariffs SET rub=?, stars=? WHERE title IN (?,?) AND rub=? AND stars=?',
                                (new_rub, new_stars, old_title, new_title, old_rub, old_stars),
                            )
                            if old_days != new_days:
                                con.execute(
                                    """UPDATE tariffs SET duration_days=?
                                       WHERE title IN (?,?) AND duration_days=?
                                         AND (delivery_urls='[]' OR ?=0
                                              OR NOT EXISTS (SELECT 1 FROM purchases p WHERE p.tariff_id=tariffs.id))""",
                                    (new_days, old_title, new_title, old_days, new_days),
                                )
                        con.execute("UPDATE tariffs SET tier='diamond' WHERE tier='' AND UPPER(title) LIKE '%DIAMOND%'")
                        con.execute("UPDATE tariffs SET tier='ultra' WHERE tier='' AND UPPER(title) LIKE '%ULTRA%'")
                        con.execute('PRAGMA user_version=26')

                    if version < 27:
                        for key, digest in V26_SECTION_DIGESTS.items():
                            row = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
                            if row and hashlib.sha256(row[0].encode('utf8')).hexdigest() == digest:
                                con.execute('UPDATE settings SET value=? WHERE key=?', (DEFAULT_SETTINGS[key], key))
                        for idx, (old_title, old_desc, old_rub, old_stars, old_days, old_prev) in enumerate(V26_SEED_UPGRADES):
                            new_title, new_desc, new_rub, new_stars, new_days, new_prev = SEED_TARIFFS[idx]
                            if old_title != new_title:
                                con.execute('UPDATE tariffs SET title=? WHERE title=?', (new_title, old_title))
                            con.execute(
                                'UPDATE tariffs SET description=? WHERE title IN (?,?) AND description=?',
                                (new_desc, old_title, new_title, old_desc),
                            )
                            con.execute(
                                'UPDATE tariffs SET preview_description=? WHERE title IN (?,?) AND preview_description IN (?,?)',
                                (new_prev, old_title, new_title, '', old_prev),
                            )
                            con.execute(
                                'UPDATE tariffs SET rub=?, stars=? WHERE title IN (?,?) AND rub=? AND stars=?',
                                (new_rub, new_stars, old_title, new_title, old_rub, old_stars),
                            )
                        con.execute(
                            "UPDATE tariffs SET title=? WHERE title IN ('🖤 Black VIP: Всё + Личный поиск', '🖤 Black VIP: Консьерж 24/7')",
                            (SEED_TARIFFS[6][0],),
                        )
                        con.execute("DELETE FROM settings WHERE key='model_url'")
                        con.execute('DROP INDEX IF EXISTS custom_requests_user')
                        con.execute('DROP INDEX IF EXISTS orders_custom_invoice')
                        con.execute('DROP TABLE IF EXISTS custom_requests')
                        con.execute('DROP TABLE IF EXISTS custom_invoices')
                        order_col_names = {
                            row['name'] for row in con.execute('PRAGMA table_info(orders)').fetchall()
                        }
                        if 'custom_invoice_id' in order_col_names:
                            con.execute('ALTER TABLE orders DROP COLUMN custom_invoice_id')
                        con.execute('PRAGMA user_version=27')

                    if version < 28:
                        for key, digest in V27_SECTION_DIGESTS.items():
                            row = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
                            if row and hashlib.sha256(row[0].encode('utf8')).hexdigest() == digest:
                                con.execute('UPDATE settings SET value=? WHERE key=?', (DEFAULT_SETTINGS[key], key))
                        con.execute('PRAGMA user_version=28')

                    if version < 29:
                        for key, digest in V28_SETTINGS_DIGESTS.items():
                            row = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
                            if row and hashlib.sha256(row[0].encode('utf8')).hexdigest() == digest:
                                con.execute('UPDATE settings SET value=? WHERE key=?', (DEFAULT_SETTINGS[key], key))
                        con.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='menu_revision'")
                        con.execute('PRAGMA user_version=29')

                    if version < 30:
                        row = con.execute("SELECT value FROM settings WHERE key='promo_push_text'").fetchone()
                        if row and hashlib.sha256(row[0].encode('utf8')).hexdigest() == V29_PROMO_TEXT_DIGEST:
                            con.execute("UPDATE settings SET value=? WHERE key='promo_push_text'",
                                        (DEFAULT_SETTINGS['promo_push_text'],))
                        con.execute("UPDATE tariffs SET tier='diamond' WHERE title=? AND tier=''",
                                    (SEED_TARIFFS[6][0],))
                        con.execute('PRAGMA user_version=30')

                    recovery_row = con.execute(
                        "SELECT value FROM settings WHERE key='payment_gap_recovery_v1'"
                    ).fetchone()
                    if not recovery_row or recovery_row['value'] != '1':
                        # One-time backfill when upgrading existing databases: rewind a skipped
                        # Stars cursor and return erroneously failed issued invoices to reconciliation.
                        con.execute("UPDATE settings SET value='0' WHERE key='stars_ledger_offset'")
                        con.execute(
                            """UPDATE orders
                                  SET status='expired', reconcile_at=NULL
                                WHERE status='failed'
                                  AND provider IN ('cryptopay', '2328')
                                  AND (external_id!='' OR payment_url!='')"""
                        )
                        con.execute(
                            "INSERT OR REPLACE INTO settings(key, value) VALUES('payment_gap_recovery_v1', '1')"
                        )
                    recovery_v2_row = con.execute(
                        "SELECT value FROM settings WHERE key='payment_gap_recovery_v2'"
                    ).fetchone()
                    if not recovery_v2_row or recovery_v2_row['value'] != '1':
                        archive_cutoff = in_seconds(-3 * 86400)
                        con.execute(
                            """UPDATE orders
                                  SET reconcile_at=NULL
                                WHERE provider IN ('cryptopay', '2328')
                                  AND status IN ('pending', 'expired')
                                  AND (external_id != '' OR payment_url != '')
                                  AND created_at <= ?""",
                            (archive_cutoff,),
                        )
                        con.execute(
                            "INSERT OR REPLACE INTO settings(key, value) VALUES('payment_gap_recovery_v2', '1')"
                        )

                    if not con.execute('SELECT 1 FROM tariffs LIMIT 1').fetchone():
                        tiers = {5: 'ultra', 6: 'ultra', 7: 'diamond'}
                        con.executemany(
                            """INSERT INTO tariffs(title,description,rub,stars,duration_days,preview_description,
                               active,tier) VALUES(?,?,?,?,?,?,?,?)""",
                            (tariff + (int(idx <= 5), tiers.get(idx, ''))
                             for idx, tariff in enumerate(SEED_TARIFFS, 1)),
                        )

                    # Seed dormant promo code for broadcast campaigns; disabled on entry
                    # so cold traffic checks out at storefront prices without extra friction.
                    con.execute('''INSERT OR IGNORE INTO promos(code, discount, max_uses, enabled)
                                   VALUES('LUXURYNEW', 150, 1000, 0)''')

                    con.commit()
                except Exception:
                    con.rollback()
                    raise
                finally:
                    con.execute('PRAGMA foreign_keys=ON')
        await asyncio.to_thread(setup)

    async def setting(self, key: str) -> dict | None:
        return await self.read('SELECT * FROM settings WHERE key=?', (key,))

    async def project_name(self) -> str:
        return (await self.setting('project_name') or {}).get('value') or DEFAULT_PROJECT_NAME

    async def paid_channel(self, chat_id: int) -> bool:
        return bool(await self.read("""SELECT 1 FROM grants WHERE chat_id=?
            UNION ALL SELECT 1 FROM product_channel_links WHERE chat_id=?
            UNION ALL SELECT 1 FROM tariffs
                WHERE ',' || replace(replace(channel_ids, ' ', ''), char(10), ',') || ',' LIKE ?
            UNION ALL SELECT 1 FROM orders o, json_each(o.channels) c
                WHERE o.status IN ('pending','review','paid') AND c.value=? LIMIT 1""",
            (chat_id, chat_id, f'%,{chat_id},%', chat_id)))

    async def set_setting(self, key: str, value: str | None = None,
                          media_id: str | None = None, media_type: str | None = None) -> None:
        def txn(con):
            con.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)', (key, ''))
            if value is not None:
                if key in ('project_name', 'reviews_visible', 'help_visible'):
                    old = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()[0]
                    if key == 'project_name':
                        old = old or DEFAULT_PROJECT_NAME
                        if old != value:
                            _tokenize_brand_mentions(con, old)
                    elif key == 'help_visible':
                        old = old or '1'
                    if old != value:
                        con.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='menu_revision'")
                con.execute('UPDATE settings SET value=? WHERE key=?', (value, key))
            if media_id is not None:
                con.execute('UPDATE settings SET media_id=?, media_type=? WHERE key=?',
                            (media_id, media_type or '', key))
        await self.atomic(txn)

    async def register(self, user_id: int, username: str | None = None, name: str | None = None,
                       *, via_start: bool = False):
        def txn(con):
            timestamp = now()
            con.execute('''INSERT INTO users(user_id,username,name,created_at,last_seen,first_start_at)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                        username=CASE WHEN ? IS NOT NULL THEN excluded.username ELSE users.username END,
                        name=CASE WHEN ? IS NOT NULL THEN excluded.name ELSE users.name END,
                        last_seen=excluded.last_seen,
                        first_start_at=COALESCE(users.first_start_at,excluded.first_start_at),
                        promo_push_retry_at=CASE WHEN ? AND COALESCE(users.promo_push_retry_at,'')>='9999-' THEN NULL ELSE users.promo_push_retry_at END,
                        review_push_retry_at=CASE WHEN ? AND COALESCE(users.review_push_retry_at,'')>='9999-' THEN NULL ELSE users.review_push_retry_at END''',
                        (user_id, username or '', name or '', timestamp, timestamp,
                         timestamp if via_start else None, username, name,
                         1 if via_start else 0, 1 if via_start else 0))
        await self.atomic(txn)

    async def audit(self, admin: int, action: str, subject: str):
        await self.execute('INSERT INTO audit(actor,action,subject,created_at) VALUES(?,?,?,?)',
                           (admin,action,subject,now()))


async def contacts_visible(db: Database) -> bool:
    return (await db.setting('contacts_visible') or {}).get('value', '1') == '1'


async def reviews_visible(db: Database) -> bool:
    return (await db.setting('reviews_visible') or {}).get('value') == '1'


async def help_visible(db: Database) -> bool:
    return (await db.setting('help_visible') or {}).get('value', '1') == '1'
