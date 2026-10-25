import base64
import threading
import time

from fastapi import HTTPException
import pytest
import server
import streaming
from fastapi.testclient import TestClient


def bits(value):
    return {'piece_size': 4, 'pieces': base64.b64encode(bytes([value])).decode()}


def test_unverified_sparse_bytes_are_never_reported_available():
    assert streaming.available_bytes(bits(0b10100000), 0, 12) == 4
    assert streaming.available_bytes(bits(0b10100000), 4, 4) == 0
    assert streaming.available_bytes(bits(0b10100000), 8, 4) == 4
    assert streaming.available_bytes(bits(0b11000000), 2, 9) == 6


def test_byte_ranges_cover_seeks_suffix_and_end_bounds():
    assert streaming.parse_range('bytes=5-10', 20) == (5, 10, 206)
    assert streaming.parse_range('bytes=-5', 20) == (15, 19, 206)
    assert streaming.parse_range('bytes=18-999', 20) == (18, 19, 206)
    assert streaming.parse_range(None, 20) == (0, 19, 200)
    for value in ['bytes=90-', 'bytes=8-2', 'bytes=-0', 'bytes=0-1,3-4', 'bad']:
        with pytest.raises(HTTPException): streaming.parse_range(value, 20)


def test_stream_waits_for_verified_pieces_before_reading(monkeypatch, tmp_path):
    p = tmp_path / 'film.mp4.part'
    p.write_bytes(b'AAAABBBBCCCC')
    current = {'mask': 0b10000000}
    monkeypatch.setattr(streaming, 'torrent', lambda key: bits(current['mask']))
    monkeypatch.setattr(streaming, 'file_info', lambda t, index: ({'length': 12}, tmp_path / 'film.mp4', 0))
    monkeypatch.setattr(server, 'rpc', lambda *a, **k: {})
    stream = streaming.stream_bytes('a' * 40, 0, 0, 11, wait_limit=3)
    assert next(stream) == b'AAAA'
    timer = threading.Timer(.2, lambda: current.update(mask=0b11100000))
    timer.start()
    assert b''.join(stream) == b'BBBBCCCC'
    timer.join()


def test_stream_request_reaches_queue_route_and_preserves_torrent(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(server, 'STATE', {'stream_request': None})
    monkeypatch.setattr(server, 'STATE_FILE', tmp_path / 'state.json')
    monkeypatch.setattr(streaming, 'torrent', lambda key, fresh=False: {
        'hash_string': 'a' * 40, 'name': 'waiting', 'files': [], 'metadata_percent_complete': 0})
    monkeypatch.setattr(server, 'rpc', lambda method, params: calls.append((method, params)))
    client = TestClient(server.app, base_url="http://127.0.0.1:17880")
    response = client.post('/api/torrents/7/stream', json={}, headers={'X-Console-Token': server.CONFIG['secret']})
    assert response.status_code == 200
    assert response.json()['queued'] is True
    assert server.STATE['stream_request']['hash'] == 'a' * 40
    assert [method for method, params in calls] == ['torrent_set', 'torrent_start_now']
    response = client.post('/api/stream/cancel', headers={'X-Console-Token': server.CONFIG['secret']})
    assert response.status_code == 200
    assert server.STATE['stream_request'] is None
