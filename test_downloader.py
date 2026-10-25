import base64
import hashlib
import json

from downloader import Engine
from test_console import bencode


def test_metadata_upgrade_persistence_and_non_destructive_removal(tmp_path):
    (tmp_path / 'data').mkdir()
    library = tmp_path / 'downloads'
    library.mkdir()
    data = b'video' * 10000
    piece = 16384
    info = {b'name': b'film.mp4', b'length': len(data), b'private': 1, b'piece length': piece,
            b'pieces': b''.join(hashlib.sha1(data[i:i + piece]).digest() for i in range(0, len(data), piece))}
    key = hashlib.sha1(bencode(info)).hexdigest()
    options = {'listen_interfaces': '127.0.0.1:0', 'enable_dht': False}
    engine = Engine(tmp_path, library, settings=options)
    try:
        result = engine.request('torrent_add', {'filename': 'magnet:?xt=urn:btih:' + key,
                                               'paused': True, 'download_dir': str(library)})
        ident = result['torrent_added']['id']
        result = engine.request('torrent_add', {'metainfo': base64.b64encode(bencode({b'info': info})).decode(),
                                               'download_dir': str(library), 'paused': True})
        assert result['torrent_duplicate']['id'] == ident
        rows = engine.request('torrent_get')['torrents']
        assert len(rows) == 1
        assert rows[0]['metadata_percent_complete'] == 1
        assert rows[0]['files'][0]['length'] == len(data)
        assert rows[0]['status'] == 0
        assert rows[0]['files'][0]['bytes_completed'] == 0
        engine.request('torrent_set', {'ids': [ident], 'priority_high': [0],
                                       'sequential_download': True, 'sequential_download_from_piece': 1})
    finally:
        engine.close()
    restored = Engine(tmp_path, library, settings=options)
    try:
        rows = restored.request('torrent_get', {'ids': [ident]})['torrents']
        assert len(rows) == 1 and rows[0]['hash_string'] == key
        assert rows[0]['status'] == 0
        movie = library / 'film.mp4'
        movie.write_bytes(data)
        restored.request('torrent_remove', {'ids': [ident], 'delete_local_data': False})
        assert restored.request('torrent_get')['torrents'] == []
        assert movie.read_bytes() == data
        assert json.loads((tmp_path / 'data/torrents/jobs.json').read_text())['jobs'] == []
    finally:
        restored.close()
