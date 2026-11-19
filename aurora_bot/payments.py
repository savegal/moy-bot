"""Documented payment adapters. Never use button clicks as proof of payment."""
from __future__ import annotations

import base64
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary, WeakValueDictionary

import aiohttp

from .config import Config
from .db import in_seconds, now
from .service import EXTERNAL_INVOICE_TTL_SECONDS, Shop, ShopError

log = logging.getLogger(__name__)


class GatewayError(Exception):
    pass


class GatewayUnavailable(GatewayError):
    """The provider may have received the request even though its reply failed."""


_HTTP_SESSIONS: WeakKeyDictionary[asyncio.AbstractEventLoop, aiohttp.ClientSession] = WeakKeyDictionary()


@asynccontextmanager
async def _http_session(timeout: aiohttp.ClientTimeout):
    if not getattr(aiohttp.ClientSession, '__module__', '').startswith('aiohttp'):
        async with aiohttp.ClientSession(timeout=timeout) as mocked_session:
            yield mocked_session
        return
    loop = asyncio.get_running_loop()
    session = _HTTP_SESSIONS.get(loop)
    if session is None or session.closed:
        session = aiohttp.ClientSession(timeout=timeout)
        _HTTP_SESSIONS[loop] = session
    yield session


async def close_http_sessions() -> None:
    for loop, session in list(_HTTP_SESSIONS.items()):
        _HTTP_SESSIONS.pop(loop, None)
        if not session.closed and not loop.is_closed():
            await session.close()


async def _json_post(url: str, payload: dict, *, headers: dict | None = None,
                     raw: bytes | None = None) -> dict:
    timeout = aiohttp.ClientTimeout(total=20)
    async with _http_session(timeout) as session:
        async with session.post(url, data=raw if raw is not None else None if payload is None else json.dumps(payload, ensure_ascii=False),
                                headers={'Content-Type':'application/json', **(headers or {})},
                                allow_redirects=False) as response:
            if response.status != 200:
                error = GatewayUnavailable if response.status in (408, 429) or response.status >= 500 else GatewayError
                raise error(f'Платёжный сервис вернул HTTP {response.status}')
            try:
                data = await response.json(content_type=None)
            except (ValueError, UnicodeError) as exc:
                raise GatewayUnavailable('Платёжный сервис вернул некорректный JSON') from exc
            if not isinstance(data, dict):
                raise GatewayUnavailable('Неправильный формат ответа платёжного сервиса')
            return data


def rub_match(amount: Any, rub: int) -> bool:
    try:
        return Decimal(str(amount)) == Decimal(rub)
    except (InvalidOperation, ValueError, TypeError):
        return False


def invoice_identifier(external_id: Any) -> str:
    if isinstance(external_id, bool) or not isinstance(external_id, (str, int)) or not str(external_id).strip():
        raise GatewayError('Платёжный сервис не вернул номер счёта')
    return str(external_id)


def invoice_destination(external_id: Any, url: Any) -> tuple[str, str]:
    """Reject missing identifiers and unusable checkout URLs before saving an invoice."""
    identifier = invoice_identifier(external_id)
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
        valid = (parsed and parsed.scheme == 'https' and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and (parsed.port is None or parsed.port > 0)
                 and not any(c.isspace() for c in url))
    except ValueError:
        valid = False
    if not valid:
        raise GatewayError('Платёжный сервис не вернул безопасную ссылку на счёт')
    return identifier, url


def _parse_provider_expiry(value: Any) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value <= 0:
            return None
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec='seconds')
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith(('Z', 'z')):
        raw = raw[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec='seconds')


def _extract_provider_invoice_expiry(entry: dict, *, fallback_ttl: int = EXTERNAL_INVOICE_TTL_SECONDS) -> str | None:
    if not isinstance(entry, dict):
        return None
    direct = _parse_provider_expiry(
        entry.get('expiration_date') or entry.get('expired_at') or entry.get('expires_at')
    )
    if direct:
        return direct
    created = _parse_provider_expiry(entry.get('created_at'))
    if not created:
        return None
    try:
        return (datetime.fromisoformat(created) + timedelta(seconds=fallback_ttl)).isoformat(timespec='seconds')
    except ValueError:
        return None


_GATEWAY_TERMINAL_UNPAID = frozenset({'expired', 'cancel', 'canceled', 'cancelled', 'failed', 'rejected', 'error'})


def _record_created_expiry(cache: dict[str, str], order_id: str, raw_expiry: Any) -> None:
    if len(cache) > 512:
        cache.clear()
    cache[order_id] = _parse_provider_expiry(raw_expiry) or in_seconds(EXTERNAL_INVOICE_TTL_SECONDS)


class CryptoPay:
    def __init__(self, config: Config):
        self.token = config.cryptopay_token
        self.testnet = config.cryptopay_testnet
        self.endpoint = 'https://testnet-pay.crypt.bot/api' if self.testnet else 'https://pay.crypt.bot/api'
        self._created_expires_at: dict[str, str] = {}

    async def check_connection(self):
        if not isinstance(await asyncio.wait_for(self.api('getMe', {}), timeout=8), dict):
            raise GatewayError('Некорректный ответ Crypto Pay')

    async def api(self, method: str, data: dict) -> dict:
        if not self.token:
            raise GatewayError('Crypto Pay не настроен')
        response = await _json_post(f'{self.endpoint}/{method}', data,
                                    headers={'Crypto-Pay-API-Token': self.token})
        if response.get('ok') is not True or not isinstance(response.get('result'), (dict, list)):
            raise GatewayError('Crypto Pay отклонил запрос')
        return response['result']

    async def create(self, order: dict) -> tuple[str, str]:
        data = await self.api('createInvoice', {
            'currency_type': 'fiat', 'fiat': 'RUB', 'amount': str(order['amount_rub']),
            'payload': order['id'], 'description': order['title'][:100],
            'expires_in': EXTERNAL_INVOICE_TTL_SECONDS,
        })
        if not isinstance(data, dict) or not self.matches(order, data):
            raise GatewayUnavailable('Crypto Pay вернул данные другого счёта или суммы')
        try:
            external_id, url = self.destination(data)
        except GatewayError as exc:
            raise GatewayUnavailable(str(exc)) from exc
        expiry = now() if data.get('status') == 'expired' else data.get('expiration_date')
        _record_created_expiry(self._created_expires_at, order['id'], expiry)
        return external_id, url

    @staticmethod
    def destination(data: dict) -> tuple[str, str]:
        url = data.get('bot_invoice_url') or data.get('mini_app_invoice_url') or data.get('web_app_invoice_url')
        return invoice_destination(data.get('invoice_id'), url)

    def verify_webhook(self, body: bytes, signature: str) -> bool:
        if not self.token or not re.fullmatch(r'[0-9a-fA-F]{64}', signature):
            return False
        key = hashlib.sha256(self.token.encode('utf8')).digest()
        expected = hmac.new(key, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature.lower(), expected)

    @staticmethod
    def matches(order: dict, entry: dict) -> bool:
        return (entry.get('payload') == order['id'] and entry.get('currency_type') == 'fiat'
                and entry.get('fiat') == 'RUB' and rub_match(entry.get('amount'), order['amount_rub']))

    async def status(self, order: dict) -> bool:
        return bool(await self.paid_invoice(order))

    async def fetch_invoice(self, order: dict) -> dict | None:
        """Return matching provider invoice regardless of status (active, paid, expired)."""
        items = await self.invoices({'invoice_ids': order['external_id']})
        for entry in items:
            if (isinstance(entry, dict) and str(entry.get('invoice_id')) == order['external_id']
                    and self.matches(order, entry)):
                return entry
        return None

    async def paid_invoice(self, order: dict) -> dict | None:
        """Return verified payment evidence, including the provider's payment date."""
        entry = await self.fetch_invoice(order)
        return entry if (entry and entry.get('status') == 'paid') else None

    async def invoices(self, filters: dict) -> list:
        result = await self.api('getInvoices', filters)
        items = result.get('items', []) if isinstance(result, dict) else result
        if not isinstance(items, list):
            raise GatewayError('Crypto Pay вернул некорректный список счетов')
        return items


class Gateway2328:
    URL = 'https://api.2328.io/api'

    def __init__(self, config: Config):
        self.project = config.project_2328
        self.secret = config.key_2328
        self.callback = config.public_base_url + '/webhooks/2328'
        self.custom_url = config.gateway_2328_url.rstrip('/')
        self._created_expires_at: dict[str, str] = {}

    @staticmethod
    def signature(payload: dict, secret: str) -> str:
        packed = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf8')
        encoded = base64.b64encode(packed)
        return hmac.new(secret.encode('utf8'), encoded, hashlib.sha256).hexdigest()

    def verify_webhook(self, payload: dict) -> bool:
        supplied = payload.get('sign')
        if not isinstance(supplied, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', supplied) or not self.secret:
            return False
        body = {k:v for k,v in payload.items() if k != 'sign'}
        expected = self.signature(body, self.secret)
        return hmac.compare_digest(supplied.lower(), expected)

    async def api(self, method: str, payload: dict) -> dict:
        if not self.project or not self.secret:
            raise GatewayError('2328 не настроен')
        packed = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf8')
        base_url = self.custom_url or self.URL.rstrip('/')
        data = await _json_post(f'{base_url}/{method}', payload, raw=packed, headers={
            'project': self.project,
            'sign': self.signature(payload, self.secret),
            'User-Agent': 'AuroraAccessBot/1.0',
        })
        if type(data.get('state')) is not int or data['state'] != 0 or not isinstance(data.get('result'), dict):
            raise GatewayError('2328 отклонил запрос')
        return data['result']

    async def create(self, order: dict) -> tuple[str, str]:
        data = await self.api('v1/payment', {
            'amount': f"{order['amount_rub']}.00", 'currency': 'RUB',
            'order_id': order['id'], 'description': order['title'][:100],
            'url_callback': self.callback, 'ttl_seconds': EXTERNAL_INVOICE_TTL_SECONDS,
        })
        if data.get('order_id') != order['id'] or not rub_match(data.get('amount'), order['amount_rub']) or data.get('currency') != 'RUB':
            raise GatewayError('Несоответствие данных выставленного счёта')
        external_id, url = invoice_destination(data.get('uuid'), data.get('url'))
        raw_expiry = (now() if data.get('payment_status') in _GATEWAY_TERMINAL_UNPAID else
                      data.get('expiration_date') or data.get('expired_at') or data.get('expires_at'))
        _record_created_expiry(self._created_expires_at, order['id'], raw_expiry)
        return external_id, url

    async def info(self, order: dict):
        data = await self.api('v1/payment/info', {'order_id':order['id']})
        if (data.get('order_id') != order['id'] or
            not rub_match(data.get('amount'), order['amount_rub']) or
            data.get('currency') != 'RUB'):
            raise GatewayError('Ответ другого заказа, валюты или суммы')
        if order['external_id'] and str(data.get('uuid')) != order['external_id']:
            raise GatewayError('Неверный внешний ID')
        return data

    def valid_payment_webhook(self, order: dict, data: dict) -> bool:
        try:
            identifier = invoice_identifier(data.get('uuid'))
        except GatewayError:
            return False
        return (self.verify_webhook(data) and order.get('provider') == '2328' and
                order.get('external_id') in ('', identifier) and
                data.get('order_id') == order.get('id') and
                data.get('currency') == 'RUB' and
                rub_match(data.get('amount'), order.get('amount_rub', -1)) and
                data.get('payment_status') in ('paid', 'overpaid', 'refund_process', 'refund_fail', 'refund_paid'))


class Payments:
    def __init__(self, config: Config, shop: Shop):
        self.config, self.shop = config, shop
        self.gateway = Gateway2328(config)
        self.crypto_settings_path = Path(f'{shop.db.path}.cryptopay.json')
        self.crypto_config = self.load_crypto_config()
        self.crypto = CryptoPay(self.crypto_config)
        self.crypto_config_lock = asyncio.Lock()
        self.checkout_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
        self.creation_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self.gateway_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    @property
    def crypto_enabled(self) -> bool:
        return self.crypto_config.crypto_enabled

    def load_crypto_config(self) -> Config:
        try:
            raw = json.loads(self.crypto_settings_path.read_text(encoding='utf-8'))
            if (not isinstance(raw, dict) or not isinstance(raw.get('token'), str)
                    or not isinstance(raw.get('testnet'), bool) or not isinstance(raw.get('enabled'), bool)):
                raise ValueError('Invalid Crypto Pay settings')
        except FileNotFoundError:
            return self.config
        except (ValueError, OSError):
            log.error('Cannot load local Crypto Pay settings')
            return replace(self.config, allow_external_methods=False, cryptopay_token='')
        return replace(self.config, cryptopay_token=raw['token'], cryptopay_testnet=raw['testnet'],
                       allow_external_methods=raw['enabled'])

    async def configure_crypto(self, *, token: str | None = None, testnet: bool | None = None,
                               enabled: bool | None = None):
        async with self.crypto_config_lock:
            current = self.crypto_config
            selected_testnet = current.cryptopay_testnet if testnet is None else testnet
            keep_enabled = current.crypto_enabled and selected_testnet == current.cryptopay_testnet
            candidate = replace(
                current,
                cryptopay_token=current.cryptopay_token if token is None else token,
                cryptopay_testnet=selected_testnet,
                allow_external_methods=keep_enabled if enabled is None else enabled,
            )
            if not re.fullmatch(r'[!-~]{1,256}', candidate.cryptopay_token):
                raise GatewayError('Некорректный API-токен Crypto Pay')
            crypto = CryptoPay(candidate)
            if token is not None or enabled:
                await crypto.check_connection()

            settings = {'token': crypto.token, 'testnet': crypto.testnet,
                        'enabled': candidate.allow_external_methods}
            pending = NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.crypto_settings_path.parent,
                                         prefix='.cryptopay-', suffix='.cryptopay.json', delete=False)
            temp_path = Path(pending.name)
            try:
                with pending:
                    json.dump(settings, pending)
                os.replace(temp_path, self.crypto_settings_path)
            finally:
                temp_path.unlink(missing_ok=True)
            self.crypto_config, self.crypto = candidate, crypto

    async def close(self) -> None:
        await close_http_sessions()

    async def create_external(self, order: dict) -> tuple[str,str]:
        lock = self.creation_locks.get(order['id'])
        if lock is None:
            lock = asyncio.Lock()
            self.creation_locks[order['id']] = lock
        async with lock:
            current = await self.shop.order(order['id'])
            if (not current or current['status'] != 'pending' or current['provider'] != order['provider']
                    or current['expires_at'] <= now() or current['refund_requested_at']):
                raise ShopError('Заказ уже закрыт или изменился')
            if current['external_id']:
                return invoice_destination(current['external_id'], current['payment_url'])
            if current['provider'] == 'cryptopay' and self.crypto_enabled:
                provider_client = self.crypto
            elif current['provider'] == '2328' and self.config.gateway_enabled:
                provider_client = self.gateway
            else:
                raise GatewayError('Этот способ оплаты не активирован')
            marked = await self.shop.begin_external_invoice(current['id'], current['provider'])
            if not marked:
                latest = await self.shop.order(current['id'])
                if (latest and latest['status'] == 'pending' and latest['external_id']
                        and latest['expires_at'] > now() and not latest['refund_requested_at']):
                    return invoice_destination(latest['external_id'], latest['payment_url'])
                raise ShopError('Заказ уже закрыт или изменился')
            try:
                external_id, url = await provider_client.create(current)
            except (GatewayUnavailable, aiohttp.ClientError, TimeoutError, asyncio.CancelledError):
                if current['provider'] == 'cryptopay':
                    # Crypto Pay has no creation idempotency key. Leave this attempt
                    # discoverable for reconciliation without calling create again for it.
                    await self._expire_failed_creation(current['id'], keep_attempt=True)
                raise
            except GatewayError:
                await self._expire_failed_creation(
                    current['id'], keep_attempt=bool(current['invoice_sending_at']),
                )
                raise
            provider_expires_at = provider_client._created_expires_at.pop(current['id'], None)
            open_checkout = await self.shop.set_external(
                current['id'], current['provider'], external_id, url,
                expires_at=provider_expires_at,
            )
            if not open_checkout:
                raise ShopError('Заказ уже закрыт или изменился')
            return external_id, url

    async def _expire_failed_creation(self, order_id: str, *, keep_attempt: bool):
        await self.shop.db.execute("""UPDATE orders SET status='expired',expires_at=?,
            invoice_sending_at=CASE WHEN ? THEN invoice_sending_at ELSE NULL END
            WHERE id=? AND status='pending' AND external_id='' AND payment_url=''""",
            (now(), keep_attempt, order_id))

    async def reconcile(self, order: dict, *, refund_only: bool = False,
                        expected_status: str | None = None,
                        webhook_invoice_id: str | None = None,
                        crypto: CryptoPay | None = None) -> bool:
        if order['provider'] == '2328' and self.config.gateway_configured:
            lock = self.gateway_locks.setdefault(order['id'], asyncio.Lock())
            async with lock:
                current = await self.shop.order(order['id'])
                if current and current['status'] in ('pending', 'expired', 'paid'):
                    return await self.reconcile_gateway(current, refund_only, expected_status, webhook_invoice_id)
            return False
        crypto = crypto or self.crypto
        if order['provider'] != 'cryptopay' or refund_only or not crypto.token:
            return False
        current = await self.shop.order(order['id'])
        if not current or current['status'] not in ('pending', 'expired', 'paid'):
            return False

        external_id = current['external_id']
        if webhook_invoice_id is not None and external_id not in ('', webhook_invoice_id):
            raise GatewayError('Вебхук относится к другому счёту')
        lookup_id = external_id or webhook_invoice_id
        if not lookup_id:
            return False
        lookup_order = {**current, 'external_id': lookup_id}
        use_fetch = (
            hasattr(crypto, 'fetch_invoice')
            and 'paid_invoice' not in getattr(crypto, '__dict__', {})
            and getattr(type(crypto), 'paid_invoice', None) is CryptoPay.paid_invoice
        )
        if use_fetch:
            invoice = await crypto.fetch_invoice(lookup_order)
            if not invoice:
                return False
            return await self.sync_crypto_invoice(current, invoice, lookup_id)
        invoice = await crypto.paid_invoice(lookup_order)
        return bool(invoice and await self.settle_crypto_invoice(current, invoice, lookup_id))

    async def sync_crypto_invoice(self, order: dict, invoice: dict, invoice_id: str) -> bool:
        """Apply verified Crypto Pay invoice state."""
        status = invoice.get('status')
        if status == 'paid':
            return await self.settle_crypto_invoice(order, invoice, invoice_id)
        if status == 'expired':
            await self.shop.expire_external_invoice(order['id'], 'cryptopay', invoice_id)
        elif status == 'active' and not order['external_id']:
            external_id, url = CryptoPay.destination(invoice)
            if external_id != invoice_id:
                raise GatewayError('Ответ Crypto Pay относится к другому счёту')
            await self.shop.set_external(
                order['id'], 'cryptopay', external_id, url,
                expires_at=_extract_provider_invoice_expiry(invoice),
            )
        return False

    async def settle_crypto_invoice(self, order: dict, invoice: dict, invoice_id: str) -> bool:
        if not order['external_id']:
            external_id, url = CryptoPay.destination(invoice)
            if external_id != invoice_id:
                raise GatewayError('Ответ Crypto Pay относится к другому счёту')
            await self.shop.set_external(order['id'], 'cryptopay', external_id, url,
                                         expires_at=_extract_provider_invoice_expiry(invoice))
        paid_at = None
        if invoice.get('paid_at'):
            try:
                paid_at = datetime.fromisoformat(invoice['paid_at'])
                if paid_at.tzinfo is None:
                    paid_at = paid_at.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                log.warning('Crypto Pay payment date unavailable for order %s', order['id'])
        await self.shop.settle(order['id'], 'cryptopay', external_id=invoice_id, paid_at=paid_at)
        current = await self.shop.order(order['id'])
        return current['status'] == 'paid' and current['external_id'] == invoice_id

    async def reconcile_gateway(self, order: dict, refund_only: bool, expected_status: str | None,
                                webhook_invoice_id: str | None) -> bool:
        order_id = order['id']
        external_id = order['external_id']
        if webhook_invoice_id is not None and external_id not in ('', webhook_invoice_id):
            raise GatewayError('Вебхук относится к другому счёту')
        if external_id and expected_status in ('refund_process', 'refund_paid'):
            await self.shop.pause_gateway_delivery(order_id, external_id)

        data = await self.gateway.info(order)
        status = data.get('payment_status')
        terminal_unpaid = status in _GATEWAY_TERMINAL_UNPAID
        if not external_id:
            external_id, url = invoice_destination(data.get('uuid'), data.get('url'))
            if webhook_invoice_id is not None and webhook_invoice_id != external_id:
                raise GatewayError('Ответ 2328 относится к другому счёту')
            await self.shop.set_external(
                order_id,
                '2328',
                external_id,
                url,
                expires_at=now() if terminal_unpaid else _extract_provider_invoice_expiry(data),
            )
            if expected_status in ('refund_process', 'refund_paid'):
                await self.shop.pause_gateway_delivery(order_id, external_id)
        transitions = {
            'refund_process': ('refund_process', 'refund_paid', 'refund_fail'),
            'refund_paid': ('refund_paid',),
            'refund_fail': ('refund_fail', 'refund_paid'),
        }
        if expected_status in transitions and status not in transitions[expected_status]:
            raise GatewayError('Статус возврата ещё не подтверждён API провайдера')
        if status == 'refund_paid':
            await self.shop.refund_gateway(order_id, external_id)
        elif status == 'refund_process':
            await self.shop.pause_gateway_delivery(order_id, external_id)
        elif status == 'refund_fail':
            # A failed refund may concern an underpayment.
            await self.shop.resume_gateway_delivery(order_id, external_id)
        elif not refund_only and status in ('paid', 'overpaid'):
            await self.shop.settle(order_id, '2328', external_id=external_id)
            current = await self.shop.order(order['id'])
            return current['status'] == 'paid' and not current['refund_requested_at']
        elif terminal_unpaid:
            await self.shop.expire_external_invoice(order_id, '2328', external_id)
        return False
