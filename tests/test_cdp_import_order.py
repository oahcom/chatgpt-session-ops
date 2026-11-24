# -*- coding: utf-8 -*-
"""CDP 是可选依赖：没有它也必须能导入纯逻辑模块，且必须保留打桩点。

回归对象：extract_auth.py:21 / continue_full.py:28 曾在模块级无条件
`from cdp import CDPClient`，导致无浏览器环境下整个模块不可导入
（`--help` 都起不来，`judge_error()` 这类纯函数也用不了）。

验收判据（逐条对应）：
  1. 无 cdp 时两个模块可导入，且**导入过程根本不探测 cdp**；
  2. 无 cdp 时 `CDPClient` 属性存在（None），只有真正取客户端才抛 CdpUnavailable；
  3. 模块级 `CDPClient` 仍可被替换——tests/test_hardening.py 依赖此点打桩；
  4. 配好 CHATGPT_CDP_DIR 时能拿到真实的 CDPClient 类；
  5. cdp 存在但内部依赖损坏，不能被误报成「cdp 未安装」。

授权：第二轮裁决 Q3（仅此文件新增）。
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import textwrap
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
MODULES = ("extract_auth", "continue_full")
_CACHE = {}


class _CdpProbeBlocker(object):
    """把 `import cdp` 变成 ModuleNotFoundError，并记录被探测的模块名。"""

    def __init__(self):
        self.probes = []

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "cdp" or fullname.startswith("cdp."):
            self.probes.append(fullname)
            raise ModuleNotFoundError("No module named 'cdp'", name="cdp")
        return None


class _blocked(object):
    """上下文管理器：屏蔽 cdp 并暴露探测记录。"""

    def __enter__(self):
        self.saved = sys.modules.pop("cdp", None)
        self.blocker = _CdpProbeBlocker()
        sys.meta_path.insert(0, self.blocker)
        return self.blocker

    def __exit__(self, *exc):
        try:
            sys.meta_path.remove(self.blocker)
        except ValueError:
            pass
        sys.modules.pop("cdp", None)
        if self.saved is not None:
            sys.modules["cdp"] = self.saved
        return False


def _load(name, force=False):
    """按文件路径加载脚本模块；默认复用已加载实例以控制耗时。"""
    if name in _CACHE and not force:
        return _CACHE[name]
    modname = "p2a_under_test_" + name
    sys.modules.pop(modname, None)
    spec = importlib.util.spec_from_file_location(
        modname, os.path.join(SCRIPTS, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(modname, None)
        raise
    _CACHE[name] = mod
    return mod


class TestNoCdpAtImport(unittest.TestCase):

    def test_modules_import_without_touching_cdp(self):
        for name in MODULES:
            with _blocked() as blocker:
                mod = _load(name, force=True)
                self.assertEqual(
                    blocker.probes, [],
                    "%s 在导入阶段探测了 cdp（%r）——延迟加载没有做到"
                    % (name, blocker.probes))
                self.assertTrue(
                    hasattr(mod, "CDPClient"),
                    "%s 缺少模块级 CDPClient 替换点" % name)
                self.assertIsNone(
                    getattr(mod, "CDPClient"), "%s.CDPClient 默认应为 None" % name)

    def test_blocker_is_effective(self):
        """反向对照：拦截器真的生效，否则上面的断言只是空转。"""
        with _blocked():
            with self.assertRaises(ModuleNotFoundError):
                __import__("cdp")


class TestLazyFailureIsExplicit(unittest.TestCase):

    def test_client_request_raises_typed_error(self):
        for name in MODULES:
            mod = _load(name)
            with _blocked():
                with self.assertRaises(mod.CdpUnavailable) as ctx:
                    mod._cdp_client()
            msg = str(ctx.exception)
            self.assertIn("CDP_UNAVAILABLE", msg, name)
            self.assertIn("未执行任何浏览器操作", msg, name)
            self.assertIn("CHATGPT_CDP_DIR", msg, name)

    def test_cdp_unavailable_is_runtime_error(self):
        """调用方按 RuntimeError 捕获也必须成立（CLI 顶层已有该兜底）。"""
        for name in MODULES:
            self.assertTrue(issubclass(_load(name).CdpUnavailable, RuntimeError))

    def test_judge_error_is_usable_without_cdp(self):
        """纯逻辑不该被浏览器依赖拖下水。"""
        mod = _load("continue_full")
        has_error, structural, text_err = mod.judge_error("", "Something went wrong")
        self.assertEqual((has_error, structural, text_err), (True, True, False))


class TestPatchPointPreserved(unittest.TestCase):

    def test_module_level_override_bypasses_import(self):
        """tests/test_hardening.py 的用法：p1.CDPClient = lambda ...: fake。"""
        sentinel = object()
        for name in MODULES:
            mod = _load(name)
            mod.CDPClient = lambda *a, **k: sentinel
            try:
                with _blocked():
                    got = (mod.CDPClient or mod._cdp_client())(
                        host="127.0.0.1", port=9222, timeout=30)
                self.assertIs(got, sentinel, name)
            finally:
                mod.CDPClient = None


class TestCdpResolution(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p2a-cdp-")
        self._old_env = os.environ.get("CHATGPT_CDP_DIR")
        self._old_path = list(sys.path)

    def tearDown(self):
        sys.modules.pop("cdp", None)
        sys.path[:] = self._old_path
        if self._old_env is None:
            os.environ.pop("CHATGPT_CDP_DIR", None)
        else:
            os.environ["CHATGPT_CDP_DIR"] = self._old_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_cdp(self, body):
        with open(os.path.join(self.tmp, "cdp.py"), "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(body))
        os.environ["CHATGPT_CDP_DIR"] = self.tmp

    def test_env_override_path_is_honored(self):
        self._write_cdp("""
            class CDPClient(object):
                def __init__(self, **kwargs):
                    self.kwargs = kwargs
        """)
        for name in MODULES:
            mod = _load(name)
            cls = mod._cdp_client()
            self.assertEqual(cls.__name__, "CDPClient", name)
            inst = cls(host="127.0.0.1", port=9222, timeout=30)
            self.assertEqual(inst.kwargs["port"], 9222, name)

    def test_missing_transitive_dependency_is_distinguished(self):
        """cdp 在、但它的依赖缺失：必须报「内部依赖缺失」，不能报「未安装」。"""
        self._write_cdp("""
            import dependency_that_does_not_exist_xyz  # noqa: F401
            class CDPClient(object):
                pass
        """)
        mod = _load("extract_auth")
        with self.assertRaises(mod.CdpUnavailable) as ctx:
            mod._cdp_client()
        msg = str(ctx.exception)
        self.assertIn("内部依赖缺失", msg)
        self.assertNotIn("CDP_UNAVAILABLE", msg)


class TestCliSurface(unittest.TestCase):
    """CLI 入口契约：--help 不依赖浏览器，用法错误与依赖缺失退出码分离。"""

    def test_extract_auth_help_exits_zero(self):
        import subprocess
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "extract_auth.py"), "--help"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertIn("usage:", proc.stdout.decode("utf-8", "replace"))

    def test_continue_full_usage_guard_still_returns_2(self):
        import subprocess
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "continue_full.py")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("usage:", proc.stdout.decode("utf-8", "replace"))

    def test_extract_auth_bad_port_still_returns_2(self):
        """退出码契约：用法/参数错误仍是 2，不得被归入「缺依赖」的 3。"""
        import subprocess
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "extract_auth.py"),
             "--port", "abc"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--port", proc.stdout.decode("utf-8", "replace"))

    def test_continue_full_missing_prompt_returns_2(self):
        import subprocess
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "continue_full.py"),
             "9f8e7d6c-1111-4222-8333-444455556666",
             os.path.join(SCRIPTS, "no-such-prompt-file.txt")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("读不到提示词文件", proc.stdout.decode("utf-8", "replace"))

    def test_continue_full_invalid_conv_id_returns_2(self):
        import subprocess
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "continue_full.py"),
             "../../etc/passwd", os.path.join(SCRIPTS, "SKILL.md")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("非法", proc.stdout.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
