"""Telegram UI primitives: emojis, markup, reply and inline keyboards."""
from __future__ import annotations

from datetime import datetime, timezone
import html
from html.parser import HTMLParser
import json
import re
from typing import Literal
from urllib.parse import urlparse

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, Message,
)

from .db import DEFAULT_PROJECT_NAME, Database, DEFAULT_SETTINGS
from .service import channels_of, tariff_tier_kind

MENU_CATALOG = '📂 Каталог фуллов'
MENU_ACCESS = '🔓 Мои доступы'
MENU_REVIEWS = '⭐ Отзывы и обновления'
MENU_HELP = '💬 Помощь / Промо'

MENU_LEGACY_CATALOG = '🔞 Каталог фуллов'
MENU_PROFILE = '👤 Профиль / Промокод'
MENU_HELP_SIMPLE = '💬 Помощь'
MENU_INFO = '💎 Гарантии клуба'
MENU_STATS_PREFIX = '🔥 Live-покупки'

_MENU_EXACT_ITEMS = frozenset({
    MENU_CATALOG, MENU_ACCESS, MENU_REVIEWS, MENU_HELP,
    MENU_LEGACY_CATALOG, MENU_PROFILE, MENU_HELP_SIMPLE, MENU_INFO,
    '📂 Каталог фуллов', '🔓 Мои доступы', '⭐ Отзывы и обновления', '💬 Помощь / Промо',
    '🔞 Каталог фуллов', '👤 Профиль / Промокод', '💬 Помощь / Отзывы', '💎 Гарантии клуба',
    '🔐 Доступ к контенту', '👤 Мой аккаунт', '❓ Помощь/Отзывы', 'ℹ️ Информация',
    '◈ Каталог', 'Каталог', '◇ Мой аккаунт', 'Мой аккаунт',
    '☏ Помощь', 'Помощь', '≡ Информация', 'Информация',
})
_MENU_PREFIXES = (MENU_STATS_PREFIX, '💜 Статистика', '▥ Статистика', 'Статистика', '/')


def is_menu_navigation_text(text: str | None) -> bool:
    """Return True if private chat text corresponds to persistent menu navigation or a command."""
    if not text:
        return False
    return text in _MENU_EXACT_ITEMS or text.startswith(_MENU_PREFIXES)

ButtonStyle = Literal['primary', 'success', 'danger']


def esc(value) -> str:
    return html.escape(str(value or ''), quote=True)


async def rendered_section(db: Database, key: str, **values: str) -> dict:
    """Substitute section variables once, escaping their values for Telegram HTML."""
    row = await db.setting(key) or {}
    project = await db.project_name()
    variables = {'project_name': project, 'name': '', 'channel': '', 'bot_username': '', **values}
    template = row.get('value') or DEFAULT_SETTINGS.get(key, '')
    content = re.sub(r'\{(project_name|name|channel|bot_username)\}',
                     lambda match: esc(variables[match[1]]), template)
    return {**row, 'value': content}


def duration(days: int) -> str:
    return f'{days} дн.' if days else 'НАВСЕГДА'


MEDIA_TYPES = ('photo', 'video', 'animation')


def has_product_photo(tariff: dict) -> bool:
    return tariff.get('media_type') in MEDIA_TYPES and bool((tariff.get('media_id') or '').strip())


def tariff_view_media(tariff: dict, *, details: bool = False) -> tuple[str, str]:
    """Return (media_id, media_type) for tariff card or its 'what is inside' preview."""
    if details:
        preview_id = (tariff.get('preview_media_id') or '').strip()
        preview_type = tariff.get('preview_media_type') or ''
        if preview_id and preview_type in MEDIA_TYPES:
            return preview_id, preview_type
    return (tariff.get('media_id') or '').strip(), tariff.get('media_type') or ''


def fmt_rub(amount: int | float) -> str:
    """Format RUB amount with space thousands separators for scannable buttons."""
    return f'{int(amount):,}'.replace(',', ' ')


def _utf16_len(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def tariff_channel_count(tariff: dict) -> int:
    """Return channel count declared in a tariff or order payload."""
    raw_ids = tariff.get('channel_ids')
    if isinstance(raw_ids, list) and raw_ids:
        return len(raw_ids)
    if isinstance(raw_ids, str) and raw_ids.strip():
        stripped = raw_ids.strip()
        if stripped.startswith('['):
            try:
                items = json.loads(stripped)
                if items:
                    return len(items)
            except (TypeError, ValueError):
                pass
        else:
            try:
                ids = channels_of(stripped)
                if ids:
                    return len(ids)
            except ValueError:
                parts = [p for p in stripped.replace('\n', ',').split(',') if p.strip()]
                if parts:
                    return len(parts)
    for key in ('channels', 'delivery_urls'):
        raw = tariff.get(key)
        if not raw:
            continue
        try:
            items = json.loads(raw) if isinstance(raw, str) else raw
            if items:
                return len(items)
        except (TypeError, ValueError):
            pass
    return 0


def plural_channels(count: int) -> str:
    n = abs(int(count))
    if 11 <= n % 100 <= 19:
        word = 'каналов'
    elif n % 10 == 1:
        word = 'канал'
    elif 2 <= n % 10 <= 4:
        word = 'канала'
    else:
        word = 'каналов'
    return f'{n} {word}'


def duration_phrase(days: int | None) -> str:
    d = int(days or 0)
    if d <= 0:
        return 'навсегда'
    if d == 1:
        return 'на 24 часа'
    return f'на {d} дн.'


def remaining_time_label(expires_at: str | None, *, current: datetime | None = None) -> str:
    """Return compact human-readable remaining time until an ISO UTC timestamp, or '' if expired/empty."""
    if not expires_at:
        return ''
    try:
        dt = datetime.fromisoformat(expires_at)
    except ValueError:
        return ''
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now_dt = current or datetime.now(timezone.utc)
    seconds = int((dt - now_dt).total_seconds())
    if seconds <= 0:
        return ''
    minutes = max(1, seconds // 60)
    if minutes < 60:
        return f'{minutes} мин.'
    hours, rem_mins = divmod(minutes, 60)
    if hours < 24:
        return f'{hours} ч. {rem_mins} мин.' if rem_mins else f'{hours} ч.'
    days = hours // 24
    return f'~{days} дн.'


_LEADING_DECOR_RE = re.compile(r'^[^\wА-Яа-яЁё«"]+')
_PROMO_TAG_RE = re.compile(r'\s*\[(?:ХИТ|ТОП)\]\s*$', re.IGNORECASE)


def short_tariff_name(title: str | None, fallback: str = 'тариф', max_len: int = 30) -> str:
    """Return compact readable tariff title for buttons while keeping the main label intact."""
    clean = ' '.join((title or '').split()).strip()
    if not clean:
        return fallback
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1].rstrip() + '…'


def button_tariff_name(title: str | None, fallback: str = 'тариф', max_len: int = 30) -> str:
    """Return tariff title stripped of leading decorative emoji and [ХИТ]/[ТОП] tags for prefixed buttons."""
    clean = ' '.join((title or '').split()).strip()
    clean = _PROMO_TAG_RE.sub('', clean).strip()
    clean = _LEADING_DECOR_RE.sub('', clean).strip()
    return short_tariff_name(clean or title, fallback, max_len=max_len)


def _conversion_nudge(tariff: dict, rub_price: int | None, upgrade: dict | None,
                      *, allow_vip_nudge: bool = True) -> str:
    """Return empty string; 2026 texts integrate hints into descriptions directly."""
    del tariff, rub_price, upgrade, allow_vip_nudge
    return ''


def product_text(tariff: dict, stars: int, *, buyers: int | None = None, promo: str = '',
                 rub: int | None = None, upgrade: dict | None = None,
                 allow_vip_nudge: bool = True, project_name: str = DEFAULT_PROJECT_NAME) -> str:
    del allow_vip_nudge
    details = buyers is None
    title = esc(tariff['title'])
    heading = f'👀 <b>Что внутри «{title}»?</b>' if details else f'🔐 <b>{title}</b>'
    description_key = 'preview_description' if details else 'description'
    description = (tariff.get(description_key) or '').strip().replace('{project_name}', project_name)
    blocks = [heading]
    if description:
        blocks.append(f'<blockquote>{esc(description)}</blockquote>')

    term = duration(tariff.get('duration_days', 0))
    rub_price = tariff.get('rub') if rub is None else rub
    base_rub = tariff.get('rub')
    base_stars = tariff.get('stars', stars)

    if upgrade:
        applied_credit = upgrade.get('saved_rub', upgrade.get('credit_rub', 0))
        price = (
            f'🕘 Доступ: <b>{term}</b>\n'
            f'💳 Обычная цена: <s>{base_stars} Stars (~{fmt_rub(base_rub)} ₽)</s>\n'
            f"⚡ <b>Цена апгрейда: {fmt_rub(upgrade['rub'])} ₽</b> <i>(или {upgrade['stars']} ⭐)</i>\n"
            f'💡 Твоя оплата за предыдущий раздел зачтена (скидка {fmt_rub(applied_credit)} ₽)'
        )
    else:
        if promo and rub_price:
            if base_rub and base_rub > rub_price:
                price = (
                    f'🕘 Доступ: <b>{term}</b>\n'
                    f'💳 Стоимость: <b>{fmt_rub(rub_price)} ₽</b> <i>(или {stars} ⭐)</i> <s>{fmt_rub(base_rub)} ₽</s>\n'
                    f'🏷 Промокод <b>{esc(promo)}</b>'
                )
            else:
                price = (
                    f'🕘 Доступ: <b>{term}</b>\n'
                    f'💳 Стоимость: <b>{fmt_rub(rub_price)} ₽</b> <i>(или {stars} ⭐)</i>\n'
                    f'🏷 Промокод <b>{esc(promo)}</b>'
                )
        elif rub_price:
            price = (
                f'🕘 Доступ: <b>{term}</b>\n'
                f'💳 Стоимость: <b>{fmt_rub(rub_price)} ₽</b> <i>(или {stars} ⭐)</i>'
            )
        else:
            price = (
                f'🕘 Доступ: <b>{term}</b>\n'
                f'💳 Стоимость: <b>{stars} ⭐</b>'
            )
            if promo:
                price += f'\n🏷 Промокод <b>{esc(promo)}</b>'
    blocks.append(price)

    if buyers is not None:
        count = f'{buyers:,}'.replace(',', ' ')
        blocks.append(f'👥 Внутри уже: <b>{count} чел.</b>')

    if details:
        blocks.append('⚡ Ссылка в закрытый канал приходит сразу после оплаты.')
    else:
        blocks.append('⚡ Ссылка в чат за 5 секунд · Разовая оплата без подписок')

    return '\n\n'.join(blocks)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows if row])


def cb(title: str, data: str, *, style: ButtonStyle | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=title, callback_data=data, style=style)


def link(title: str, url: str, *, style: ButtonStyle | None = None) -> InlineKeyboardButton:
    parsed = urlparse(url)
    if parsed.scheme not in ('https', 'http') or not parsed.netloc:
        raise ValueError('Нужна полная ссылка (https://...)')
    return InlineKeyboardButton(text=title, url=url, style=style)


def is_https_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        _ = parsed.port
        return parsed.scheme == 'https' and bool(parsed.hostname) and not any(c.isspace() for c in value)
    except ValueError:
        return False


def menu(project_name: str = DEFAULT_PROJECT_NAME, *, show_reviews: bool = True,
         show_help: bool = True) -> ReplyKeyboardMarkup:
    del project_name
    second_row = []
    if show_reviews:
        second_row.append(KeyboardButton(text=MENU_REVIEWS))
    if show_help:
        second_row.append(KeyboardButton(text=MENU_HELP))
    rows = [
        [KeyboardButton(text=MENU_CATALOG, style='success'), KeyboardButton(text=MENU_ACCESS)],
    ]
    if second_row:
        rows.append(second_row)
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


class _PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == 'br':
            self.parts.append('\n')


def plain_html(text: str) -> str:
    parser = _PlainHTML()
    parser.feed(text)
    parser.close()
    return ''.join(parser.parts)


class _SectionHTML(HTMLParser):
    tags = {'b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del',
            'span', 'tg-spoiler', 'a', 'tg-emoji', 'tg-time', 'code', 'pre', 'blockquote'}

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.tags:
            raise ValueError(f'Тег <{tag}> не поддерживается Telegram. Для переноса строки нажмите Enter.')
        attributes = dict(attrs)
        if len(attributes) != len(attrs):
            raise ValueError(f'В теге <{tag}> повторяются атрибуты.')
        allowed = {'a': {'href'}, 'span': {'class'}, 'code': {'class'},
                   'blockquote': {'expandable'}, 'tg-emoji': {'emoji-id'}, 'tg-time': {'unix', 'format'}}
        if attributes.keys() - allowed.get(tag, set()):
            raise ValueError(f'Неподдерживаемые атрибуты в теге <{tag}>.')
        if tag == 'span' and attributes.get('class') != 'tg-spoiler':
            raise ValueError('Для <span> допустим только class="tg-spoiler".')
        if tag == 'a':
            href = attributes.get('href') or ''
            try:
                parsed = urlparse(href)
                valid = (parsed.scheme in ('http', 'https', 'tg', 'mailto', 'tel')
                         and bool(parsed.netloc or parsed.path) and not any(c.isspace() for c in href))
            except ValueError:
                valid = False
            if not valid:
                raise ValueError('В теге <a> укажите корректный href со схемой https://, tg://, mailto: или tel:.')
        if tag == 'code' and attributes and (
            not self.stack or self.stack[-1] != 'pre'
            or not (attributes.get('class') or '').startswith('language-')
        ):
            raise ValueError('Язык кода задаётся только как <pre><code class="language-python">…</code></pre>.')
        if tag == 'tg-emoji' and not (attributes.get('emoji-id') or '').isdigit():
            raise ValueError('В <tg-emoji> требуется числовой emoji-id.')
        if tag == 'tg-time' and not re.fullmatch(r'-?[0-9]+', attributes.get('unix') or ''):
            raise ValueError('В <tg-time> требуется числовое время unix.')
        if tag == 'tg-time' and 'format' in attributes and not re.fullmatch(
            r'r|w?[dD]?[tT]?', attributes['format'] or ''
        ):
            raise ValueError('Неверный format в <tg-time>. Допустим r либо сочетание w, d/D и t/T.')
        if tag in ('a', 'blockquote') and tag in self.stack:
            raise ValueError(f'Тег <{tag}> нельзя вкладывать в такой же тег.')
        if 'code' in self.stack or ('pre' in self.stack and tag != 'code'):
            raise ValueError('Внутри <pre> и <code> нельзя использовать другие теги форматирования.')
        if tag in ('pre', 'code') and self.stack and self.stack != ['pre']:
            raise ValueError('Блоки <pre> и <code> не должны находиться внутри другого форматирования.')
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            raise ValueError(f'Нарушен порядок закрытия тега </{tag}>.')
        self.stack.pop()

    def handle_startendtag(self, tag, attrs):
        raise ValueError(f'Тег <{tag}/> не поддерживается. Используйте парные теги или обычный перенос строки.')

    def handle_data(self, data):
        if any(char in data for char in '<>&'):
            raise ValueError('Замените символы <, > и & в обычном тексте на &lt;, &gt; и &amp;.')

    def handle_entityref(self, name):
        if name not in {'lt', 'gt', 'amp', 'quot'}:
            raise ValueError(f'Сущность &{name}; не поддерживается. Используйте числовой код символа.')

    def handle_charref(self, name):
        try:
            value = int(name[1:], 16) if name.lower().startswith('x') else int(name)
            if not 0 < value <= 0x10FFFF or 0xD800 <= value <= 0xDFFF:
                raise ValueError
        except ValueError:
            raise ValueError(f'Неверный числовой код символа &#{name};.') from None

    def handle_comment(self, data):
        raise ValueError('HTML-комментарии в сообщениях не поддерживаются.')

    def handle_decl(self, decl):
        raise ValueError('HTML-декларации в сообщениях не поддерживаются.')

    def unknown_decl(self, data):
        raise ValueError('CDATA и нестандартные HTML-декларации в сообщениях не поддерживаются.')

    def handle_pi(self, data):
        raise ValueError('HTML-инструкции в сообщениях не поддерживаются.')


def validate_section_html(text: str) -> None:
    """Validate ordinary Telegram HTML before saving an editable section."""
    if re.search(r'&(?!(?:lt|gt|amp|quot|#[0-9]+|#[xX][0-9a-fA-F]+);)', text):
        raise ValueError('Используйте &amp; вместо & и завершайте HTML-сущности точкой с запятой.')
    parser = _SectionHTML()
    parser.feed(text)
    parser.close()
    if parser.stack:
        raise ValueError(f'Закройте тег <{parser.stack[-1]}>.')
    if not plain_html(text).strip():
        raise ValueError('Сообщение должно содержать видимый текст.')


def text_chunks(text: str, limit: int = 4000) -> list[str]:
    """Keep every character, staying within Telegram's UTF-16 length budget."""
    chunks, current, units = [], [], 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > limit and current:
            chunks.append(''.join(current))
            current, units = [], 0
        current.append(char)
        units += size
    if current:
        chunks.append(''.join(current))
    return chunks


async def send_text(sender, content: str, markup=None, **kwargs):
    """Send normal HTML, or the complete plain text in safe-sized messages."""
    plain = plain_html(content)
    if len(plain.encode('utf-16-le')) // 2 <= 4000:
        try:
            return await sender(content, reply_markup=markup, **kwargs)
        except TelegramBadRequest:
            pass
    chunks = text_chunks(plain or content)
    result = None
    for index, chunk in enumerate(chunks):
        result = await sender(chunk, parse_mode=None,
                              reply_markup=markup if index == len(chunks) - 1 else None, **kwargs)
    return result


async def send_section(message: Message, row: dict | None, markup=None, fallback: str = ''):
    content = (row or {}).get('value') or fallback or 'Раздел временно не заполнен.'
    media_id = (row or {}).get('media_id', '')
    media_type = (row or {}).get('media_type', '')
    # Telegram caption supports <= 1024 chars; send HTML text separately for long sections.
    if media_id and media_type in MEDIA_TYPES:
        caption = content if len(content.encode('utf-16-le')) // 2 <= 950 else None
        try:
            if media_type == 'photo':
                await message.answer_photo(photo=media_id, caption=caption,
                                           reply_markup=markup if caption is not None else None)
            elif media_type == 'video':
                await message.answer_video(video=media_id, caption=caption,
                                           reply_markup=markup if caption is not None else None)
            else:
                await message.answer_animation(animation=media_id, caption=caption,
                                               reply_markup=markup if caption is not None else None)
        except TelegramBadRequest:
            # Invalid media or caption. The text remains available.
            pass
        else:
            if caption is not None:
                return
    return await send_text(message.answer, content, markup=markup)


async def send_rich_report(message: Message, heading: str, data: list[tuple[str, int]],
                           *, note: str = 'Данные взяты из базы заказов и пользователей.'):
    """Use real Telegram 10.1+ Rich Message tables; fall back for standard clients."""
    from aiogram.types import InputRichMessage
    from aiogram.exceptions import TelegramAPIError
    lines = [f'# {heading}', '', '| Показатель | Значение |', '| :-- | --: |']
    lines += [f'| {name} | {value} |' for name, value in data]
    lines += ['', f'> {note}']
    try:
        await message.bot.send_rich_message(chat_id=message.chat.id,
                                            rich_message=InputRichMessage(markdown='\n'.join(lines)))
    except (TelegramAPIError, AttributeError):
        fallback = f'📊 <b>{esc(heading)}</b>\n\n' + '\n'.join(f'• {esc(k)}: <b>{v}</b>' for k, v in data)
        fallback += f'\n\n<blockquote>{esc(note)}</blockquote>'
        await send_text(message.answer, fallback)
