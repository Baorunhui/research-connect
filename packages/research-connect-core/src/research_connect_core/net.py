"""DNS order shared by the server processes.

Some model hosts advertise an IPv6 address that does not accept connections.
Python then waits out that attempt before trying IPv4. Prefer IPv4 so a paper
summary or a Xiaohongshu draft does not sit idle on every request.
"""

from __future__ import annotations

import socket

IPV4_BOOTSTRAP = """
import os, runpy, socket, sys
if not getattr(socket, "_rc_ipv4_first", False):
    _orig = socket.getaddrinfo
    def _ipv4_first(host, *args, **kwargs):
        return sorted(_orig(host, *args, **kwargs), key=lambda item: 0 if item[0] == socket.AF_INET else 1)
    socket.getaddrinfo = _ipv4_first
    socket._rc_ipv4_first = True
script = sys.argv[1]
sys.argv = sys.argv[1:]
script_dir = os.path.dirname(os.path.abspath(script))
if script_dir and script_dir not in sys.path:
    sys.path.insert(0, script_dir)
runpy.run_path(script, run_name="__main__")
""".strip()


def prefer_ipv4() -> None:
    if getattr(socket, "_rc_ipv4_first", False):
        return
    original = socket.getaddrinfo

    def _ipv4_first(host, *args, **kwargs):
        return sorted(
            original(host, *args, **kwargs),
            key=lambda item: 0 if item[0] == socket.AF_INET else 1,
        )

    socket.getaddrinfo = _ipv4_first
    socket._rc_ipv4_first = True


def command_with_ipv4(args: list[str]) -> list[str]:
    """Run a Python script in a child process that also prefers IPv4."""
    if len(args) < 2:
        return list(args)
    exe = str(args[0]).rsplit("/", 1)[-1].lower()
    if not exe.startswith("python") or not str(args[1]).endswith(".py"):
        return list(args)
    return [args[0], "-c", IPV4_BOOTSTRAP, *args[1:]]
