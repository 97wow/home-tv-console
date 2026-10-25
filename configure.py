"""Create private configuration without putting passwords in command arguments."""
import argparse
import getpass
import socket

from configuration import create_config, write_config


def main():
    parser = argparse.ArgumentParser(description='配置家庭电视控制台')
    parser.add_argument('--tv-host', help='电视局域网 IPv4 地址')
    parser.add_argument('--media-host', help='电视能够访问的本机 IPv4 地址')
    parser.add_argument('--tv-port', type=int, default=5555, help='电视 ADB 连接端口')
    parser.add_argument('--port', type=int, default=17880, help='控制台与媒体服务端口')
    parser.add_argument('--library', help='本地片库目录')
    args = parser.parse_args()
    tv = args.tv_host or input('电视局域网 IPv4 地址：').strip()
    host = args.media_host
    if not host:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((tv, args.tv_port))
            detected = sock.getsockname()[0]
        host = input(f'本机局域网 IPv4 地址 [{detected}]：').strip() or detected
    user = input('Kodi Web 服务用户名 [tvconsole]：').strip() or 'tvconsole'
    password = getpass.getpass('Kodi Web 服务密码（与电视设置一致，不回显）：')
    config = create_config(tv, host, password, library=args.library, kodi_user=user,
                           tv_adb_port=args.tv_port, port=args.port)
    target = write_config(config)
    print(f'配置已保存：{target}')
    print(f'授权 ADB 后运行 python run_console.py，再打开 http://127.0.0.1:{args.port}/')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, FileExistsError, OSError) as exc:
        raise SystemExit(str(exc))
