"""Persistent libtorrent engine with the console's existing transfer interface."""
import base64
import json
from pathlib import Path
import threading
import time

import libtorrent as lt


class Engine:
    def __init__(self, root, download_dir, settings=None):
        self.root = Path(root) / 'data/torrents'
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.download_dir = Path(download_dir).resolve()
        self.lock = threading.RLock()
        self.records = {}
        self.handles = {}
        self.next_id = 1
        self.last_save = 0
        options = {
            'listen_interfaces': '0.0.0.0:17883', 'enable_upnp': False, 'enable_natpmp': False,
            'enable_lsd': False, 'enable_dht': True,
            'upload_rate_limit': 1024 * 1024, 'connections_limit': 200,
            'alert_mask': lt.alert.category_t.storage_notification | lt.alert.category_t.status_notification |
                          lt.alert.category_t.error_notification,
        }
        options.update(settings or {})
        self.session = lt.session(options)
        manifest = self.root / 'jobs.json'
        if manifest.exists():
            saved = json.loads(manifest.read_text())
            self.next_id = saved['next_id']
            for record in saved['jobs']:
                self.records[record['hash']] = record
                self._restore(record)

    def _write(self, path, data):
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_bytes(data)
        tmp.chmod(0o600)
        tmp.replace(path)

    def _manifest(self):
        self._write(self.root / 'jobs.json', json.dumps({'next_id': self.next_id,
                    'jobs': list(self.records.values())}, ensure_ascii=False).encode())

    def _restore(self, record):
        key = record['hash']
        resume, metadata = self.root / (key + '.resume'), self.root / (key + '.torrent')
        params = lt.read_resume_data(resume.read_bytes()) if resume.exists() else lt.add_torrent_params()
        if metadata.exists():
            params.ti = lt.torrent_info(str(metadata))
        elif record.get('magnet'):
            parsed = lt.parse_magnet_uri(record['magnet'])
            params.info_hashes, params.trackers = parsed.info_hashes, parsed.trackers
        else:
            raise RuntimeError('下载任务缺少种子信息')
        params.save_path = str(self.download_dir)
        params.flags &= ~lt.torrent_flags.auto_managed
        if record.get('paused'):
            params.flags |= lt.torrent_flags.paused
        else:
            params.flags &= ~lt.torrent_flags.paused
        if record.get('sequential', True):
            params.flags |= lt.torrent_flags.sequential_download
        self.handles[key] = self.session.add_torrent(params)

    def _metadata(self, key, handle):
        path = self.root / (key + '.torrent')
        if not path.exists() and handle.status().has_metadata:
            self._write(path, lt.bencode(lt.create_torrent(handle.torrent_file()).generate()))

    def _maintenance(self, force=False):
        for alert in self.session.pop_alerts():
            if isinstance(alert, lt.save_resume_data_alert):
                key = str(alert.params.info_hashes.v1)
                if key in self.records:
                    self._write(self.root / (key + '.resume'), lt.write_resume_data_buf(alert.params))
        if force or time.monotonic() - self.last_save > 15:
            self.last_save = time.monotonic()
            for key, handle in self.handles.items():
                self._metadata(key, handle)
                status = handle.status()
                if status.is_seeding and status.all_time_upload >= max(status.total_wanted, status.all_time_download, 1):
                    handle.pause()
                    self.records[key]['paused'] = True
                handle.save_resume_data(lt.torrent_handle.save_info_dict)

    def _selected(self, ids):
        if ids is None:
            return list(self.handles)
        return [key for key, record in self.records.items() if key in ids or record['id'] in ids]

    def _snapshot(self, key):
        h, record = self.handles[key], self.records[key]
        st = h.status(lt.torrent_handle.query_pieces)
        info = h.torrent_file() if st.has_metadata else None
        files, stats = [], []
        if info:
            storage = info.files()
            progress = h.file_progress(lt.torrent_handle.piece_granularity)
            priorities = h.get_file_priorities()
            for index in range(storage.num_files()):
                files.append({'name': storage.file_path(index), 'length': storage.file_size(index),
                              'bytes_completed': progress[index]})
                stats.append({'bytes_completed': progress[index], 'wanted': priorities[index] > 0,
                              'priority': priorities[index]})
        piece_bits = bytearray((len(st.pieces) + 7) // 8)
        for index, have in enumerate(st.pieces):
            if have: piece_bits[index // 8] |= 128 >> (index % 8)
        state = 0 if st.paused else 2 if int(st.state) in [0, 1, 7] else 6 if st.is_finished else 4
        rate = st.download_payload_rate
        return {'id': record['id'], 'hash_string': key, 'name': info.name() if info else record.get('name', key),
                'status': state, 'percent_done': st.total_wanted_done / st.total_wanted if st.total_wanted else 0,
                'rate_download': rate, 'rate_upload': st.upload_payload_rate,
                'eta': int((st.total_wanted - st.total_wanted_done) / rate) if rate else -2,
                'total_size': info.total_size() if info else 0, 'download_dir': str(self.download_dir),
                'files': files, 'file_stats': stats, 'error_string': st.errc.message() if st.errc.value() else '',
                'metadata_percent_complete': 1 if st.has_metadata else 0, 'peers_connected': st.num_peers,
                'pieces': base64.b64encode(piece_bits).decode(), 'piece_size': info.piece_length() if info else 0,
                'piece_count': info.num_pieces() if info else 0, 'sequential_download': record.get('sequential', True),
                'webseeds_sending_to_us': st.num_seeds, 'downloaded_ever': st.total_payload_download}

    def _add(self, params):
        if Path(params.get('download_dir', str(self.download_dir))).resolve() != self.download_dir:
            raise RuntimeError('下载目录必须位于电视片库')
        raw = base64.b64decode(params['metainfo']) if params.get('metainfo') else None
        if raw:
            info = lt.torrent_info(lt.bdecode(raw))
            if not info.info_hashes().has_v1():
                raise RuntimeError('目前支持 BT v1 或 v1/v2 混合种子')
            key, name = str(info.info_hashes().v1), info.name()
        else:
            parsed = lt.parse_magnet_uri(params['filename'])
            key, name = str(parsed.info_hashes.v1), parsed.name
        if key in self.records:
            if raw and not self.handles[key].status().has_metadata:
                self.handles[key].set_metadata(info.metadata())
                self._write(self.root / (key + '.torrent'), raw)
            return {'torrent_duplicate': {'id': self.records[key]['id'], 'hash_string': key, 'name': name}}
        record = {'id': self.next_id, 'hash': key, 'name': name or key, 'paused': params.get('paused', False),
                  'sequential': True, 'magnet': params.get('filename', '')}
        self.next_id += 1
        if raw: self._write(self.root / (key + '.torrent'), raw)
        self.records[key] = record
        try:
            self._restore(record)
        except Exception:
            self.records.pop(key)
            raise
        self._manifest()
        self._set(key, params)
        return {'torrent_added': {'id': record['id'], 'hash_string': key, 'name': name}}

    def _set(self, key, params):
        h, record = self.handles[key], self.records[key]
        if 'sequential_download' in params:
            record['sequential'] = params['sequential_download']
            if record['sequential']: h.set_flags(lt.torrent_flags.sequential_download)
            else: h.unset_flags(lt.torrent_flags.sequential_download)
        if 'download_limit' in params:
            h.set_download_limit(params['download_limit'] * 1024 if params.get('download_limited', True) else 0)
        if 'tracker_list' in params:
            for url in params['tracker_list'].splitlines():
                if url.strip(): h.add_tracker({'url': url.strip()})
        if h.status().has_metadata:
            priorities = h.get_file_priorities()
            changed = False
            for field, priority in [('files_unwanted', 0), ('files_wanted', 4), ('priority_high', 7)]:
                for index in params.get(field, []):
                    if not 0 <= index < len(priorities): raise RuntimeError('视频文件编号无效')
                    priorities[index] = priority
                    changed = True
            if changed: h.prioritize_files(priorities)
            if 'sequential_download_from_piece' in params:
                info = h.torrent_file()
                first = int(params['sequential_download_from_piece'])
                if not 0 <= first < info.num_pieces(): raise RuntimeError('视频缓冲位置无效')
                h.clear_piece_deadlines()
                count = max(2, min(32, (8 * 1024 * 1024 + info.piece_length() - 1) // info.piece_length()))
                for piece in range(first, min(info.num_pieces(), first + count)):
                    h.set_piece_deadline(piece, (piece - first) * 200)
        self._manifest()

    def request(self, method, params=None):
        params = params or {}
        with self.lock:
            self._maintenance()
            if method == 'torrent_add': return self._add(params)
            keys = self._selected(params.get('ids'))
            if method == 'torrent_get':
                rows = [self._snapshot(key) for key in keys]
                fields = params.get('fields')
                return {'torrents': [{k: v for k, v in row.items() if not fields or k in fields} for row in rows]}
            for key in keys:
                h = self.handles[key]
                if method in ['torrent_start', 'torrent_start_now']:
                    h.resume(); self.records[key]['paused'] = False
                elif method == 'torrent_stop':
                    h.pause(); self.records[key]['paused'] = True
                elif method == 'torrent_set': self._set(key, params)
                elif method == 'torrent_reannounce': h.force_reannounce()
                elif method == 'torrent_remove':
                    self.session.remove_torrent(h, lt.options_t.delete_files if params.get('delete_local_data') else 0)
                    self.handles.pop(key); self.records.pop(key)
                    for ext in ['.torrent', '.resume']:
                        (self.root / (key + ext)).unlink(missing_ok=True)
                else: raise RuntimeError('不支持的下载操作')
            self._manifest()
            return {}

    def close(self):
        with self.lock:
            self._maintenance(force=True)
            self.session.pause()
            until = time.monotonic() + 2
            while time.monotonic() < until:
                time.sleep(.05)
                self._maintenance()
            self._manifest()


_engine = None
_engine_lock = threading.Lock()


def get_engine(root, download_dir):
    global _engine
    with _engine_lock:
        if _engine is None: _engine = Engine(root, download_dir)
        return _engine


def close_engine():
    if _engine is not None: _engine.close()
