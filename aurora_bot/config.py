"""Environment configuration. Secrets are never embedded into source."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlsplit

from aiogram.utils.token import TokenValidationError, validate_token

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {'1', 'yes', 'true', 'on'}


def _load_dotenv(path: Path = Path('.env')) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        if not line or line.lstrip().startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key, value = key.strip(), value.strip()
        if value[:1] in ('"', "'") and value[-1:] == value[:1]:
            value = value[1:-1]
        else:
            value = value.split(' #', 1)[0].rstrip()
        os.environ.setdefault(key, value)


def _https_base_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return bool(parsed.scheme == 'https' and parsed.hostname and not parsed.username
                    and not parsed.password and not parsed.query and not parsed.fragment
                    and not any(char.isspace() for char in value)
                    and (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        return False


@dataclass(frozen=True)
class Config:
    token: str
    admin_ids: frozenset[int]
    db_path: str = 'data/bot.sqlite3'
    cryptopay_token: str = ''
    cryptopay_testnet: bool = False
    project_2328: str = ''
    key_2328: str = ''
    public_base_url: str = ''
    http_port: int = 8080
    allow_external_methods: bool = False
    poll_seconds: int = 30
    telegram_test_mode: bool = False
    gateway_2328_url: str = ''

    @property
    def crypto_enabled(self) -> bool:
        return self.allow_external_methods and bool(self.cryptopay_token)

    @property
    def gateway_enabled(self) -> bool:
        return self.allow_external_methods and self.gateway_configured and _https_base_url(self.public_base_url)

    @property
    def gateway_configured(self) -> bool:
        return bool(self.project_2328 and self.key_2328)

    @classmethod
    def from_env(cls) -> 'Config':
        _load_dotenv(PROJECT_ROOT / '.env')
        try:
            ids = frozenset(int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip())
        except ValueError:
            raise ValueError('ADMIN_IDS должен содержать числовые Telegram user ID через запятую') from None
        token = os.getenv('BOT_TOKEN', '').strip()
        try:
            validate_token(token)
        except TokenValidationError:
            raise ValueError('Укажи действительный BOT_TOKEN в .env') from None
        if not ids or any(not 0 < user_id < 2 ** 52 for user_id in ids):
            raise ValueError('Укажи ADMIN_IDS в .env (Telegram user ID)')
        try:
            port = int(os.getenv('HTTP_PORT', '8080'))
            poll_seconds = max(20, int(os.getenv('POLL_SECONDS', '30')))
        except ValueError:
            raise ValueError('HTTP_PORT и POLL_SECONDS должны быть целыми числами') from None
        if not 1 <= port <= 65535:
            raise ValueError('HTTP_PORT должен быть от 1 до 65535')
        test_mode = _bool('TELEGRAM_TEST_MODE')
        raw_db_path = os.getenv('DB_PATH', 'data/bot.sqlite3').strip()
        if not raw_db_path:
            raise ValueError('DB_PATH не должен быть пустым')
        db_path = Path(raw_db_path).expanduser()
        if not db_path.is_absolute():
            db_path = PROJECT_ROOT / db_path
        if test_mode and db_path.resolve() == (PROJECT_ROOT / 'data/bot.sqlite3').resolve():
            db_path = PROJECT_ROOT / 'data/bot-test.sqlite3'
        gateway_url = os.getenv('GATEWAY_2328_URL', '').strip().rstrip('/')
        if gateway_url and not _https_base_url(gateway_url):
            raise ValueError('GATEWAY_2328_URL должен быть корректным HTTPS URL')
        return cls(
            token=token, admin_ids=ids,
            db_path=str(db_path.resolve()),
            cryptopay_token=os.getenv('CRYPTOPAY_TOKEN', ''),
            cryptopay_testnet=_bool('CRYPTOPAY_TESTNET'),
            project_2328=os.getenv('GATEWAY_2328_PROJECT', ''),
            key_2328=os.getenv('GATEWAY_2328_KEY', ''),
            public_base_url=os.getenv('PUBLIC_BASE_URL', '').rstrip('/'),
            http_port=port,
            allow_external_methods=_bool('ENABLE_EXTERNAL_PAYMENTS'),
            poll_seconds=poll_seconds,
            telegram_test_mode=test_mode,
            gateway_2328_url=gateway_url,
        )
