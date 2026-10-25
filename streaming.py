import base64
import hashlib
import hmac
import mimetypes
from pathlib import Path
import re
import threading
import time
from urllib.parse import quote
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

B = None
PIECES = {}
PIECE_LOCK = threading.RLock()
FIELDS = ['id', 'hash_string', 'name', 'download_dir', 'files', 'metadata_percent_complete', 'pieces', 'piece_size', 'piece_count', 'status']


def torrent(key, fresh=False):
    with PIECE_LOCK:
        cached = PIECES.get(str(key))
        if not fresh and cached and time.monotonic() - cached[0] < .7:
            return cached[1]
        data = B.rpc('torrent_get', {'ids': [key], 'fields': FIELDS}).get('torrents', [])
        if not data:
            raise HTTPException(404, '下载任务不存在')
        t = data[0]
        PIECES[str(key)] = (time.monotonic(), t)
        return t


def file_info(t, index):
    if not 0 <= index < len(t.get('files', [])):
        raise HTTPException(409, '还没有获取到视频文件信息')
    f = t['files'][index]
    root = Path(t['download_dir']).resolve()
    p = (root / f['name']).resolve()
    if root != (B.LIBRARY / 'Downloads').resolve() or root not in p.parents or p.suffix.lower() not in B.VIDEO:
        raise HTTPException(400, '只允许播放片库目录内的视频文件')
    offset = sum(x['length'] for x in t['files'][:index])
    return f, p, offset


def signature(key, index):
    return hmac.new(B.CONFIG['secret'].encode(), f'stream:{key}:{index}'.encode(), hashlib.sha256).hexdigest()


def available_bytes(t, offset, length):
    size = t.get('piece_size', 0)
    if not size or length <= 0:
        return 0
    bits = base64.b64decode(t.get('pieces') or '')
    cursor, end = offset, offset + length
    while cursor < end:
        piece = cursor // size
        if piece // 8 >= len(bits) or not (bits[piece // 8] & (128 >> (piece % 8))):
            break
        cursor = min(end, (piece + 1) * size)
    return cursor - offset


def parse_range(header, length):
    if not header:
        return 0, length - 1, 200
    m = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
    if not m or not any(m.groups()):
        raise HTTPException(416, '不支持的播放范围', headers={'Content-Range': f'bytes */{length}'})
    first, last = m.groups()
    if not first:
        count = int(last)
        start, end = max(0, length - count), length - 1
    else:
        start, end = int(first), min(int(last), length - 1) if last else length - 1
    if start < 0 or start >= length or end < start:
        raise HTTPException(416, '播放范围超出文件', headers={'Content-Range': f'bytes */{length}'})
    return start, end, 206


def stream_bytes(key, index, start, end, wait_limit=150):
    position, last_progress, last_priority = start, time.monotonic(), 0
    while position <= end:
        t = torrent(key)
        f, p, offset = file_info(t, index)
        count = available_bytes(t, offset + position, min(512 * 1024, end - position + 1))
        candidate = p if p.exists() else Path(str(p) + '.part')
        if candidate.is_symlink() or candidate.resolve().parent != p.parent:
            raise RuntimeError('影片路径已改变')
        if count and candidate.exists():
            with candidate.open('rb') as handle:
                handle.seek(position)
                data = handle.read(count)
            if data:
                position += len(data)
                last_progress = time.monotonic()
                yield data
                continue
        now = time.monotonic()
        if now - last_progress > wait_limit:
            raise RuntimeError('下载速度不足，播放缓冲已超时')
        if now - last_priority > 4 and t.get('piece_size'):
            B.rpc('torrent_set', {'ids': [key], 'sequential_download': True,
                                 'sequential_download_from_piece': (offset + position) // t['piece_size']})
            last_priority = now
        time.sleep(.5)


def tick():
    if B is None:
        return
    with B.STATE_LOCK:
        req = B.STATE.get('stream_request')
        if not req:
            return
        req = req.copy()
    if time.time() - req['requested_at'] > 1800:
        with B.STATE_LOCK:
            B.STATE['stream_request'] = None
        B.event('等待资源超过 30 分钟，已取消自动推送；下载任务保留')
        return
    t = torrent(req['hash'], fresh=True)
    if t.get('metadata_percent_complete', 0) < 1:
        return
    videos = [(i, x) for i, x in enumerate(t['files']) if Path(x['name']).suffix.lower() in B.VIDEO]
    if not videos:
        with B.STATE_LOCK:
            B.STATE['stream_request'] = None
        B.event('该种子中没有可直接播放的视频文件')
        return
    index = req.get('index')
    if index is None:
        index = max(videos, key=lambda item: item[1]['length'])[0]
    f, p, offset = file_info(t, index)
    if not req.get('prioritized'):
        B.rpc('torrent_set', {'ids': [t['id']], 'sequential_download': True,
                             'sequential_download_from_piece': offset // t['piece_size'], 'priority_high': [index]})
        with B.STATE_LOCK:
            if (B.STATE.get('stream_request') or {}).get('hash') == req['hash']:
                B.STATE['stream_request'].update(index=index, prioritized=True, name=f['name'], status='正在缓冲视频开头')
                B.save_state()
    target = min(2 * 1024 * 1024, f['length'])
    have = available_bytes(t, offset, target)
    with B.STATE_LOCK:
        if (B.STATE.get('stream_request') or {}).get('hash') == req['hash']:
            B.STATE['stream_request'].update(buffered=have, buffer_target=target)
    if have < target:
        return
    current = B.tv_status(force=True)
    if not current['online'] or not current['awake']:
        return
    with B.PLAY_LOCK:
        with B.STATE_LOCK:
            pending = B.STATE.get('stream_request') or {}
            if pending.get('requested_at') != req['requested_at'] or pending.get('hash') != req['hash']:
                return
        B.launch_app('kodi')
        for _ in range(24):
            try:
                if B.kodi('JSONRPC.Ping', timeout=1) == 'pong': break
            except Exception: time.sleep(.5)
        with B.STATE_LOCK:
            pending = B.STATE.get('stream_request') or {}
            if pending.get('requested_at') != req['requested_at'] or pending.get('hash') != req['hash']:
                return
        url = f"http://{B.CONFIG['mac_host']}:{B.CONFIG['port']}/stream/{req['hash']}/{index}/{quote(p.name)}?sig={signature(req['hash'], index)}"
        B.kodi('Player.Open', {'item': {'file': url}}, timeout=8)
        with B.STATE_LOCK:
            B.STATE['active_stream'] = {'hash': req['hash'], 'index': index, 'name': p.name, 'path': str(p.relative_to(B.LIBRARY))}
            if (B.STATE.get('stream_request') or {}).get('requested_at') == req['requested_at']:
                B.STATE['stream_request'] = None
        B.event('已发送边下边播：' + p.name)
        B.CACHE['tv_at'] = 0


class StreamChoice(BaseModel):
    index: Optional[int] = None


class TrackChoice(BaseModel):
    index: int


class SeekChoice(BaseModel):
    percentage: float = Field(ge=0, le=100)


def install(app, backend):
    global B
    B = backend

    @app.post('/api/torrents/{ident}/stream')
    def queue_stream(ident: int, choice: StreamChoice):
        t = torrent(ident, fresh=True)
        if choice.index is not None and t.get('metadata_percent_complete') == 1:
            file_info(t, choice.index)
        B.rpc('torrent_set', {'ids': [ident], 'sequential_download': True})
        B.rpc('torrent_start_now', {'ids': [ident]})
        with B.STATE_LOCK:
            B.STATE['stream_request'] = {'hash': t['hash_string'], 'index': choice.index, 'requested_at': time.time(),
                                         'status': '正在获取资源信息' if not t['files'] else '正在缓冲视频开头', 'name': t['name']}
            B.save_state()
        return {'ok': True, 'queued': True}

    @app.post('/api/stream/cancel')
    def cancel_stream():
        with B.STATE_LOCK:
            B.STATE['stream_request'] = None
            B.save_state()
        return {'ok': True}

    @app.api_route('/stream/{key}/{index}/{filename}', methods=['GET', 'HEAD'])
    def serve_stream(key: str, index: int, filename: str, request: Request, sig: str = ''):
        if not re.fullmatch('[0-9a-f]{40}', key) or not hmac.compare_digest(sig, signature(key, index)):
            raise HTTPException(403, '播放链接无效')
        t = torrent(key)
        f, p, offset = file_info(t, index)
        start, end, status = parse_range(request.headers.get('range'), f['length'])
        headers = {'Accept-Ranges': 'bytes', 'Content-Length': str(end - start + 1), 'Cache-Control': 'no-store'}
        if status == 206:
            headers['Content-Range'] = f"bytes {start}-{end}/{f['length']}"
        media_type = mimetypes.guess_type(p.name)[0] or 'video/mp4'
        if request.method == 'HEAD':
            return Response(status_code=status, headers=headers, media_type=media_type)
        return StreamingResponse(stream_bytes(key, index, start, end), status_code=status, headers=headers, media_type=media_type)

    def active_player():
        players = [p for p in B.kodi('Player.GetActivePlayers') if p.get('type') == 'video']
        if not players:
            raise HTTPException(409, '请先在电视上播放影片')
        return players[0]['playerid']

    @app.post('/api/player/audio')
    def audio(choice: TrackChoice):
        if choice.index < 0:
            raise HTTPException(400, '音轨编号无效')
        B.kodi('Player.SetAudioStream', {'playerid': active_player(), 'stream': choice.index})
        B.CACHE['tv_at'] = 0
        return {'ok': True}

    @app.post('/api/player/subtitle')
    def subtitle(choice: TrackChoice):
        B.kodi('Player.SetSubtitle', {'playerid': active_player(), 'subtitle': 'off' if choice.index < 0 else choice.index, 'enable': choice.index >= 0})
        B.CACHE['tv_at'] = 0
        return {'ok': True}

    @app.post('/api/player/seek')
    def seek(choice: SeekChoice):
        B.kodi('Player.Seek', {'playerid': active_player(), 'value': choice.percentage})
        B.CACHE['tv_at'] = 0
        return {'ok': True}
