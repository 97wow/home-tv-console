import stat

import pytest

from configuration import create_config, load_config, write_config


def test_private_setup_creates_unique_secret_and_does_not_overwrite(tmp_path, monkeypatch):
    first = create_config('192.0.2.10', '192.0.2.20', 'local-test', library=tmp_path / 'library')
    second = create_config('192.0.2.10', '192.0.2.20', 'local-test', library=tmp_path / 'library')
    assert first['secret'] != second['secret']
    path = write_config(first, tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_config(second, tmp_path)
    monkeypatch.setenv('TV_CONSOLE_HOME', str(tmp_path))
    root, config = load_config()
    assert root == tmp_path
    assert config['secret'] == first['secret']
    assert (tmp_path / 'library/Local').is_dir()
    assert (tmp_path / 'library/Downloads').is_dir()


@pytest.mark.parametrize('field,value', [('tv_host', 'device;id'), ('mac_host', '0.0.0.0'),
                                        ('port', -1), ('tv_adb_port', 99999)])
def test_invalid_network_configuration_is_rejected(field, value):
    args = {'tv_host': '192.0.2.10', 'mac_host': '192.0.2.20', 'kodi_password': 'local-test', field: value}
    with pytest.raises(ValueError):
        create_config(**args)


def test_custom_app_cannot_inject_a_shell_command():
    with pytest.raises(ValueError):
        create_config('192.0.2.10', '192.0.2.20', 'local-test',
                      apps={'home': ['Home', '', ''], 'kodi': ['Kodi', 'org.xbmc.kodi;id', '.Splash']})
