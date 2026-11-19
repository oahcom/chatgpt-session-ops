# 证据矩阵（每条基线由什么证明）

`baselines.md` 的 49 条判定口径，逐条标注**证据类型与载体**。本文件不是荣誉榜，是**反夸大声明**：凡是写「可判定」的，必须说得清是哪一行代码、哪一条用例、哪一次实测；说不清的，就老实写「人判」，不许用「已经在文档里规定了」冒充机制。

**四种证据类型**（多选题，一行可并列）：

| 类型 | 含义 | 校验方式 |
|---|---|---|
| `test:` | 离线回归里有对应用例 | 用例名必须存在于 `tests/test_hardening.py`，否则矩阵校验失败 |
| `code:` | 脚本里有对应实现 | 符号必须能在 `scripts/*.py` 里找到 `def/class/赋值`，否则校验失败 |
| `live:` | 真机实测过（日期） | 只写日期；日期不得晚于今天 |
| `human:` | 只能人判（附理由） | 不给理由不算填写 |

**矩阵自身受回归保护**：`tests/test_hardening.py::TestEvidenceMatrix` 会校验「条款集合 = 矩阵行集合」「每个 `test:`/`code:` 引用都能解析」「每条至少一种证据」。也就是说，新增条款而不补证据行、或引用了不存在的用例，测试会直接红。

> 最后更新：2026-11-19（v1.8）。当前回归 139 条，全绿。

---

## A. 操控基线

| 条款 | 类型 | 证据 | 说明 |
|---|---|---|---|
| A0 | 人判 | `human:清单是操作序列，各项证据分别挂在 A26/A7/A5/A8/E` | 30 秒清单本身不产生新判定，只是把已有条款串成开工顺序 |
| A1 | 测试+代码 | `test:test_between_extracts_exact_bytes_and_offsets` `test:test_only_blank_lines_are_trimmed` `code:extract_deliverable` `code:_sha256_file` | CRLF/尾随空格逐字节比对；提取只允许裁首尾空行 |
| A2 | 测试+代码 | `test:test_fence_picks_nth_not_longest` `test:test_duplicate_marker_rejected` `test:test_locator_is_mandatory` `code:extract_deliverable` | 最长块是干扰项，第 2 块才是交付；定位器不给就退 2 |
| A3 | 测试+代码 | `test:test_post_payload_carries_conversation_id_only_when_given` `test:test_new_chat_has_no_parent` `code:send_prompt` | 请求体级别断言：`--new` 时 body 里没有该字段，既有会话必须带 |
| A4 | 测试+代码 | `test:test_verify_copy_detects_single_byte_change` `test:test_cli_exit_codes` `code:verify_copy` | 大小写差一个字节即判不一致并退 2 |
| A5 | 测试+代码 | `test:test_fallback_requires_all_login_429` `test:test_rotation_plan_puts_unavailable_last` `code:rotation_plan` `code:anon_fallback_block_reason` | 两账号都 429 才允许匿名；匿名计数不推主游标 |
| A6 | 测试+代码 | `test:test_fallback_requires_all_login_429` `test:test_fallback_blocked_when_disabled_by_env` `code:anon_fallback_block_reason` | 兜底判定只读 429 / nologin 冷却两个输入 |
| A7 | 代码+实测+人判 | `code:conv_owner` `live:2026-11-19` `human:显式账号与登记冲突退 2 的分支只走了代码路径，没有独立回归` | 已知缺口：本条缺一条「冲突即退 2」的用例 |
| A8 | 测试+代码 | `test:test_device_summary_counts_bound_sessions` `test:test_jar_is_persisted_and_header_is_readable` `code:list_device_ids` `code:device_summary` | 设备快照双文件（txt + json）同时存在才算 known |
| A9 | 代码+人判 | `code:_attach` `code:find_tab` `human:targetId+oai-did+conv_id 三要素校验在 UI 通道内，离线只能验标签页选择` | 可写窗口身份绑定靠真机验证 |
| A10 | 测试+代码 | `test:test_happy_path_keeps_conversation_id` `code:send_prompt` | 发送后 SSE 首包 id 必须与目标一致 |
| A11 | 测试+代码 | `test:test_missing_conversation_id_is_failure_not_silent_success` `test:test_invalid_conversation_id_is_rejected` `test:test_happy_path_keeps_conversation_id` `code:create_chat_rotating` | 200 后不再换身份；解不出 id 或 id 不合规 → 返回 1、不落盘、不重投 |
| A12 | 测试+代码 | `test:test_reply_digest_reports_parts_and_sha` `test:test_tail_from_mapping_picks_newest` `code:reply_digest` `code:tail_from_mapping` | 截断补全文时按 part 数 + 拼接 SHA-256 对账，不只看「变长了」 |
| A13 | 测试+代码 | `test:test_full_text_is_kept` `test:test_optional_truncation_is_labelled` `code:save_reply` | 默认全文；截断必须写进文件头 |
| A14 | 测试+代码 | `test:test_jar_is_persisted_and_header_is_readable` `test:test_auth_cookies_are_never_written_to_disk` `code:save_device_cookie` | 发前落身份，auth cookie 永不落盘 |
| A15 | 代码+人判 | `code:_file_lock` `human:账号级串行是调用点约定（同一账号不并发起两个 GET），离线只能验锁本身，验不了「谁在调用」` | 已知缺口：缺一个「同账号并发调用被拒」的用例 |
| A16 | 代码+人判 | `code:_idle` `human:超时预算由调用方按思考/非思考传入，没有统一常量可断言` | 分档超时是参数约定，不是硬编码 |
| A17 | 测试+代码 | `test:test_retry_after_is_clamped` `test:test_retry_after_prefers_body_rate_limit` `code:_retry_after` | Retry-After 当下限；body 里的 resets_in 优先 |
| A18 | 代码+人判 | `code:classify_failure` `code:warm_cookies` `human:401/403/429 各自冷却时长由调用方按定性结果落地，无集中断言` | 403 补 cookie + PoW 单次重试有实现，冷却值是调用点约定 |
| A19 | 测试+代码 | `test:test_rejects_traversal_and_junk` `test:test_malicious_conv_id_cannot_escape` `code:safe_id` | id 白名单 + 归档路径不逃逸 |
| A20 | 测试+代码 | `test:test_atomic_write_leaves_no_tmp` `test:test_atomic_write_cleans_tmp_on_failure` `code:_atomic_write` | tmp 唯一名 + fsync + replace |
| A21 | 测试+代码 | `test:test_corrupt_json_is_quarantined_not_silently_lost` `test:test_empty_file_is_not_quarantined` `code:_load_json` | 损坏即隔离为 `.corrupt-<ts>`，空文件不误伤 |
| A22 | 测试+代码 | `test:test_lock_is_reentrant` `test:test_stale_lock_is_reclaimed` `test:test_lock_live_holder_not_reclaimed` `test:test_lock_pid_reused_reclaimed` `test:test_lock_unknown_identity_kept` `test:test_lock_legacy_format_uses_mtime` `test:test_lock_records_holder_identity` `code:_lock_holder_stale` | 线程重入 + pid/启动时间/租约三态回收 |
| A23 | 测试+代码+实测 | `test:test_webrtc_leak_detected` `test:test_webrtc_via_proxy` `test:test_webrtc_no_candidates_is_clean` `code:webrtc_probe` `code:_classify_webrtc` `live:2026-11-19` | 一次性标签页跑真实 ICE，`leaked` 退 3 |
| A24 | 测试+代码 | `test:test_fail_closed_when_nothing_configured` `test:test_allow_direct_is_explicit_only` `code:require_proxy` | 解析不到出口就 SystemExit(3)，直连需显式放行 |
| A25 | 测试+代码 | `test:test_dead_proxy_detected_even_if_port_accepts` `test:test_socks5_handshake_reaches_target` `code:_proxy_alive` `code:_socks5_open` | 端口在听 ≠ 能用；要过 TLS 握手 |
| A26 | 测试+代码 | `test:test_probe_site_ok` `test:test_probe_site_blocked_by_vpn` `test:test_probe_site_challenge_is_usable` `test:test_probe_site_other_status_not_ok` `test:test_proxy_check_requires_site_ok` `code:probe_site` | 目标站 verdict 五分类，只有 ok/challenge 算通 |
| A27 | 测试+代码+实测 | `test:test_thinking_uses_thinking_slug` `test:test_default_keeps_auto_model` `code:resolve_thinking_model` `code:enable_thinking` `live:2026-11-19` | `-t-` 变体解析不出就退 1；浏览器侧幂等开按钮 |
| A28 | 测试+代码 | `test:test_truncated_stream_completed_by_get` `code:send` | `IncompleteRead.partial` 按 200 处理 + GET 补全文 |

## B. 定时任务基线

| 条款 | 类型 | 证据 | 说明 |
|---|---|---|---|
| B0 | 人判 | `human:六问是建任务前的自检，逐项落到 B1–B15` | 不自证，只是入口 |
| B1 | 测试+代码 | `test:test_hourly_rrule_ok` `test:test_non_hourly_rrule_rejected` `test:test_interval_not_one_rejected` `code:check_schedule` | 只认 HOURLY + INTERVAL=1 |
| B2 | 人判 | `human:三态状态机写在 automation prompt 与各任务状态文件里，没有共享库可断言` | 已知缺口：三态靠 prompt 自包含 |
| B3 | 人判 | `human:窗口左闭右开由调度器解释，本地只能校验时间串格式` | 已知缺口 |
| B4 | 测试+代码 | `test:test_oneshot_within_hour_ok` `test:test_oneshot_in_the_past_rejected` `test:test_oneshot_without_timezone_rejected` `test:test_oneshot_beyond_one_hour_rejected` `code:check_oneshot` | 已过去 / 无时区 / 超 1 小时 三头全堵 |
| B5 | 测试+代码+实测+人判 | `test:test_oneshot_in_the_past_rejected` `code:check_oneshot` `live:2026-11-19` `human:已 ACTIVE 的过期任务要靠接口盘点，本地只能判「不该建」` | 本次盘点出 4 个过期 ACTIVE，已归档 |
| B6 | 人判 | `human:路径类型校验写在 prompt 的 preflight 段，没有本地实现` | 已知缺口 |
| B7 | 人判 | `human:step_id/attempt_count 状态机由各任务自己的状态文件落地，无共享实现` | 已知缺口 |
| B8 | 测试+代码 | `test:test_slot_cap_sequential` `test:test_slot_cap_under_process_race` `test:test_acquire_slot_fails_closed_on_lock_timeout` `test:test_fresh_empty_slot_file_is_not_reclaimed` `code:acquire_slot` `code:release_slot` | 抢不到锁即 QUEUED，空槽 5s 内不当死槽 |
| B9 | 人判 | `human:PAUSED→等实例结束→删除的顺序只在操作时执行，无脚本` | 已知缺口 |
| B10 | 测试+代码 | `test:test_readonly_name_ok` `test:test_write_name_ok` `test:test_four_segments_rejected` `test:test_verb_must_match_whole_segment` `test:test_readonly_suffix_with_write_verb_rejected` `code:check_automation_name` | 分词判段，动作词整段相等 |
| B11 | 测试+代码 | `test:test_readonly_argv_whitelist_is_exact` `code:READONLY_ARGV` | `--rotation-reset` 不再被前缀匹配放行 |
| B12 | 测试+代码+人判 | `test:test_cli_exit_codes` `test:test_proxy_check_requires_site_ok` `code:main` `human:exit 0/1/2/3 的语义分散在各分支，只能逐分支看；无事静默靠 stdout 行数人眼确认` | 0/1/2/3 各有用例，但不是集中式断言 |
| B13 | 人判 | `human:attempt_count 与 MANUAL_REVIEW 在各任务状态文件里，无共享库` | 已知缺口 |
| B14 | 人判 | `human:红线本身不含新判定，约束由 B8/A15/A17/A5/A23 各自实现` | 交叉引用型条款 |
| B15 | 测试+代码 | `test:test_lease_valid_blocks_reclaim` `test:test_lease_expired_process_gone` `test:test_lease_pid_reused` `test:test_heartbeat_keeps_lease_alive` `test:test_lease_run_timeout_cancels_body` `test:test_lease_run_timeout_orphan_keeps_lease` `code:lease_run` | 心跳续期 + 超时两档（TIMEOUT / TIMEOUT_ORPHAN） |

## C. 配方

| 条款 | 类型 | 证据 | 说明 |
|---|---|---|---|
| C1 | 人判 | `human:配方是 prompt 骨架，约束来自 B1/B12/B13 与 exit 码` | 模板不产生新判定 |
| C2 | 人判 | `human:配方是 prompt 骨架，判定依赖 B12 的 exit 码语义` | 同上 |
| C3 | 人判 | `human:配方是 prompt 骨架，窗口与三态由 B2/B3 约束` | 同上 |
| C4 | 人判 | `human:配方是 prompt 骨架，只读白名单由 B11 约束` | 同上 |

---

## 统计（2026-11-19，由本节自动对账得出）

| 组合 | 条数 | 条款 |
|---|---|---|
| 用例 + 代码 | 28 | A1 A2 A3 A4 A5 A6 A8 A10 A11 A12 A13 A14 A15 A17 A19 A20 A21 A22 A24 A25 A26 A28 B1 B4 B8 B10 B11 B15 |
| 用例 + 代码 + 实测 | 2 | A23 A27 |
| 用例 + 代码 + 实测 + 人判 | 1 | B5 |
| 用例 + 代码 + 人判 | 1 | B12 |
| 代码 + 实测 + 人判（缺回归） | 1 | A7 |
| 代码 + 人判（缺回归） | 3 | A9 A16 A18 |
| 纯人判 | 13 | A0 B0 B2 B3 B6 B7 B9 B13 B14 C1 C2 C3 C4 |
| 合计 | 49 | — |

**结论**：32 条有离线用例兜底（65%）；4 条只有代码没有回归；13 条纯人判。这三档必须分开说，不能笼统宣称「都已机械化」。

**已知缺口（按危害排序，不许糊过去）**：

1. **B2/B7 三态状态机没有共享实现**——每个任务各写一份，最容易在续跑时重复提交副作用。
2. **B6 路径类型校验只在 prompt 里**——工作目录写错时，报错发生在半小时后的业务步骤，而不是 preflight。
3. **A7 缺「显式账号与登记冲突退 2」的回归**——代码路径存在，但没有用例钉住。
4. **B9/B13/B5 的「已存在」判定依赖接口**——本地只能判「不该建」，不能判「该归档」。

这些缺口写在这里，是为了下次审查时不被当成「已完成」；补一条就从纯人判移到测试列。
