# Flake 日志

记录测试套件里「非确定性失败」的排查结论与处置。原则：**先证明是不是 flake，再改断言；
改断言必须保留原本要抓的缺陷意图**，不能把病理性变慢一起放过。

## 2026-11-27：TestSlotsAndLease / TestStateFiles 时序调查（issue #145）

### 复现结果

基线 `d03989fe` 上共 84 轮，**0 失败**：

| 轮次类型 | 次数 |
| --- | --- |
| 单类（`TestSlotsAndLease`） | 50 |
| 负载下（并发跑测试/编译） | 20 |
| 整文件（`tests/test_hardening.py`） | 10 |
| 4 路并发整文件 | 4 |

结论：没有稳定的产品缺陷复现路径；按「测试对时序过度敏感」处理。

平台差异：本机 Windows 11 + Python 3.13；CI 为 ubuntu + Python 3.11。
Windows 的文件共享/删除语义（WinError 5/32）是本地独有，CI 上反而更宽松。

### Top-3 时序嫌疑与处置

1. **租约释放断言过严**（`test_lease_run_done`、`test_lease_run_timeout_cancels_body`）
   - 产品在 `os.remove` 抛 OSError 时会把租约降级写成 `{}` 并保留文件（有意设计，
     避免 Windows 共享冲突造成永久死锁）。测试却断言文件必须不存在。
   - 处置：新增 `assert_lease_released()`，接受「不存在 / 空租约 `{}`」两种终态；
     非空内容仍失败并打印原文。新增自测把租约文件删除打挂，验证降级路径仍返回
     `(42, "DONE")` 且 helper 不误红。

2. **TestStateFiles 墙钟上界低于内部重试预算**
   - `_replace_with_bounded_retry` 的退避预算是
     0.05+0.10+0.20+0.40+0.80+1.00 = **2.55s**；原先 1.0s/3s 的断言比预算还紧，
     一次瞬时 WinError 32/5 就可能误红。
   - 处置：锁路径的时间断言统一改为 10s「挂死保护」，只抓真的等满
     `LOCK_TIMEOUT_SEC` 或死锁的病理性变慢，不再承担毫秒级 SLA。

3. **`test_device_list_is_newest_first` 依赖 50ms mtime 间隔**
   - 用例靠 `time.sleep(0.05)` 制造两个设备文件的 mtime 差；文件系统时间戳精度
     或负载下的调度抖动都可能让两次写落在同一刻度。
   - 现状：本轮 84 轮未复现，暂不改动；若再次报告，优先改为显式 `os.utime`
     固定 mtime，而不是继续加大 sleep。

### 本轮额外修掉的时序点

- `test_heartbeat_keeps_lease_alive` 在心跳 `os.replace` 期间直接 `open()` 租约文件，
  Windows 的「删除挂起」窗口会抛 `PermissionError`。已改为 20×10ms 有界重试读取；
  读的仍是同一份瞬时文件，不改变用例意图。
- 子进程抢槽用例的 `subprocess.Popen` 改为 3 次有界重试（冷启动/AV 下可能抛
  OSError），全部失败时 `self.fail` 并带最后一次错误；`communicate(timeout=120)`
  之后在 `finally` 里 terminate/kill，保证进程回收。

### 后续要求

任何新的 flake 报告**必须附首轮失败时的完整 traceback**（含用例名、断言原文、
平台与 Python 版本）。只有「又红了」而没有 traceback 的报告无法区分产品缺陷、
环境问题和断言过紧，一律不按 flake 处理。
