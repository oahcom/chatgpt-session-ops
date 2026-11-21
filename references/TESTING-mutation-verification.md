# 安全测试方法论 — 变异验证与「假绿」

- **建立日期**：2026-11-21
- **来源**：DECISION-156（Cookie 持久化边界）执行过程中的真实翻车
- **裁定**：ChatGPT `157`（`6b017d52-8a3c-83ec-8571-eb99be26e6f5`，acct2）定为**长期工程要求**
- **适用范围**：本技能全部回归测试，以及后续任何 Adapter / 项目治理工作的测试质量要求

---

## 1. 一句话

**一条没做过变异验证的安全测试，等于没有测试。**

它可能跑通了、是绿的、覆盖了那行代码——但断言从来没被执行到，或者执行到了却
断言了一个恒真的东西。绿灯不代表守住了边界，只代表「没报错」。

---

## 2. 事故复盘：一个假绿的回归测试

### 2.1 想测什么

`warm_cookies()` 回写磁盘前必须过滤掉非 OpenAI 域 cookie（DECISION-156）。
真实事故：acct1 的 `cookies.json` 混进 50 条 Google 会话，每次补 CF cookie
都把整包回写磁盘。

### 2.2 第一版怎么写的（错的）

```python
class _NullOpener:
    """warm_cookies 里 opener.open(...) 的替身：不触网，返回空响应。"""
    def __init__(self, cookies=()):
        self._cookies = list(cookies)      # ← 这个参数从头到尾没被用过
    def open(self, req, timeout=None):
        ...
```

```python
with mock.patch.object(m, "build_opener_for",
                       return_value=_NullOpener([_FakeCookie()])):
    api.warm_cookies()
```

### 2.3 为什么它是假的

`warm_cookies()` 内部自己 `import http.cookiejar` 并 `jar = http.cookiejar.CookieJar()`，
然后通过 `extra=[HTTPCookieProcessor(jar)]` 交给 `build_opener_for`。
我的 `_NullOpener.__init__(cookies=...)` 收到的 cookie **没有写进那个 jar**。

于是 `jar` 里只有原有 cookie，`changed` 为空：

```python
if not changed:
    return False          # ← 在这里就返回了
```

**回写分支压根没执行**，过滤代码一次都没跑。测试是绿的，因为它断言的是
「`api.cookies` 长度是 4」——而 `api.cookies` 本来就是 `foreign + keep` 的 54 条，
等等，那它应该红啊？

——是的，**第一版其实是红的**。但红的原因不是它抓到了 bug，而是我 mock 的方式
不对导致 `NameError: name 'mock' is not defined`（没 import）。修完 import 之后
它变成了另一种红，我才开始怀疑测试本身。如果当时恰好改对了 mock 让它变绿，
我就会以为自己守住了边界。

### 2.4 怎么发现它是假的

**变异验证（mutation testing）**：把修复代码回滚，看测试会不会红。

```bash
# 把 warm_cookies 里的过滤三行删掉，模拟修复前的代码
python - <<'EOF'
s = open("scripts/chatgpt_send.py").read()
s = s.replace("""        before = len(self.cookies)
        self.cookies = _filter_openai_cookies(self.cookies)
        dropped = before - len(self.cookies)""",
              """        before = len(self.cookies)
        dropped = 0""")
open("scripts/_mut_send.py", "w").write(s)
EOF

CHATGPT_SEND_PY="scripts/_mut_send.py" python -m pytest tests -q -k "warm_cookies_drops"
```

第一版测试的结果：**还是绿的**（因为回写分支没执行，过滤删不删都一样）。
修好 `_NullOpener`（让它真的往 jar 里塞 cookie）之后再跑变异：

```
AssertionError: 54 != 4 : 内存里不该留 Google cookie
```

这才证明测试真的守住了那三行代码。

---

## 3. 五步法（ChatGPT 157 定稿）

安全类回归测试按这个顺序写，**缺任何一步都不算完成**：

| # | 步骤 | 说明 |
|---|---|---|
| 1 | **正常路径验证** | 合法输入不被误伤。过滤类改动的最大风险是过滤太狠 |
| 2 | **恶意输入验证** | 攻击者会构造什么：仿冒域、路径穿越、超长串、类型污染 |
| 3 | **边界输入验证** | 空串 / `None` / 纯空格 / 单点 `.` / 大小写 / 前后空白 |
| 4 | **修复回滚后的变异验证** | 把修复删掉，测试必须红。**不红说明测试是假的** |
| 5 | **确认实际执行了目标代码分支** | 用 print / 计数 / 断点确认那行真的跑了，不是被提前 return 绕过 |

第 4、5 步是这次的血泪。第 1–3 步只证明「我想到的攻击面都覆盖了」，
第 4 步才证明「我的断言真的连着那行代码」。

---

## 4. 本轮四条用例的变异验证记录

| 用例 | 回滚什么 | 期望 | 实际 |
|---|---|---|---|
| `test_warm_cookies_drops_foreign_cookies_on_writeback` | `warm_cookies()` 的 `_filter_openai_cookies` 调用 | 红 | `AssertionError: 54 != 4` ✅ |
| `test_cookie_header_built_at_init_excludes_foreign_domains` | `__init__` 的域判定改回字符串包含 | 红 | `'oai-did=d; __cf_bm=c; evil=x; evil2=y' != 'oai-did=d; __cf_bm=c'` ✅ |
| `test_extract_auth_shares_the_same_domain_semantics` | `extract_auth.py` 改回字符串包含 | 红 | `True is not false : extract_auth 应拒绝 'notchatgpt.com'` ✅ |
| `test_lookalike_domains_rejected` | 域判定改回字符串包含 | 红 | 仿冒域全部被放行 ✅ |

**注意第二条的实际输出**：`evil=x; evil2=y` 出现在出站请求头里。
这不是「过滤没生效」这种软问题，是**仿冒域的 cookie 被真的发给了 chatgpt.com**。
变异验证让这个从「代码审查时的假设」变成了「可复现的安全缺陷」。

---

## 5. 反面模式清单（都是这次踩过的）

| 反面模式 | 症状 | 正确做法 |
|---|---|---|
| **mock 参数没人用** | `_NullOpener(cookies=...)` 收下就扔，被测代码自己 new 了对象 | mock 必须注入到**被测代码实际使用的那个对象** |
| **断言恒真** | `assertIsNotNone(result)`，而 result 在任何分支都非 None | 断言具体值，且该值在缺陷存在时会变 |
| **测试自己抛异常也算「红」** | `NameError` / `ImportError` 让测试红，误以为是抓到了 bug | 先让测试在**修复后**变绿，再做变异验证 |
| **只测纯函数** | `_filter_openai_cookies()` 单测全绿，但 `warm_cookies()` 没调用它 | 必须有端到端路径用例（第 4 步） |
| **同语义多实现只测一份** | `extract_auth.py` 有独立实现，只测主脚本那份 | 同一组向量喂给两份（见 §6） |

---

## 6. 两份实现的漂移检测

`extract_auth.py` 故意不 import 主脚本（避免脚本互相依赖），于是
`_cookie_domain_allowed()` 存在两份实现。

**最危险的失败模式不是一起错，而是漂移**：有人只改一处，边界就在另一条路径上裂开。

```python
def test_extract_auth_shares_the_same_domain_semantics(self):
    allow = ("chatgpt.com", ".chatgpt.com", "ws.chatgpt.com",
             "openai.com", ".openai.com", "auth.openai.com", "CHATGPT.COM")
    deny = ("notchatgpt.com", "evil-openai.com", "chatgpt.com.evil.io",
            "openai.com.evil.io", "chatgpt.co", "myopenai.com",
            "google.com", ".google.com", "accounts.google.com",
            ".youtube.com", ".oaistatic.com", "", None, "  ", ".")

    for d in allow:
        self.assertTrue(m._cookie_domain_allowed(d))
        self.assertTrue(ea._cookie_domain_allowed(d))
    for d in deny:
        self.assertFalse(m._cookie_domain_allowed(d))
        self.assertFalse(ea._cookie_domain_allowed(d))
```

变异验证：把 `extract_auth.py` 改回字符串包含后，立刻报
`extract_auth 应拒绝 'notchatgpt.com'`。

---

## 7. 推广：这不是只适用于本技能

任何「安全边界」类改动都适用同一套：

- **输入校验**（id 白名单、域名白名单、路径穿越）
- **凭据处理**（哪些能存、哪些能发）
- **权限判定**（谁能读、谁能写）
- **fail-closed 逻辑**（解析不到就拒绝，而不是降级放行）

判断标准很简单：**问自己「如果我把这段修复删掉，哪条测试会红？」**
答不上来，就说明这段修复没有测试保护。

---

## 8. 与既有文档的关系

- 测试的**写测试硬要求**见 `baselines.md` §「写测试的硬要求」
- 每条基线的证据载体见 `evidence-matrix.md`（本文是方法，矩阵是台账）
- Cookie 域边界的**具体实现**见 `SKILL.md` 加固表「Cookie 持久化边界」行
- 本文只讲**怎么证明测试有效**，不重复实现细节
