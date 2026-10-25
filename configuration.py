"""Private runtime configuration, separate from the source checkout."""
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sys


DEFAULT_APPS = {
    'home': ['电视主页', '', ''],
    'kodi': ['本地影院 · Kodi', 'org.xbmc.kodi', '.Splash'],
    'settings': ['电视设置', 'com.android.tv.settings', '.MainSettings'],
}


def runtime_root():
    if os.environ.get('TV_CONSOLE_HOME'):
        return Path(os.environ['TV_CONSOLE_HOME']).expanduser().resolve()
    base = (Path.home() / 'Library/Application Support' if sys.platform == 'darwin' else
            Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')))
    return base / 'HomeTVConsole'


def validate(config):
    for field in ['tv_host', 'mac_host']:
        address = ipaddress.IPv4Address(config[field])
        if address.is_unspecified or address.is_multicast:
            raise ValueError(f'{field} 必须是设备的局域网 IPv4 地址')
    for field in ['port', 'tv_adb_port', 'adb_server_port', 'kodi_port']:
        if not isinstance(config[field], int) or not 1 <= config[field] <= 65535:
            raise ValueError(f'{field} 必须是有效端口')
    if len(config.get('secret', '')) < 32:
        raise ValueError('请重新运行 configure.py 生成控制台密钥')
    if not config.get('kodi_password'):
        raise ValueError('请设置 Kodi Web 服务密码')
    for key, app in config['apps'].items():
        if not re.fullmatch('[a-z][a-z0-9_-]*', key) or not isinstance(app, list) or len(app) != 3:
            raise ValueError('应用配置需要 id 和 [名称, 包名, Activity]')
        label, package, activity = app
        if not isinstance(label, str) or (key != 'home' and not re.fullmatch(r'[\w.]+', package)):
            raise ValueError('应用包名无效')
        if activity and not re.fullmatch(r'[\w.$]+', activity):
            raise ValueError('应用 Activity 无效')
    if 'home' not in config['apps'] or 'kodi' not in config['apps']:
        raise ValueError('应用配置需要保留 home 和 kodi')
    return config


def create_config(tv_host, mac_host, kodi_password, *, library=None, **overrides):
    config = {
        'tv_host': tv_host, 'mac_host': mac_host, 'tv_adb_port': 5555,
        'port': 17880, 'adb_server_port': 5037, 'kodi_port': 8080,
        'kodi_user': 'tvconsole', 'kodi_password': kodi_password,
        'secret': secrets.token_urlsafe(48), 'adb': shutil.which('adb') or 'adb', 'adb_key': '',
        'node': shutil.which('node') or 'node', 'subtitle_proxy': '',
        'library': str(Path(library or Path.home() / 'Movies/TVLibrary').expanduser().resolve()),
        'tv_name': '客厅电视', 'apps': {key: list(value) for key, value in DEFAULT_APPS.items()},
        'idle_packages': ['com.gathin.tvhome', 'com.google.android.tvlauncher',
                          'com.google.android.apps.tv.launcherx'],
    }
    config.update(overrides)
    return validate(config)


def write_config(config, root=None):
    validate(config)
    root = Path(root or runtime_root())
    data = root / 'data'
    data.mkdir(parents=True, mode=0o700, exist_ok=True)
    data.chmod(0o700)
    target = data / 'config.json'
    if target.exists():
        raise FileExistsError(f'配置已存在，请直接编辑：{target}')
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
    return target


def load_config():
    root = runtime_root()
    target = root / 'data/config.json'
    if not target.exists():
        raise RuntimeError('尚未配置，请先运行 python configure.py')
    config = validate(json.loads(target.read_text()))
    for name in ['Local', 'Downloads']:
        (Path(config['library']) / name).mkdir(parents=True, exist_ok=True)
    return root, config
