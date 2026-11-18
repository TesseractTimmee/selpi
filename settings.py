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
