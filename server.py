import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit, quote

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from configuration import load_config

SOURCE_ROOT = Path(__file__).resolve().parent
ROOT, CONFIG = load_config()
LIBRARY = Path(CONFIG['library']).resolve()
VIDEO = {'.mp4', '.mkv', '.mov', '.m4v', '.avi', '.webm', '.ts', '.m2ts', '.mpg', '.mpeg'}
STATE_FILE = ROOT / 'data/state.json'
STATE_LOCK = threading.RLock()
ADB_LOCK = threading.RLock()
PLAY_LOCK = threading.RLock()
HTTP = httpx.Client(trust_env=False, timeout=8)
STATE = {'autoplay': False, 'media': {}, 'finished': [], 'queue': [], 'events': []}
if STATE_FILE.exists():
    STATE.update(json.loads(STATE_FILE.read_text()))
CACHE = {'jobs': [], 'download_error': '', 'tv': {}, 'tv_at': 0}
STOP = threading.Event()
APPS = CONFIG['apps']
KEYS = {
    'up': 'DPAD_UP', 'down': 'DPAD_DOWN', 'left': 'DPAD_LEFT', 'right': 'DPAD_RIGHT',
    'ok': 'DPAD_CENTER', 'back': 'BACK', 'home': 'HOME', 'playpause': 'MEDIA_PLAY_PAUSE',
    'stop': 'MEDIA_STOP', 'volup': 'VOLUME_UP', 'voldown': 'VOLUME_DOWN', 'mute': 'VOLUME_MUTE',
    'input': 'TV_INPUT', 'wake': 'WAKEUP',
}


def save_state():
    with STATE_LOCK:
        p = STATE_FILE.with_suffix('.tmp')
        p.write_text(json.dumps(STATE, ensure_ascii=False, indent=2))
        p.chmod(0o600)
        os.replace(p, STATE_FILE)


def event(message):
    with STATE_LOCK:
        STATE['events'].insert(0, {'time': time.time(), 'message': message})
        STATE['events'] = STATE['events'][:20]
        save_state()


def checked_path(relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise HTTPException(400, '文件路径无效')
    p = (LIBRARY / relative).resolve()
    if LIBRARY not in p.parents or not p.is_file() or p.suffix.lower() not in VIDEO:
        raise HTTPException(404, '影片文件不存在')
    return p


def register_media(p, source):
    p = p.resolve()
    if LIBRARY not in p.parents or not p.is_file() or p.suffix.lower() not in VIDEO:
        return None
    relative = str(p.relative_to(LIBRARY))
    ident = hashlib.sha256(relative.encode()).hexdigest()[:24]
    with STATE_LOCK:
        old = STATE['media'].get(ident, {})
        STATE['media'][ident] = {'id': ident, 'path': relative, 'name': p.name, 'size': p.stat().st_size,
                                'source': source, 'added': old.get('added', time.time())}
    return ident


def rpc(method, params=None):
    from downloader import get_engine
    return get_engine(ROOT, LIBRARY / 'Downloads').request(method, params)


def adb(*args, timeout=15):
    env = dict(os.environ)
    if CONFIG.get('adb_key'):
        env['ADB_VENDOR_KEYS'] = str(Path(CONFIG['adb_key']).expanduser())
    base = [CONFIG['adb'], '-P', str(CONFIG['adb_server_port'])]
    target = CONFIG['tv_host'] + ':' + str(CONFIG['tv_adb_port'])
    with ADB_LOCK:
        cmd = base + ['-s', target] + list(args)
        r = subprocess.run(cmd, env=env, capture_output=True, timeout=timeout)
        if r.returncode and any(s in r.stderr for s in [b'not found', b'offline', b'no devices']):
            subprocess.run(base + ['connect', target], env=env,
                           capture_output=True, timeout=8)
            r = subprocess.run(cmd, env=env, capture_output=True, timeout=timeout)
        if r.returncode:
            raise RuntimeError('电视连接失败，请检查电视电源和调试授权')
        return r.stdout.decode('utf-8', errors='replace').strip()


def kodi(method, params=None, timeout=3):
    r = HTTP.post(f"http://{CONFIG['tv_host']}:{CONFIG['kodi_port']}/jsonrpc",
                  auth=(CONFIG['kodi_user'], CONFIG['kodi_password']), timeout=timeout,
                  json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})
    r.raise_for_status()
    body = r.json()
    if body.get('error'):
        raise RuntimeError(body['error'].get('message', '播放器请求失败'))
    return body.get('result')


def tv_status(force=False):
    now = time.monotonic()
    if not force and CACHE['tv'] and now - CACHE['tv_at'] < 5:
        return CACHE['tv']
    data = {'online': False, 'awake': False, 'app': '', 'app_label': '电视未连接', 'player': None, 'player_known': False}
    try:
        out = adb('shell', 'dumpsys activity activities | grep mResumedActivity; dumpsys power | grep mWakefulness=')
        match = re.search(r'u0 ([\w.]+)/', out)
        data.update(online=True, awake='mWakefulness=Awake' in out, app=match.group(1) if match else '')
        data['app_label'] = next((a[0] for a in APPS.values() if a[1] == data['app']), data['app'] or '电视已连接')
        if data['app'] == 'org.xbmc.kodi':
            try:
                players = kodi('Player.GetActivePlayers', timeout=1)
                data['player_known'] = True
                if players:
                    pid = players[0]['playerid']
                    props = kodi('Player.GetProperties', {'playerid': pid, 'properties': ['speed', 'time', 'totaltime', 'percentage', 'audiostreams', 'currentaudiostream', 'subtitles', 'currentsubtitle', 'subtitleenabled']}, timeout=1)
                    item = kodi('Player.GetItem', {'playerid': pid, 'properties': ['title', 'file']}, timeout=1)
                    detail = item.get('item', {})
                    match_id = re.search(r'/media/([0-9a-f]{24})', detail.get('file', ''))
                    title = STATE['media'].get(match_id.group(1), {}).get('name') if match_id else None
                    if '/stream/' in detail.get('file', ''):
                        title = (STATE.get('active_stream') or {}).get('name') or title
                    data['player'] = dict(props, id=pid, title=title or detail.get('label', '正在播放'))
            except Exception:
                pass
    except Exception:
        pass
    CACHE['tv'], CACHE['tv_at'] = data, now
    return data


def launch_app(key):
    if key not in APPS:
        raise HTTPException(400, '未知应用')
    label, package, activity = APPS[key]
    adb('shell', 'input keyevent KEYCODE_WAKEUP')
    if key == 'home':
        adb('shell', 'input keyevent KEYCODE_HOME')
        CACHE['tv_at'] = 0
        return label
    if activity:
        component = package + '/' + activity
    else:
        out = adb('shell', 'cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.LEANBACK_LAUNCHER ' + package)
        component = next((l for l in reversed(out.splitlines()) if re.fullmatch(r'[\w.]+/[\w.$]+', l)), '')
        if not component:
            raise HTTPException(409, '找不到该应用的电视入口')
    out = adb('shell', 'am start -W -n ' + component)
    if 'Error' in out or 'Exception' in out:
        raise RuntimeError('电视未能打开该应用')
    CACHE['tv_at'] = 0
    return label


def media_signature(ident):
    return hmac.new(CONFIG['secret'].encode(), ('media:' + ident).encode(), hashlib.sha256).hexdigest()


def media_record(ident):
    with STATE_LOCK:
        item = STATE['media'].get(ident)
        if not item:
            raise HTTPException(404, '影片尚未下载完成或不在片库中')
        return item.copy(), checked_path(item['path'])


def can_autoplay(current):
    return bool(current['online'] and current['awake'] and (
        current['app'] in CONFIG['idle_packages'] or
        (current['app'] == 'org.xbmc.kodi' and current.get('player_known') and not current['player'])
    ))


def play_media(ident, automatic=False):
    with PLAY_LOCK:
        if automatic and not can_autoplay(tv_status(force=True)):
            return {'ok': False}
        item, path = media_record(ident)
        launch_app('kodi')
        for attempt in range(24):
            try:
                if kodi('JSONRPC.Ping', timeout=1) == 'pong':
                    break
            except Exception:
                time.sleep(.5)
        else:
            raise RuntimeError('Kodi 尚未准备好，请稍后重试')
        url = f"http://{CONFIG['mac_host']}:{CONFIG['port']}/media/{ident}/{quote(path.name)}?sig={media_signature(ident)}"
        kodi('Player.Open', {'item': {'file': url}}, timeout=8)
        CACHE['tv_at'] = 0
        with STATE_LOCK:
            STATE['queue'] = [q for q in STATE['queue'] if q != ident]
        event('播放到电视：' + item['name'])
        return {'ok': True, 'name': item['name']}


def sync_library():
    for p in (LIBRARY / 'Local').rglob('*'):
        if p.is_file() and not p.is_symlink() and p.suffix.lower() in VIDEO:
            register_media(p, '本地影片')
    fields = ['id', 'hash_string', 'name', 'status', 'percent_done', 'rate_download', 'rate_upload',
              'eta', 'total_size', 'download_dir', 'files', 'file_stats', 'error_string', 'metadata_percent_complete', 'peers_connected']
    result = rpc('torrent_get', {'fields': fields})
    jobs = result.get('torrents', [])
    public_jobs = []
    for t in jobs:
        ids = []
        download_root = Path(t['download_dir']).resolve()
        if download_root == (LIBRARY / 'Downloads').resolve():
            for f in t.get('files', []):
                if f.get('length', 0) > 0 and f.get('bytes_completed', 0) >= f['length']:
                    candidate = (download_root / f['name']).resolve()
                    if download_root in candidate.parents:
                        ident = register_media(candidate, '种子下载')
                        if ident:
                            ids.append(ident)
        videos = [{'index': i, 'name': f['name'], 'size': f['length']} for i, f in enumerate(t.get('files', [])) if Path(f['name']).suffix.lower() in VIDEO]
        public_jobs.append({k: t.get(k) for k in ['id', 'name', 'status', 'percent_done', 'rate_download', 'rate_upload', 'eta', 'total_size', 'error_string', 'metadata_percent_complete', 'peers_connected']} | {'media_ids': ids, 'videos': videos})
        key = t['hash_string']
        with STATE_LOCK:
            if t.get('percent_done') == 1 and ids and key not in STATE['finished']:
                STATE['finished'].append(key)
                if STATE['autoplay']:
                    main = max(ids, key=lambda x: STATE['media'][x]['size'])
                    if main not in STATE['queue']:
                        STATE['queue'].append(main)
                event('下载完成：' + t['name'])
    CACHE['jobs'], CACHE['download_error'] = public_jobs, ''
    save_state()


def worker():
    while not STOP.is_set():
        try:
            sync_library()
        except Exception:
            CACHE['download_error'] = '下载引擎暂不可用，请刷新后重试'
        try:
            streaming.tick()
            CACHE['stream_error'] = ''
        except Exception as exc:
            message = str(exc)
            if CACHE.get('stream_error') != message:
                logging.getLogger(__name__).exception('Stream preparation failed')
            CACHE['stream_error'] = message
        with STATE_LOCK:
            pending = STATE['queue'][0] if STATE['autoplay'] and STATE['queue'] else None
        if pending:
            current = tv_status(force=True)
            # Never wake the TV or interrupt another app for automatic playback.
            if can_autoplay(current):
                try:
                    play_media(pending, automatic=True)
                except Exception:
                    pass
        STOP.wait(5)


@asynccontextmanager
async def lifespan(app):
    STOP.clear()
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    yield
    STOP.set()
    thread.join(timeout=3)
    from downloader import close_engine
    close_engine()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware('http')
async def access_guard(request: Request, call_next):
    is_media = request.url.path.startswith(('/media/', '/stream/', '/subtitle-file/'))
    client = request.client.host if request.client else ''
    if not is_media:
        try:
            loopback = ipaddress.ip_address(client).is_loopback
        except ValueError:
            loopback = client == 'testclient'
        allowed_hosts = {f"localhost:{CONFIG['port']}", f"127.0.0.1:{CONFIG['port']}"}
        if not loopback or request.headers.get('host', '') not in allowed_hosts or request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail': '控制台仅允许在服务主机上访问'}, status_code=403)
        if request.method not in ['GET', 'HEAD']:
            origin = request.headers.get('origin')
            if origin and origin not in {f'http://{h}' for h in allowed_hosts}:
                return JSONResponse({'detail': '来源校验失败'}, status_code=403)
            if not hmac.compare_digest(request.headers.get('x-console-token', ''), CONFIG['secret']):
                return JSONResponse({'detail': '请刷新控制台后重试'}, status_code=403)
    response = await call_next(request)
    if not is_media:
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'"
    return response


@app.exception_handler(Exception)
@app.exception_handler(RuntimeError)
@app.exception_handler(httpx.HTTPError)
async def unexpected(request, exc):
    return JSONResponse({'detail': str(exc) if isinstance(exc, RuntimeError) else '操作未完成，请检查电视或下载器连接后重试'}, status_code=503)


@app.get('/', response_class=HTMLResponse)
def index():
    import html
    return (SOURCE_ROOT / 'index.html').read_text().replace('__CONSOLE_TOKEN__', CONFIG['secret']).replace('__TV_NAME__', html.escape(CONFIG['tv_name']))


@app.get('/api/state')
def state():
    tv = tv_status()
    with STATE_LOCK:
        media = []
        for item in STATE['media'].values():
            try:
                checked_path(item['path'])
                media.append({k: v for k, v in item.items() if k != 'path'})
            except HTTPException:
                pass
        return {'tv': tv, 'jobs': CACHE['jobs'], 'download_error': CACHE['download_error'],
                'media': sorted(media, key=lambda m: m['added'], reverse=True), 'autoplay': STATE['autoplay'],
                'queue': list(STATE['queue']), 'events': list(STATE['events'])[:6],
                'stream_request': STATE.get('stream_request'), 'active_stream': STATE.get('active_stream'),
                'free_bytes': __import__('shutil').disk_usage(LIBRARY).free,
                'apps': [{'id': k, 'label': a[0]} for k, a in APPS.items()], 'library': str(LIBRARY)}


class Magnet(BaseModel):
    magnet: str = Field(max_length=8192)


def validate_magnet(value):
    value = value.strip()
    u = urlsplit(value)
    xt = parse_qs(u.query).get('xt', [])
    if u.scheme != 'magnet' or not any(re.fullmatch(r'urn:btih:(?:[0-9a-fA-F]{40}|[A-Za-z2-7]{32})', x) for x in xt):
        raise HTTPException(400, '请输入包含有效 BT 信息哈希的 magnet 链接')
    return value


@app.post('/api/torrents/magnet')
def add_magnet(payload: Magnet):
    result = rpc('torrent_add', {'filename': validate_magnet(payload.magnet), 'download_dir': str(LIBRARY / 'Downloads'), 'paused': False})
    event('已添加磁力下载')
    return {'ok': True, 'duplicate': 'torrent_duplicate' in result}


def validate_torrent(data):
    # Bound metadata parsing before handing untrusted paths to the downloader.
    if not data or len(data) > 8 * 1024 * 1024:
        raise HTTPException(400, '种子文件为空或超过 8 MB')
    pos = 0
    def parse(depth=0):
        nonlocal pos
        if depth > 25 or pos >= len(data):
            raise ValueError()
        c = data[pos:pos + 1]
        if c == b'i':
            end = data.index(b'e', pos); n = int(data[pos + 1:end]); pos = end + 1; return n
        if c in [b'd', b'l']:
            pos += 1; items = {} if c == b'd' else []
            while data[pos:pos + 1] != b'e':
                key = parse(depth + 1)
                if c == b'd':
                    if not isinstance(key, bytes): raise ValueError()
                    items[key] = parse(depth + 1)
                else: items.append(key)
            pos += 1; return items
        end = data.index(b':', pos)
        size = int(data[pos:end]); start = end + 1
        if size < 0 or start + size > len(data): raise ValueError()
        pos = start + size; return data[start:pos]
    def component(name):
        if not isinstance(name, bytes) or name in [b'', b'.', b'..'] or any(c in name for c in [b'/', b'\\', b'\x00']):
            raise ValueError()
    try:
        meta = parse()
        if pos != len(data) or not isinstance(meta, dict) or not isinstance(meta.get(b'info'), dict): raise ValueError()
        info = meta[b'info']; component(info[b'name'])
        if b'name.utf-8' in info: component(info[b'name.utf-8'])
        for f in info.get(b'files', []):
            for k in [b'path', b'path.utf-8']:
                if k in f:
                    if not f[k]: raise ValueError()
                    for n in f[k]: component(n)
    except (ValueError, IndexError, KeyError, TypeError, OverflowError):
        raise HTTPException(400, '这不是有效的种子文件，或种子包含不安全的路径')
    return data


@app.post('/api/torrents/file')
def add_torrent(file: UploadFile = File(...)):
    data = validate_torrent(file.file.read(8 * 1024 * 1024 + 1))
    result = rpc('torrent_add', {'metainfo': base64.b64encode(data).decode(), 'download_dir': str(LIBRARY / 'Downloads'), 'paused': False})
    event('已添加种子：' + (file.filename or '未命名种子')[:150])
    return {'ok': True, 'duplicate': 'torrent_duplicate' in result}


@app.post('/api/torrents/{ident}/control/{action}')
def torrent_action(ident: int, action: str):
    if action not in ['start', 'stop', 'remove'] or ident < 0:
        raise HTTPException(400, '不支持的下载操作')
    rpc('torrent_' + action, {'ids': [ident], 'delete_local_data': False})
    return {'ok': True}


@app.post('/api/media/{ident}/play')
def play(ident: str):
    return play_media(ident)


@app.post('/api/apps/{key}')
def launch(key: str):
    label = launch_app(key)
    event('已打开：' + label)
    return {'ok': True}


@app.post('/api/remote/{key}')
def remote(key: str):
    if key not in KEYS:
        raise HTTPException(400, '未知遥控按键')
    if key in ['stop', 'home']:
        with STATE_LOCK:
            STATE['stream_request'] = None
            save_state()
    adb('shell', 'input keyevent KEYCODE_' + KEYS[key])
    CACHE['tv_at'] = 0
    return {'ok': True}


class Autoplay(BaseModel):
    enabled: bool


@app.post('/api/autoplay')
def autoplay(value: Autoplay):
    with STATE_LOCK:
        STATE['autoplay'] = value.enabled
        if not value.enabled:
            STATE['queue'] = []
        save_state()
    return {'ok': True}


@app.post('/api/folder')
def folder():
    subprocess.run(['open' if sys.platform == 'darwin' else 'xdg-open', str(LIBRARY)], check=True)
    return {'ok': True}


@app.get('/api/screen')
def screen():
    # Protected video surfaces may intentionally return an empty screenshot.
    with ADB_LOCK:
        adb('shell', 'screencap -p /data/local/tmp/tv-console-preview.png', timeout=20)
        p = ROOT / 'data/preview.png'
        adb('pull', '/data/local/tmp/tv-console-preview.png', str(p), timeout=20)
        if not p.exists() or p.stat().st_size == 0:
            raise HTTPException(409, '当前应用不允许截屏，电视仍可正常播放')
        p.chmod(0o600)
        data = p.read_bytes()
    return Response(data, media_type='image/png')


@app.api_route('/media/{ident}', methods=['GET', 'HEAD'])
@app.api_route('/media/{ident}/{filename}', methods=['GET', 'HEAD'])
def serve_media(ident: str, sig: str = '', filename: str = ''):
    if not hmac.compare_digest(sig, media_signature(ident)):
        raise HTTPException(403, '播放链接无效')
    item, p = media_record(ident)
    return FileResponse(p, media_type=mimetypes.guess_type(p.name)[0] or 'video/mp4',
                        headers={'Cache-Control': 'private, max-age=60'})


import streaming
streaming.install(app, sys.modules[__name__])
import subtitles
subtitles.install(app, sys.modules[__name__])

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=CONFIG['port'], access_log=False)
