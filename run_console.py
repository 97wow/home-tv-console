import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parent
os.chdir(root)
os.execv(sys.executable, [sys.executable, str(root / 'server.py')])
