from dotenv import load_dotenv
from pathlib import Path
import os

# Preference order will be:
#  1) existing environment variables
#  2) .env.local
#  3) .env.dist
# .env.local is used instead of .env to avoid interacting with pipenv
# (all relative to selpi's own directory, so commands work from any working directory; .env first)
base_dir = Path(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(dotenv_path=base_dir / '.env')
load_dotenv(dotenv_path=base_dir / '.env.local')
load_dotenv(dotenv_path=base_dir / '.env.dist')

def getb(key: bytes) -> bytes:
    return os.getenvb(b'SELPI_' + key)


def _int_env(name: str, default: int) -> int:
    """A non-negative int from the environment, or the default when unset/blank/invalid."""
    v = os.environ.get(name)
    if v is None or v == '':
        return default
    try:
        return max(0, int(v))
    except ValueError:
        return default


def _is_select_live() -> bool:
    return os.environ.get('SELPI_CONNECTION_TYPE') == 'SelectLive'


def cache_ttl() -> int:
    """Seconds a configuration/settings page may be served from its last read before re-reading the link.
    0 = always read fresh (a page error still falls back to the last read). The caches exist to ride out
    Select.Live's stalls, so the default is long on Select.Live and 0 on a direct/local link; override with
    SELPI_CACHE_TTL for a TCP link that is actually remote/slow."""
    return _int_env('SELPI_CACHE_TTL', 1800 if _is_select_live() else 0)


def stale_grace() -> int:
    """Seconds a last-good live value is served after the link fails, before a poll returns nothing (so the
    integration can mark entities unavailable). 0 = never serve stale — entities go 'unknown' as soon as the
    link drops (useful on a reliable local link where a dropout is meaningful). Default 600 on Select.Live
    (ride out stalls), 0 on a direct/local link; override with SELPI_STALE_GRACE."""
    return _int_env('SELPI_STALE_GRACE', 600 if _is_select_live() else 0)
