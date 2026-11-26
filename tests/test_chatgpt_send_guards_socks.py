# -*- coding: utf-8 -*-
"""issue #136：chatgpt_send.py 代理 / SOCKS5 失败路径守卫的定点回归。

每个用例只对应一条显式 `raise`：本地 fake 代理（127.0.0.1 随机端口）返回
畸形握手字节或直接断开，断言精确异常类型 + 消息片段。全程离线，不触外网。
"""

import importlib.util
import os
import socket
import struct
import threading
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.environ.get("CHATGPT_SEND_PY") or os.path.join(
    HERE, "..", "scripts", "chatgpt_send.py")


def load_module():
    spec = importlib.util.spec_from_file_location("chatgpt_send_guards_socks", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = load_module()


class _StubProxy(object):
    """只接一次连接的本地 TCP 服务；handler(conn) 决定回什么字节。"""

    def __init__(self, handler):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self._handler = handler
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        conn = None
        try:
            conn, _ = self.srv.accept()
            self._handler(conn)
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
            try:
                self.srv.close()
            except OSError:
                pass

    def close(self):
        try:
            self.srv.close()
        except OSError:
            pass


def _eat_greeting(conn):
    """读掉 SOCKS5 问候（VER + NMETHODS + METHODS），避免服务端 RST。"""
    head = conn.recv(2)
    if len(head) == 2:
        conn.recv(head[1])


def _close_gracefully(conn):
    """先半关写端再关闭：Windows 上「带未读数据直接 close」会变成 RST，
    对端看到 ConnectionResetError 而不是干净的 EOF。"""
    try:
        conn.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def _call_with_deadline(case, fn, timeout=5.0):
    """在带截止时间的线程里调用：EOF 守卫若缺失会变成忙等死循环，
    没有这条断言，测试会挂住而不是失败。"""
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:            # 原样带回主线程再断言
            box["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    case.assertFalse(t.is_alive(),
                     "调用未在 %.1fs 内返回（疑似 EOF 守卫缺失导致忙等）" % timeout)
    if "error" in box:
        raise box["error"]
    return box.get("value")


class TestHttpConnectGuards(unittest.TestCase):
    """_proxy_alive 的 HTTP CONNECT 两处失败出口。"""

    def test_connect_stage_peer_close_is_reported(self):
        # 3673：接受连接后一个字节不回就关——必须判死且给出原因
        def handler(conn):
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            _close_gracefully(conn)

        stub = _StubProxy(handler)
        try:
            ok, why = _call_with_deadline(
                self,
                lambda: m._proxy_alive("http://127.0.0.1:%d" % stub.port, timeout=2))
        finally:
            stub.close()
        self.assertFalse(ok)
        self.assertIn("代理在 CONNECT 阶段直接断开", why)

    def test_connect_rejected_by_proxy_is_reported(self):
        # 3677：CONNECT 返回非 200——状态行要原样带进原因
        def handler(conn):
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")

        stub = _StubProxy(handler)
        try:
            ok, why = m._proxy_alive("http://127.0.0.1:%d" % stub.port, timeout=2)
        finally:
            stub.close()
        self.assertFalse(ok)
        self.assertIn("代理拒绝 CONNECT：HTTP/1.1 403 Forbidden", why)


class SocksGuardBase(unittest.TestCase):
    def _cfg(self, port, user="", password="", rdns=True):
        return {"host": "127.0.0.1", "port": port, "user": user,
                "password": password, "rdns": rdns}

    def _open(self, stub, user="", password="", timeout=2):
        return m._socks5_open(self._cfg(stub.port, user, password),
                              "example.com", 443, timeout)


class TestSocksHandshakeGuards(SocksGuardBase):
    """_socks5_open 握手阶段逐条失败出口。"""

    def test_peer_close_while_reading_greeting(self):
        # 3799：_recv_exact 读不满就 EOF
        def handler(conn):
            _eat_greeting(conn)
            _close_gracefully(conn)

        stub = _StubProxy(handler)
        try:
            with self.assertRaisesRegex(OSError, "SOCKS5 连接被对端关闭"):
                _call_with_deadline(self, lambda: self._open(stub))
        finally:
            stub.close()

    def test_bad_version_byte(self):
        # 3830：服务端回 VER=4，不是 SOCKS5
        def handler(conn):
            _eat_greeting(conn)
            conn.sendall(b"\x04\x00")

        stub = _StubProxy(handler)
        try:
            with self.assertRaisesRegex(OSError, "版本字节 4"):
                self._open(stub)
        finally:
            stub.close()

    def test_auth_required_but_credentials_missing(self):
        # 3833：服务端选 0x02，但配置里没有用户名
        def handler(conn):
            _eat_greeting(conn)
            conn.sendall(b"\x05\x02")

        stub = _StubProxy(handler)
        try:
            with self.assertRaisesRegex(OSError, "要求用户名密码，但地址里没带"):
                self._open(stub)
        finally:
            stub.close()

    def test_username_password_rejected(self):
        # 3839：RFC1929 子协商 status != 0
        def handler(conn):
            _eat_greeting(conn)
            conn.sendall(b"\x05\x02")
            ver = conn.recv(1)                  # 0x01
            if not ver:
                return
            ulen = conn.recv(1)[0]
            conn.recv(ulen)
            plen = conn.recv(1)[0]
            conn.recv(plen)
            conn.sendall(b"\x01\x01")           # 认证失败

        stub = _StubProxy(handler)
        try:
            with self.assertRaisesRegex(OSError, "SOCKS5 认证失败（status=1）"):
                self._open(stub, user="u1", password="p1")
        finally:
            stub.close()

    def test_no_acceptable_auth_method(self):
        # 3841：服务端回 0xFF，没有任何双方都支持的方法
        def handler(conn):
            _eat_greeting(conn)
            conn.sendall(b"\x05\xff")

        stub = _StubProxy(handler)
        try:
            with self.assertRaisesRegex(OSError, "没有可用的认证方式（method=255）"):
                self._open(stub)
        finally:
            stub.close()

    def test_connect_request_refused(self):
        # 3853：CONNECT 回包 REP != 0
        def handler(conn):
            _eat_greeting(conn)
            conn.sendall(b"\x05\x00")
            req = conn.recv(4)                  # VER CMD RSV ATYP
            if len(req) != 4:
                return
            if req[3] == 3:
                n = conn.recv(1)[0]
                conn.recv(n)
            elif req[3] == 1:
                conn.recv(4)
            conn.recv(2)                        # DST.PORT
            conn.sendall(b"\x05\x05\x00\x01" + socket.inet_aton("0.0.0.0")
                         + struct.pack(">H", 0))

        stub = _StubProxy(handler)
        try:
            with self.assertRaisesRegex(OSError, "SOCKS5 CONNECT 被拒（code=5"):
                self._open(stub)
        finally:
            stub.close()

    def test_transport_failure_reraised_after_close(self):
        # 3867：except 里必须先关 socket，再把原异常原样抛出去
        class _BoomSock(object):
            def __init__(self):
                self.closed = False

            def settimeout(self, _timeout):
                pass

            def sendall(self, _data):
                pass

            def recv(self, _n):
                raise RuntimeError("boom-from-socks")

            def close(self):
                self.closed = True

        sock = _BoomSock()
        fake_socket = types.SimpleNamespace(
            create_connection=lambda *a, **k: sock,
            OSError=OSError,
            inet_aton=socket.inet_aton,
        )
        with mock.patch.object(m, "socket", fake_socket):
            with self.assertRaisesRegex(RuntimeError, "boom-from-socks"):
                m._socks5_open(self._cfg(1080), "example.com", 443, 1)
        self.assertTrue(sock.closed, "失败路径必须先关闭已建立的 socket 再抛")


class TestSocksPortGuards(unittest.TestCase):
    """端口字段收敛：校验发生在任何 socket 操作之前，直接调用即可。"""

    def test_illegal_target_port(self):
        # 3814：目标端口不是 1-65535 的整数
        cfg = {"host": "127.0.0.1", "port": 1080, "user": "",
               "password": "", "rdns": True}
        for port in (0, -1, 65536, 99999, "abc", None, [1], float("inf"), True):
            with self.subTest(port=port):
                with self.assertRaisesRegex(OSError, "目标端口非法"):
                    m._socks5_open(cfg, "example.com", port, 0.2)

    def test_illegal_proxy_port(self):
        # 3817：代理自身端口非法
        for port in (0, -1, 65536, 99999, "abc", None, float("inf"), True):
            cfg = {"host": "127.0.0.1", "port": port, "user": "",
                   "password": "", "rdns": True}
            with self.subTest(port=port):
                with self.assertRaisesRegex(OSError, "SOCKS 代理端口非法"):
                    m._socks5_open(cfg, "example.com", 443, 0.2)


if __name__ == "__main__":
    unittest.main()
