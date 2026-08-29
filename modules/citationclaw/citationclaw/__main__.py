"""
CitationClaw v2 — 论文被引画像分析🦞

Usage:
    citationclaw          # start web server at http://127.0.0.1:8000
    citationclaw --port 8080
    citationclaw --no-browser
"""

import argparse
import socket
import sys
import threading
import time
import webbrowser
import urllib.request
import urllib.error


def _port_in_use(host: str, port: int) -> bool:
    """Return True if the port is already bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _wait_for_server(host: str, port: int, timeout: float = 15.0) -> bool:
    """Block until the server is accepting HTTP connections, or timeout."""
    deadline = time.monotonic() + timeout
    url = f"http://{host}:{port}/api/task/status"
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


def _prefer_ipv4():
    """Sort IPv4 addresses before IPv6 in DNS resolution.

    Some providers (e.g. Zhipu BigModel) advertise IPv6 AAAA records that do not
    actually accept connections from this host, causing Python's default
    ``socket.create_connection`` to wait out the IPv6 attempt (~10s) before
    falling back to IPv4. curl already prefers IPv4 here, so mirror that to keep
    the OpenAI SDK / httpx calls fast.
    """
    import socket as _socket
    _orig = _socket.getaddrinfo

    def _ipv4_first(host, *args, **kwargs):
        try:
            results = _orig(host, *args, **kwargs)
        except _socket.gaierror:
            return _orig(host, *args, **kwargs)
        return sorted(results, key=lambda r: 0 if r[0] == _socket.AF_INET else 1)

    _socket.getaddrinfo = _ipv4_first


def main():
    _prefer_ipv4()
    parser = argparse.ArgumentParser(
        prog="citationclaw",
        description="CitationClaw v2 — 论文被引画像分析🦞",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError as e:
        print("=" * 60)
        print("错误: 缺少依赖包!")
        print(f"详细信息: {e}")
        print("\n请先安装依赖:")
        print("  pip install citationclaw")
        print("=" * 60)
        sys.exit(1)

    # Check port availability before starting
    if _port_in_use(args.host, args.port):
        print(f"\n  错误: 端口 {args.port} 已被占用。")
        print(f"  请尝试使用其他端口:  citationclaw --port {args.port + 1}")
        print(f"  或先关闭占用该端口的程序。\n")
        sys.exit(1)

    print(f"\n  CitationClaw v2 🦞  →  http://{args.host}:{args.port}\n")

    if not args.no_browser:
        def _open_browser():
            if _wait_for_server(args.host, args.port, timeout=15.0):
                try:
                    webbrowser.open(f"http://{args.host}:{args.port}")
                except Exception:
                    pass
            else:
                print("  Warning: server did not become ready in time; skipping browser open.")
        threading.Thread(target=_open_browser, daemon=True).start()

    try:
        uvicorn.run(
            "citationclaw.app.main:app",
            host=args.host,
            port=args.port,
            log_level="warning",
        )
    except KeyboardInterrupt:
        print("\nCitationClaw v2 stopped.")


if __name__ == "__main__":
    main()
