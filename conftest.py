"""Keep tests away from the real TV, private credentials and download state."""
import os
from pathlib import Path
import tempfile

from configuration import create_config, write_config

_test_home = tempfile.TemporaryDirectory(prefix='home-tv-console-test-')
os.environ['TV_CONSOLE_HOME'] = _test_home.name
write_config(create_config('192.0.2.10', '192.0.2.20', 'test-only-password',
                            library=Path(_test_home.name) / 'library'), _test_home.name)
