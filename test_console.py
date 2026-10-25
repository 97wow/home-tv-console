import hashlib
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
import server


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    library = tmp_path / 'library'
    (library / 'Local').mkdir(parents=True)
    monkeypatch.setattr(server, 'LIBRARY', library)
    monkeypatch.setattr(server, 'STATE_FILE', tmp_path / 'state.json')
    monkeypatch.setattr(server, 'STATE', {'autoplay': False, 'media': {}, 'finished': [], 'queue': [], 'events': []})


def bencode(value):
    if isinstance(value, bytes): return str(len(value)).encode() + b':' + value
    if isinstance(value, int): return b'i' + str(value).encode() + b'e'
    if isinstance(value, list): return b'l' + b''.join(bencode(x) for x in value) + b'e'
    return b'd' + b''.join(bencode(k) + bencode(v) for k, v in sorted(value.items())) + b'e'


def test_metadata_accepts_a_real_file_manifest():
    torrent = bencode({b'info': {b'name': b'free-film.mp4', b'length': 4, b'piece length': 16384, b'pieces': hashlib.sha1(b'test').digest()}})
    assert server.validate_torrent(torrent) == torrent


@pytest.mark.parametrize('name', [b'../escape', b'/absolute', b'..', b'a\\b', b'zero\x00'])
def test_torrent_rejects_path_escape(name):
    with pytest.raises(HTTPException):
        server.validate_torrent(bencode({b'info': {b'name': name}}))


def test_multifile_torrent_cannot_escape_download_root():
    with pytest.raises(HTTPException):
        server.validate_torrent(bencode({b'info': {b'name': b'film', b'files': [{b'path': [b'..', b'escape.mp4'], b'length': 4}]}}))


@pytest.mark.parametrize('data', [b'', b'd4:infod4:name4:film', b'd4:infodeeTRAILER', b'not-a-torrent'])
def test_corrupt_metadata_is_rejected(data):
    with pytest.raises(HTTPException): server.validate_torrent(data)


def test_only_magnet_identifiers_are_accepted():
    valid = 'magnet:?xt=urn:btih:' + 'a' * 40
    assert server.validate_magnet(valid) == valid
    for value in ['http://127.0.0.1/private', 'file:///etc/passwd', 'magnet:?dn=film']:
        with pytest.raises(HTTPException): server.validate_magnet(value)


def test_mutating_api_rejects_cross_site_requests():
    c = TestClient(server.app, base_url="http://127.0.0.1:17880")
    assert c.post('/api/autoplay', json={'enabled': True}).status_code == 403
    assert c.post('/api/autoplay', json={'enabled': True}, headers={'X-Console-Token': server.CONFIG['secret'], 'Origin': 'https://evil.example'}).status_code == 403
    assert server.STATE['autoplay'] is False


def test_local_authenticated_control_works():
    c = TestClient(server.app, base_url="http://127.0.0.1:17880")
    assert c.post('/api/autoplay', json={'enabled': True}, headers={'X-Console-Token': server.CONFIG['secret']}).status_code == 200
    assert server.STATE['autoplay'] is True


def test_dns_rebinding_host_cannot_read_console_token():
    c = TestClient(server.app, base_url='http://evil.example')
    assert c.get('/').status_code == 403


def test_media_requires_signature_and_supports_seeking():
    p = server.LIBRARY / 'Local/test.mp4'
    p.write_bytes(bytes(range(100)))
    ident = server.register_media(p, 'test')
    c = TestClient(server.app, base_url="http://127.0.0.1:17880")
    assert c.get('/media/' + ident).status_code == 403
    r = c.get('/media/' + ident, params={'sig': server.media_signature(ident)}, headers={'Range': 'bytes=10-19'})
    assert r.status_code == 206
    assert r.content == bytes(range(10, 20))
    assert r.headers['content-range'] == 'bytes 10-19/100'
    r = c.get('/media/' + ident + '/test.mp4', params={'sig': server.media_signature(ident)}, headers={'Range': 'bytes=20-29'})
    assert r.status_code == 206
    assert r.content == bytes(range(20, 30))
    r = c.get('/media/' + ident, params={'sig': server.media_signature(ident)}, headers={'Range': 'bytes=999-1000'})
    assert r.status_code == 416


def test_media_symlink_cannot_read_outside_library(tmp_path):
    outside = tmp_path / 'outside.mp4'
    outside.write_bytes(b'private')
    (server.LIBRARY / 'Local/link.mp4').symlink_to(outside)
    with pytest.raises(HTTPException): server.checked_path('Local/link.mp4')
    assert server.register_media(outside, 'test') is None


def test_auto_play_never_interrupts_busy_unknown_or_sleeping_tv():
    base = {'online': True, 'awake': True, 'app': 'com.gathin.tvhome', 'player': None, 'player_known': False}
    assert server.can_autoplay(base)
    assert not server.can_autoplay(dict(base, awake=False))
    assert not server.can_autoplay(dict(base, app='com.apple.atve.androidtv.appletv'))
    assert not server.can_autoplay(dict(base, app='org.xbmc.kodi'))
    assert not server.can_autoplay(dict(base, app='org.xbmc.kodi', player_known=True, player={'speed': 0}))
    assert server.can_autoplay(dict(base, app='org.xbmc.kodi', player_known=True))
