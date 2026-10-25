import hashlib
import hmac
import os
from pathlib import Path
import re
import subprocess
import shutil
import threading
from urllib.parse import quote, urlsplit

from fastapi import File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

SUB_LOCK = threading.Lock()
FORMATS = {'.srt', '.ass', '.ssa', '.vtt'}


class SubtitleLanguage(BaseModel):
    language: str = 'zh-cn'


def install(app, b):
    cache = b.ROOT / 'data/subtitles'
    cache.mkdir(exist_ok=True)

    def playing():
        players = b.kodi('Player.GetActivePlayers')
        videos = [p for p in players if p.get('type') == 'video']
        if not videos:
            raise HTTPException(409, '请先在电视上播放视频，再获取字幕')
        pid = videos[0]['playerid']
        item = b.kodi('Player.GetItem', {'playerid': pid, 'properties': ['file', 'title']})['item']
        parts = urlsplit(item.get('file', '')).path.split('/')
        if len(parts) >= 3 and parts[1] == 'media':
            record, path = b.media_record(parts[2])
        elif len(parts) >= 4 and parts[1] == 'stream':
            import streaming
            t = streaming.torrent(parts[2]); f, path, offset = streaming.file_info(t, int(parts[3]))
        else:
            raise HTTPException(409, '字幕获取目前支持从本控制台播放的影片')
        ident = hashlib.sha256(str(path.relative_to(b.LIBRARY)).encode()).hexdigest()[:24]
        work = cache / ident
        work.mkdir(exist_ok=True)
        return pid, path, ident, work, item.get('file')

    def signature(ident, name):
        return hmac.new(b.CONFIG['secret'].encode(), f'subtitle:{ident}:{name}'.encode(), hashlib.sha256).hexdigest()

    def attach(pid, ident, file, original_url):
        now = b.kodi('Player.GetItem', {'playerid': pid, 'properties': ['file']})['item'].get('file')
        if now != original_url:
            raise HTTPException(409, '字幕已经保存，但电视切换了影片；未给新影片加载旧字幕')
        url = f"http://{b.CONFIG['mac_host']}:{b.CONFIG['port']}/subtitle-file/{ident}/{quote(file.name)}?sig={signature(ident, file.name)}"
        b.kodi('Player.AddSubtitle', {'playerid': pid, 'subtitle': url}, timeout=10)
        tracks = b.kodi('Player.GetProperties', {'playerid': pid, 'properties': ['subtitles']}).get('subtitles', [])
        if tracks:
            b.kodi('Player.SetSubtitle', {'playerid': pid, 'subtitle': tracks[-1]['index'], 'enable': True})
        b.CACHE['tv_at'] = 0
        return {'ok': True, 'name': file.name}

    @app.post('/api/subtitles/fetch')
    def fetch_subtitle(choice: SubtitleLanguage):
        if choice.language not in ['zh-cn', 'zh-tw', 'en', 'ja']:
            raise HTTPException(400, '暂不支持这个字幕语言')
        if not SUB_LOCK.acquire(blocking=False):
            raise HTTPException(409, '正在获取字幕，请稍候')
        try:
            pid, path, ident, work, original_url = playing()
            source = path if path.exists() else Path(str(path) + '.part')
            if not source.exists():
                raise HTTPException(409, '视频还没有下载到足够的数据')
            local = work / path.name
            if local.exists() and local.stat().st_ino != source.stat().st_ino:
                local.unlink()
            if not local.exists():
                os.link(source, local)
            cli = b.SOURCE_ROOT / 'subtitle-client/node_modules/opensubs-cli/dist/Run.js'
            node = b.CONFIG['node']
            if not cli.is_file() or not shutil.which(node):
                raise HTTPException(409, '请先安装 Node.js 和可选字幕客户端：npm install --prefix subtitle-client')
            env = dict(os.environ, NODE_USE_ENV_PROXY='1')
            if b.CONFIG.get('subtitle_proxy'):
                env.update(HTTPS_PROXY=b.CONFIG['subtitle_proxy'], HTTP_PROXY=b.CONFIG['subtitle_proxy'])
            try:
                result = subprocess.run([node, str(cli), str(local), '-l', choice.language, '-N'],
                                        env=env, capture_output=True, text=True, timeout=90)
            except subprocess.TimeoutExpired:
                raise HTTPException(504, '字幕网站响应超时，可稍后重试或导入本地字幕')
            candidates = [p for p in work.iterdir() if p.is_file() and p.suffix.lower() in FORMATS and
                          p.name.startswith(path.stem + '.') and choice.language in p.name and p.stat().st_size <= 8 * 1024 * 1024]
            if not candidates:
                output = (result.stdout + result.stderr).lower()
                if any(word in output for word in ['quota', 'login required', 'limit reached', 'log in', 'anonymous downloads']):
                    raise HTTPException(409, 'OpenSubtitles 匿名额度已用完，需要在官方客户端登录免费账号；也可以导入已有字幕')
                raise HTTPException(404, '没有找到匹配字幕，或字幕服务暂不可用；可换语言重试，也可导入 SRT / ASS 字幕')
            file = max(candidates, key=lambda p: p.stat().st_mtime)
            response = attach(pid, ident, file, original_url)
            b.event('已获取并加载字幕：' + file.name)
            return response
        finally:
            SUB_LOCK.release()

    @app.post('/api/subtitles/upload')
    def upload_subtitle(file: UploadFile = File(...)):
        ext = Path(file.filename or '').suffix.lower()
        if ext not in FORMATS:
            raise HTTPException(400, '支持 SRT、ASS、SSA 和 VTT 字幕')
        data = file.file.read(8 * 1024 * 1024 + 1)
        if not data or len(data) > 8 * 1024 * 1024:
            raise HTTPException(400, '字幕文件为空或超过 8 MB')
        pid, path, ident, work, original_url = playing()
        target = work / ('import-' + hashlib.sha256(data).hexdigest()[:12] + ext)
        target.write_bytes(data)
        return attach(pid, ident, target, original_url)

    @app.get('/subtitle-file/{ident}/{name}')
    def subtitle_file(ident: str, name: str, sig: str = ''):
        if not re.fullmatch('[0-9a-f]{24}', ident) or Path(name).name != name or Path(name).suffix.lower() not in FORMATS:
            raise HTTPException(404)
        if not hmac.compare_digest(sig, signature(ident, name)):
            raise HTTPException(403)
        p = (cache / ident / name).resolve()
        if cache.resolve() not in p.parents or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, media_type='text/plain; charset=utf-8')
