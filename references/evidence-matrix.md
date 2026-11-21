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

> 最后更新：2026-11-21（v1.9）。当前回归 313 条，全绿（1 条 POSIX-only skip）。

---

## A. 操控基线

| 条款 | 等级 | 类型 | 证据 | 说明 |
|---|---|---|---|---|
| A0 | **HUMAN** | 人判 | `human:清单是操作序列，各项证据分别挂在 A26/A7/A5/A8/E` | 30 秒清单本身不产生新判定，只是把已有条款串成开工顺序 |
| A1 | **FULL** | 测试+代码 | `test:test_byte_exact_matrix` `test:test_between_extracts_exact_bytes_and_offsets` `test:test_only_blank_lines_are_trimmed` `test:test_extract_deliverable_non_path_is_custodyerror` `code:extract_deliverable` `code:_sha256_file` | LF/CRLF/多空行/尾随空格四种边界断言精确字节；首尾非空行的行终止符保留 |
| A2 | **PARTIAL** | 测试+代码 | `test:test_duplicate_marker_rejected` `test:test_locator_is_mandatory` `code:extract_deliverable` | 合规路径只有 `--between`（唯一标记=交付身份）；`--fence n` 是位置索引，按第九轮审查**降级为非合规辅助**，不得单独作为 A2 证据 ；`human:交付块身份依赖上游约定，机器只能证明「取到的是你指的那个块」` |
| A3 | **FULL** | 测试+代码 | `test:test_post_payload_carries_conversation_id_only_when_given` `test:test_new_chat_has_no_parent` `code:send_prompt` | 请求体级别断言：`--new` 时 body 里没有该字段，既有会话必须带 |
| A4 | **FULL** | 测试+代码 | `test:test_verify_copy_detects_single_byte_change` `test:test_cli_exit_codes` `code:verify_copy` | 大小写差一个字节即判不一致并退 2 |
| A5 | **FULL** | 测试+代码 | `test:test_fallback_requires_all_login_429` `test:test_rotation_plan_puts_unavailable_last` `test:test_explicit_account_429_escalates_to_rotation` `test:test_explicit_account_401_does_not_escalate` `test:test_explicit_nologin_429_does_not_recurse` `test:test_normalize_converges_polluted_shapes` `test:test_normalize_does_not_fold_bool_or_float_cursor` `test:test_rotation_helpers_total_on_polluted_unavailable` `test:test_polluted_counters_increment_without_typeerror` `code:_normalize_rotation` `code:_as_int` `code:rotation_plan` `code:anon_fallback_block_reason` `code:create_chat_rotating` | 两账号都 429 才允许匿名；匿名计数不推主游标。F-33（用户裁定「429 换账号或直接用匿名」）：显式 `--account acct1|acct2 --new` 遇 429 必须记冷却后转轮转，501/403 不转（凭据失效/风控换号只会掩盖病因），显式 nologin 429 不转（再兜底还是匿名=自递归） |
| A6 | **FULL** | 测试+代码 | `test:test_fallback_requires_all_login_429` `test:test_fallback_blocked_when_disabled_by_env` `code:anon_fallback_block_reason` | 兜底判定只读 429 / nologin 冷却两个输入 |
| A7 | **PARTIAL** | 测试+代码+实测+人判 | `test:test_conv_owner_tolerates_non_dict_state_file` `code:conv_owner` `live:2026-11-19` `human:显式账号与登记冲突退 2 的分支只走了代码路径，没有独立回归` | 会话归属登记；归属文件内容不是 dict 时不得抛异常（F-12）。已知缺口：本条仍缺一条「冲突即退 2」的用例 |
| A8 | **PARTIAL** | 测试+代码 | `test:test_device_summary_counts_bound_sessions` `test:test_jar_is_persisted_and_header_is_readable` `test:test_anon_sessions_shape_pollution_never_escapes` `test:test_anon_browser_port_pollution_degrades_to_default` `code:load_anon_sessions` `code:AnonBrowser.__init__` `code:list_device_ids` `code:device_summary` | 设备快照双文件（txt + json）同时存在才算 known ；`human:设备快照与浏览器上下文的绑定只能真机验证` |
| A9 | **CODE_ONLY** | 代码+人判 | `code:_attach` `code:find_tab` `human:targetId+oai-did+conv_id 三要素校验在 UI 通道内，离线只能验标签页选择` | 可写窗口身份绑定靠真机验证 |
| A10 | **FULL** | 测试+代码 | `test:test_happy_path_keeps_conversation_id` `test:test_same_conversation_is_success` `test:test_landing_mismatch_is_failure_not_success` `test:test_landing_mismatch_quarantines_reply_with_both_ids` `test:test_landing_mismatch_return_carries_both_ids` `test:test_first_event_id_wins_and_repeated_id_is_fine` `test:test_multiple_different_ids_fail_closed` `test:test_truncated_get_targets_actual_conversation` `test:test_quarantine_dir_is_outside_replies_tree` `test:test_quarantine_preserves_full_raw_response` `test:test_quarantine_same_second_never_overwrites` `test:test_atomic_create_exclusive_never_overwrites` `test:test_evidence_complete_false_when_claim_fails` `test:test_evidence_complete_false_when_resp_save_fails` `test:test_quarantine_body_reflects_resp_save_failure` `test:test_pending_claim_registry_records_actual_conversation` `test:test_ambiguous_ids_failure_carries_candidates` `test:test_events_after_done_are_ignored` `test:test_id_after_done_alone_is_not_accepted` `test:test_id_after_done_is_not_used_by_regex_fallback` `test:test_sse_parsers_accept_non_str_without_raising` `test:test_sse_parsers_decode_bytes` `test:test_lock_judgement_rejects_non_path` `code:_as_text` `code:send_prompt` `code:_sse_conv_ids` `code:_quarantine_reply` `code:ConvRef` `code:_record_pending_claim` `code:QUARANTINE_DIR` `code:PENDING_CLAIMS_FILE` | 发送后 SSE **首个带 conversation_id 的** `data:` 事件的 id 必须与目标**相等**（`[DONE]` 是解析终点，之后的事件不参与落点证明）；不一致即 fail-closed——判失败 + 隔离保全（**完整原始响应**另存并记 sha256，隔离目录在 `replies/` 之外）+ 携带 target/actual 两个 id，**不静默改归属**；实际落点会话另记**待认领登记**，不自动登记归属（F-13）；**删除正文正则兜底**——不得从 `[DONE]` 之后或整包正文捞 id 当落点（F-16）；同一响应出现两个不同 id → 失败关闭且**带候选 id 列表**（F-17） |
| A11 | **FULL** | 测试+代码 | `test:test_missing_conversation_id_is_failure_not_silent_success` `test:test_invalid_conversation_id_is_rejected` `test:test_happy_path_keeps_conversation_id` `test:test_multiple_different_ids_fail_closed` `test:test_ambiguous_ids_failure_carries_candidates` `test:test_id_after_done_alone_is_not_accepted` `test:test_id_after_done_is_not_used_by_regex_fallback` `code:create_chat_rotating` | 200 后不再换身份；解不出 id、id 不合规、或同一响应出现多个 id → 返回 1、不落盘、不重投；失败结果**带 target 与候选 id**，不丢诊断信息（F-17） |
| A12 | **PARTIAL** | 测试+代码 | `test:test_reply_digest_reports_parts_and_sha` `test:test_tail_from_mapping_picks_newest` `test:test_malformed_bodies_never_raise` `test:test_malformed_sse_never_raises` `test:test_reply_digest_malformed_returns_none` `test:test_shape_helper_contract` `test:test_mapping_checked_reports_incomplete` `test:test_mapping_checked_complete_when_clean` `test:test_sse_checked_reports_bad_events_and_done` `test:test_tail_from_mapping_returns_none_when_malformed` `test:test_non_string_part_makes_reply_digest_none` `test:test_non_string_part_makes_mapping_incomplete` `test:test_as_ctime_rejects_non_finite_and_huge` `test:test_as_ctime_accepts_normal_values` `test:test_as_ctime_rejects_large_integer_valued_float` `test:test_structured_user_part_does_not_break_mapping` `test:test_structured_user_part_does_not_break_digest` `test:test_illegal_part_in_user_message_makes_digest_none` `test:test_structured_assistant_part_makes_digest_none` `test:test_reply_digest_matches_harvest_with_whitespace` `test:test_reply_digest_selects_same_assistant_as_harvest_on_ct_tie` `test:test_digest_none_never_leaves_harvest_complete` `code:reply_digest` `code:tail_from_mapping` `code:_scan_mapping` `code:_scan_sse` `code:_as_dict` `code:_split_parts` `code:_part_is_structured` `code:_assistant_text` | 截断补全文时按 part 数 + 拼接 SHA-256 对账，不只看「变长了」；对账器是**完整性证明器**——任何畸形整体判不可用，不许跳过坏 node 拿剩余 part 算摘要（F-12）；`parts` 里混进非字符串元素同样算畸形（F-12a）；时间戳必须有限且在 float64 精确整数范围内（F-12c）；**多模态 part 合法但不可文本化**——用户消息里的结构化 part 不算畸形、不得把 assistant 回复误报成残缺；assistant 消息含结构化 part 则对账整体判不可用（F-19）；**digest 与落盘 full 必须同口径**——两者共用 `_assistant_text`（拼接+strip），首尾带空白的合法回复不得被误判不完整（F-26）；**digest 与 harvest 必须选同一节点**——选最新 assistant 用 `(ct, order)`，ct 相同取插入序最后，与 `_scan_mapping` 稳定排序对齐（F-27） ；`human:只能对账 GET 结果自身，证明不了服务端没丢 part` |
| A13 | **PARTIAL** | 测试+代码 | `test:test_full_text_is_kept` `test:test_optional_truncation_is_labelled` `test:test_malformed_sse_skipped_but_good_line_kept` `test:test_archiving_not_blocked_by_digest_failure` `test:test_non_string_part_marks_sse_bad_event` `test:test_sse_missing_author_counts_as_bad_event` `test:test_sse_non_dict_author_counts_as_bad_event` `test:test_sse_event_without_message_is_not_bad` `test:test_explicit_null_message_is_bad_event` `test:test_explicit_non_dict_message_is_bad_event` `test:test_absent_message_key_is_not_bad_event` `test:test_incomplete_sse_is_labelled_not_silent` `test:test_completeness_requires_get_evidence_not_selfratio` `test:test_events_after_done_are_ignored` `test:test_sse_content_after_done_is_not_harvested` `test:test_structured_user_part_in_sse_is_not_bad_event` `test:test_illegal_part_still_counts_as_bad_event` `test:test_whitespace_padded_full_reply_is_proven_complete` `test:test_ct_tie_full_reply_is_proven_complete` `code:save_reply` | 默认全文；截断必须写进文件头；**辅助对账器不得成为归档的前置硬依赖**——对账失败/崩溃也必须继续落盘（F-12）；SSE 坏事件（缺/坏 author、坏 part、显式 `message: null`）必须计入坏事件数，不得静默跳过还报完整（F-12a/b、F-15）；`[DONE]` 之后的事件不采信（F-16）；**SSE 不完整必须触发 GET 补全与 digest 核验**，仍无法证明完整时归档文本自带「完整性未证明」声明、不冒充定稿（F-23/F-24）；**GET 补全拉回的完整回复首尾带空白时仍须证明完整**，不得因 digest/full 口径不一致而误标不完整（F-26）；**多条 assistant 同 ct 时仍须证明完整**，不得因选节点不一致而误标不完整（F-27） `human:截断阈值由调用方设，没有默认上限可断言` |
| A14 | **PARTIAL** | 测试+代码 | `test:test_jar_is_persisted_and_header_is_readable` `test:test_auth_cookies_are_never_written_to_disk` `test:test_cookie_helpers_never_raise_on_non_str` `code:save_device_cookie` `code:parse_cookie_header` | 发前落身份，auth cookie 永不落盘 ；`human:CDP 取 jar 的真机路径无法离线复现` |
| A15 | **CODE_ONLY** | 代码+人判 | `code:_file_lock` `human:账号级串行是调用点约定（同一账号不并发起两个 GET），离线只能验锁本身，验不了「谁在调用」` | 已知缺口：缺一个「同账号并发调用被拒」的用例 |
| A16 | **FULL** | 测试+代码 | `test:test_timeout_is_split_by_thinking` `code:send_prompt` | 思考 1800s / 非思考 420s 两档都有断言（同一预算会误杀 19 分钟的思考回复） |
| A17 | **PARTIAL** | 测试+代码 | `test:test_retry_after_is_clamped` `test:test_retry_after_prefers_body_rate_limit` `code:_retry_after` | Retry-After 当下限；body 里的 resets_in 优先 ；`human:服务端不给时限时只能退默认值，等价性靠实测` |
| A18 | **CODE_ONLY** | 代码+人判 | `code:classify_failure` `code:warm_cookies` `human:401/403/429 各自冷却时长由调用方按定性结果落地，无集中断言` | 403 补 cookie + PoW 单次重试有实现，冷却值是调用点约定 |
| A19 | **FULL** | 测试+代码 | `test:test_rejects_traversal_and_junk` `test:test_malicious_conv_id_cannot_escape` `test:test_non_str_ids_use_contract_error_not_attributeerror` `code:safe_id` `code:id8` `code:cli_id` | id 白名单 + 归档路径不逃逸 |
| A20 | **PARTIAL** | 测试+代码 | `test:test_tmp_is_created_in_target_directory` `test:test_atomic_write_uses_unique_tmp_names` `test:test_tmp_is_in_same_dir_and_fsync_failure_cleans_up` `test:test_replace_failure_cleans_up_and_keeps_old_content` `test:test_atomic_write_fsyncs_parent_directory` `test:test_concurrent_updates_do_not_lose_writes` `code:_atomic_write` | 六项保证逐项有断言：同目录(spy src)、tmp 名唯一、文件 fsync、replace 失败保旧值、目录 fsync(POSIX)、失败必清 tmp `human:Windows 无法 open 目录，目录 fsync 无对应语义；断电/文件系统持久化无法用离线单测证明` |
| A21 | **FULL** | 测试+代码 | `test:test_corrupt_json_is_quarantined_not_silently_lost` `test:test_empty_file_is_not_quarantined` `test:test_top_level_type_mismatch_is_quarantined` `code:_load_json` | 损坏即隔离为 `.corrupt-<ts>`，空文件不误伤；F-34：**顶层类型坏同罪**（合法 JSON 但不是预期形状，如手改成 `[]`）——语法坏与类型坏同一份不可信输入必须同一待遇，否则 conv_owner 有守卫、record_conv 没有 |
| A22 | **FULL** | 测试+代码 | `test:test_lock_is_reentrant` `test:test_stale_lock_is_reclaimed` `test:test_lock_live_holder_not_reclaimed` `test:test_lock_pid_reused_reclaimed` `test:test_lock_unknown_identity_kept` `test:test_lock_legacy_format_uses_mtime` `test:test_lock_records_holder_identity` `test:test_proc_state_three_way_semantics` `test:test_proc_state_unknown_blocks_lock_reclaim` `test:test_proc_state_unknown_blocks_lease_reclaim` `test:test_proc_state_unknown_blocks_force_release` `test:test_lock_pid_field_falsy_not_reclaimed_by_mtime` `test:test_lock_pid_field_garbage_not_reclaimed` `test:test_lock_no_pid_field_uses_mtime` `test:test_parse_pid_rejects_lossy_coercion` `test:test_parse_pid_overflow_string_returns_none` `test:test_proc_state_rejects_float_and_bool_pid` `test:test_lock_float_pid_not_reclaimed` `test:test_lock_corrupt_json_not_reclaimed_by_mtime` `test:test_lock_non_dict_json_not_reclaimed` `test:test_lock_unreadable_not_reclaimed` `test:test_win_k32_declares_handle_prototypes` `test:test_lock_invalid_utf8_not_reclaimed` `test:test_lock_corrupt_start_time_not_reclaimed` `test:test_lock_valid_start_time_mismatch_is_reclaimed` `test:test_parse_start_time_contract` `code:_lock_holder_stale` `code:_proc_state` `code:_win_open_error_to_state` `code:_win_k32` `code:_parse_pid` `code:_parse_start_time` | 线程重入 + pid/启动时间/租约三态回收；进程存活判定 DEAD/ALIVE/UNKNOWN 三态，UNKNOWN 绝不触发回收（F-03）；pid 字段在但值坏不借 mtime 逃生（F-04）；Windows ctypes 显式 HANDLE 原型防 64 位截断（F-06）；pid 严格解析拒 int() 折叠（F-07）；损坏/非字典/读失败内容不借老格式 mtime 逃生（F-08）；非法 UTF-8 读取失败不回收（F-08补）；process_start_time 损坏值不当 pid_reused 证据（F-10） |
| A23 | **FULL** | 测试+代码+实测 | `test:test_webrtc_leak_detected` `test:test_webrtc_via_proxy` `test:test_webrtc_no_candidates_is_clean` `code:webrtc_probe` `code:_classify_webrtc` `live:2026-11-19` | 一次性标签页跑真实 ICE，`leaked` 退 3 |
| A24 | **FULL** | 测试+代码 | `test:test_fail_closed_when_nothing_configured` `test:test_allow_direct_is_explicit_only` `test:test_normalize_proxy_rejects_illegal_port` `test:test_tcp_alive_never_raises_on_junk_port` `test:test_autodetect_ports_pollution_never_escapes` `code:require_proxy` `code:normalize_proxy` `code:_tcp_alive` | 解析不到出口就 SystemExit(3)，直连需显式放行。F-35：代理端口字段收敛——坏端口＝配置非法（与坏 scheme 同待遇 ValueError，不得潜到 `u.port` 才炸）；`_tcp_alive` 对任何不可信端口返回 False（int() 的 TypeError/OverflowError 不得穿出）；`autodetect_ports` 畸形项跳过不崩 |
| A25 | **FULL** | 测试+代码 | `test:test_dead_proxy_detected_even_if_port_accepts` `test:test_socks5_handshake_reaches_target` `test:test_socks5_open_junk_port_raises_oserror_not_typeerror` `code:_proxy_alive` `code:_socks5_open` | 端口在听 ≠ 能用；要过 TLS 握手 |
| A26 | **FULL** | 测试+代码 | `test:test_probe_site_ok` `test:test_probe_site_blocked_by_vpn` `test:test_probe_site_challenge_is_usable` `test:test_probe_site_other_status_not_ok` `test:test_proxy_check_requires_site_ok` `code:probe_site` | 目标站 verdict 五分类，只有 ok/challenge 算通 |
| A27 | **FULL** | 测试+代码+实测 | `test:test_thinking_uses_thinking_slug` `test:test_default_keeps_auto_model` `code:resolve_thinking_model` `code:enable_thinking` `live:2026-11-19` | `-t-` 变体解析不出就退 1；浏览器侧幂等开按钮 |
| A28 | **FULL** | 测试+代码 | `test:test_truncated_stream_completed_by_get` `code:send` | `IncompleteRead.partial` 按 200 处理 + GET 补全文 |

## B. 定时任务基线

| 条款 | 等级 | 类型 | 证据 | 说明 |
|---|---|---|---|---|
| B0 | **HUMAN** | 人判 | `human:六问是建任务前的自检，逐项落到 B1–B15` | 不自证，只是入口 |
| B1 | **PARTIAL** | 测试+代码 | `test:test_hourly_rrule_ok` `test:test_non_hourly_rrule_rejected` `test:test_interval_not_one_rejected` `test:test_duplicate_rrule_part_rejected` `test:test_rrule_output_is_canonical` `test:test_lowercase_freq_accepted_and_normalised` `test:test_internal_whitespace_rejected` `test:test_interval_strict_decimal` `test:test_unsupported_rule_part_rejected` `test:test_malformed_rule_part_rejected` `test:test_rrule_rejects_leading_trailing_newline` `test:test_rrule_part_regex_rejects_newline_in_value` `test:test_huge_interval_does_not_escape_as_exception` `test:test_only_rrule_field_is_executable` `code:check_schedule` | 只认 HOURLY + INTERVAL=1；**重复 rule part 一律拒绝**（RFC 5545 §3.3.10），**下发用已验证字段重建的规范化 RRULE**，枚举值大小写不敏感但规范化为大写（F-14）；**唯一可执行字段是 `rrule`**，原始串只作审计留档；`INTERVAL` **不调用 `int()`**——超长十进制串会让 `int()` 抛 ValueError 逃逸（F-20） ；`human:实际触发间隔 ≥60 分钟与时区依赖调度器观测，rrule 层验不了` |
| B2 | **HUMAN** | 人判 | `human:三态状态机写在 automation prompt 与各任务状态文件里，没有共享库可断言` | 已知缺口：三态靠 prompt 自包含 |
| B3 | **HUMAN** | 人判 | `human:窗口左闭右开由调度器解释，本地只能校验时间串格式` | 已知缺口 |
| B4 | **FULL** | 测试+代码 | `test:test_oneshot_within_hour_ok` `test:test_oneshot_in_the_past_rejected` `test:test_oneshot_without_timezone_rejected` `test:test_oneshot_beyond_one_hour_rejected` `test:test_oneshot_bad_now_never_escapes_typeerror` `test:test_oneshot_echoes_canonical_iso` `code:check_oneshot` | 已过去 / 无时区 / 超 1 小时 三头全堵；`now` 异常一律 `ScheduleError` 不逃逸 TypeError（F-28）；回显规范化 ISO、原串只进 audit 字段（F-29） |
| B5 | **PARTIAL** | 测试+代码+实测+人判 | `test:test_oneshot_in_the_past_rejected` `code:check_oneshot` `live:2026-11-19` `human:存量 ACTIVE 的发现与 PAUSED 归档靠接口盘点，本地只实现「创建前预防」` | 两件事必须分开说：预防已机器化，**存量归档仍是人工**（本次盘出 4 个，已归档） |
| B6 | **HUMAN** | 人判 | `human:路径类型校验写在 prompt 的 preflight 段，没有本地实现` | 已知缺口 |
| B7 | **HUMAN** | 人判 | `human:step_id/attempt_count 状态机由各任务自己的状态文件落地，无共享实现` | 已知缺口 |
| B8 | **FULL** | 测试+代码 | `test:test_slot_cap_sequential` `test:test_slot_cap_under_process_race` `test:test_acquire_slot_fails_closed_on_lock_timeout` `test:test_release_slot_non_path_returns_false` `test:test_fresh_empty_slot_file_is_not_reclaimed` `test:test_concurrent_updates_do_not_lose_writes` `test:test_thread_lock_serialises_same_process` `test:test_strict_update_raises_locktimeout_not_silent` `test:test_strict_save_raises_locktimeout_not_silent` `test:test_file_lock_defaults_to_strict` `test:test_reclaim_removes_only_stale_lock` `test:test_reclaim_does_not_delete_fresh_lock` `test:test_reclaim_race_two_threads_never_double_enter` `test:test_reclaim_race_multi_process_mutual_exclusion` `test:test_lock_write_failure_does_not_enter_critical_section` `test:test_lock_short_write_does_not_enter_critical_section` `test:test_lock_write_failure_non_strict_also_blocks` `test:test_as_float_never_raises_on_overflow` `test:test_overflow_created_at_does_not_break_lock_judgement` `test:test_overflow_lease_and_ttl_do_not_raise` `test:test_overflow_in_server_rate_fields_does_not_raise` `code:acquire_slot` `code:release_slot` `code:_thread_lock_for` `code:_file_lock` `code:_try_reclaim_stale` `code:_kernel_lock` `code:_write_all` `code:_as_float` | 抢不到锁即 QUEUED，空槽 5s 内不当死槽；同进程线程互斥 + 跨进程文件锁两层；strict 超时抛 LockTimeout 不静默跳过；stale 回收判定+删除同处内核锁守卫内（F-02），默认 strict=True 堵误用；锁身份写入失败/短写不得进临界区（F-09）；状态文件超大数值（`10**400`）只降级不抛 OverflowError，锁判定永不因字段损坏崩溃（F-30） |
| B9 | **HUMAN** | 人判 | `human:PAUSED→等实例结束→删除的顺序只在操作时执行，无脚本` | 已知缺口 |
| B10 | **FULL** | 测试+代码 | `test:test_readonly_name_ok` `test:test_write_name_ok` `test:test_four_segments_rejected` `test:test_verb_must_match_whole_segment` `test:test_readonly_suffix_with_write_verb_rejected` `code:check_automation_name` | 分词判段，动作词整段相等 |
| B11 | **FULL** | 测试+代码 | `test:test_readonly_argv_whitelist_is_exact` `code:READONLY_ARGV` | `--rotation-reset` 不再被前缀匹配放行 |
| B12 | **PARTIAL** | 测试+代码+人判 | `test:test_cli_exit_codes` `test:test_proxy_check_requires_site_ok` `code:main` `human:exit 0/1/2/3 的语义分散在各分支，只能逐分支看；无事静默靠 stdout 行数人眼确认` | 0/1/2/3 各有用例，但不是集中式断言 |
| B13 | **HUMAN** | 人判 | `human:attempt_count 与 MANUAL_REVIEW 在各任务状态文件里，无共享库` | 已知缺口 |
| B14 | **HUMAN** | 人判 | `human:红线本身不含新判定，约束由 B8/A15/A17/A5/A23 各自实现` | 交叉引用型条款 |
| B15 | **PARTIAL** | 测试+代码 | `test:test_lease_valid_blocks_reclaim` `test:test_lease_expired_process_gone` `test:test_lease_pid_reused` `test:test_heartbeat_keeps_lease_alive` `test:test_lease_run_timeout_cancels_body` `test:test_lease_run_timeout_orphan_keeps_lease` `test:test_lease_ops_never_run_unlocked` `test:test_corrupt_lease_needs_approval_to_release` `test:test_process_alive_blocks_force_release` `test:test_force_release_refuses_without_lock` `test:test_heartbeat_and_release_timeout_keep_lease_bytes` `test:test_force_release_needs_identity_evidence_when_unreadable` `test:test_force_release_blocks_when_audit_registration_fails` `test:test_rejected_recovery_leaves_no_fake_audit_row` `test:test_force_release_reason_is_stable_enum` `test:test_force_release_unreadable_hash_is_stable` `test:test_quarantine_failure_is_stable` `test:test_quarantine_failed_row_write_failure_is_survivable` `test:test_rejected_recovery_leaves_no_audit_row_for_hash_and_identity` `test:test_force_release_missing_pid_needs_identity_evidence` `test:test_force_release_pid_zero_needs_identity_evidence` `test:test_corrupt_slot_is_registered` `test:test_lease_reclaimable_corrupt_start_time_not_reclaimed` `test:test_lease_reclaimable_valid_start_time_mismatch_is_pid_reused` `test:test_lease_reclaimable_none_start_time_alive` `test:test_force_release_preserves_existing_archive` `test:test_force_release_archive_conflict_many` `test:test_rename_no_overwrite_conflict_raises` `test:test_lease_heartbeat_write_failure_returns_false` `test:test_lease_release_write_failure_returns_false` `test:test_lease_reclaim_write_failure_returns_stable_reason` `test:test_lease_acquire_write_failure_returns_none` `code:lease_run` `code:lease_force_release` `code:FORCE_RELEASE_REASONS` `code:_rename_no_overwrite` `code:_diag` | 心跳续期 + 超时两档（TIMEOUT / TIMEOUT_ORPHAN）；F-32：磁盘写失败（磁盘满/只读盘/权限）必须落成文档承诺的返回值（`False`/`False`/`(False,"write_failed")`/`None`），OSError 不得穿出——心跳线程会静默死亡、自动化巡检会被崩掉；process_start_time 损坏值不当 pid_reused（F-10）；归档不覆盖已有归档（F-11/R7） `human:PID 复用/派生子进程/检测时序无法穷举；跨进程竞态只能抽样` |

## C. 配方

| 条款 | 等级 | 类型 | 证据 | 说明 |
|---|---|---|---|---|
| C1 | **HUMAN** | 人判 | `human:配方是 prompt 骨架，约束来自 B1/B12/B13 与 exit 码` | 模板不产生新判定 |
| C2 | **HUMAN** | 人判 | `human:配方是 prompt 骨架，判定依赖 B12 的 exit 码语义` | 同上 |
| C3 | **HUMAN** | 人判 | `human:配方是 prompt 骨架，窗口与三态由 B2/B3 约束` | 同上 |
| C4 | **HUMAN** | 人判 | `human:配方是 prompt 骨架，只读白名单由 B11 约束` | 同上 |

---

## 等级口径（第九轮审查要求：按「断言覆盖」分级，不按「有没有 test:」计数）

| 等级 | 含义 | 判定标准 |
|---|---|---|
| **FULL** | 核心判定 + 失败分支都被独立用例钉住 | 有正例和反例断言，且断言的是该条款的业务语义，不是相邻功能 |
| **PARTIAL** | 只覆盖了条款的一部分 | 例如只验「不该新建」而不验「存量归档」，或只验位置索引不验交付身份 |
| **CODE_ONLY** | 有实现、没有回归 | 代码路径存在，但没有用例钉住，改动不会被发现 |
| **HUMAN** | 只能人判 | 附理由；通常是流程约定或依赖外部接口状态 |

**为什么加这一列**：第九轮审查指出，`TestEvidenceMatrix` 只能证明「引用的用例存在」，不能证明「用例断言了这条款」。按 `test:` 出现次数统计会系统性高估。所以现在的口径是：**只有 FULL 才算「已机械化」**，PARTIAL 必须写明缺哪一半。

## 统计（2026-11-19，按等级）

| 等级 | 条数 | 条款 |
|---|---|---|
| FULL | 21 | A1 A3 A4 A5 A6 A10 A11 A16 A19 A21 A22 A23 A24 A25 A26 A27 A28 B4 B8 B10 B11 |
| PARTIAL | 12 | A2 A7 A8 A12 A13 A14 A17 A20 B1 B5 B12 B15 |
| CODE_ONLY | 3 | A9 A15 A18 |
| HUMAN | 13 | A0 B0 B2 B3 B6 B7 B9 B13 B14 C1 C2 C3 C4 |
| 合计 | 49 | — |

**结论**：49 条里 **21 条（43%）真正机械化**，12 条半机械化，3 条只有代码，13 条人判。任何「全都可以机器判定」的说法都是夸大。

**已知缺口（按危害排序，不许糊过去）**：

1. **B2/B7 三态状态机没有共享实现**——每个任务各写一份，最容易在续跑时重复提交副作用。
2. **B6 路径类型校验只在 prompt 里**——工作目录写错时，报错发生在半小时后的业务步骤，而不是 preflight。
3. **B5 只做了「创建前预防」**——存量过期 ACTIVE 的发现与 PAUSED 归档仍是人工接口操作，没有自动化回归。
4. **A2 的 `--fence` 是非合规辅助**——位置索引不等于交付身份；合规路径只有 `--between`。
5. **A7 缺「显式账号与登记冲突退 2」的回归**；A15/A16/A18 的判定依赖调用点约定。
6. **A10 升 FULL 后的残余**：落点不一致的**判定与处置**已用离线桩钉住（判失败 + 隔离保全 + 双 id + 待认领登记），但「真实服务端在什么条件下会落错会话」只能靠真机观测；隔离档案的**人工判定流程**（把回复迁回正确会话）尚无自动化。
7. **多模态内容只能投影成文本**：结构化 part（图片/音频对象）合法但不可文本化——用户消息里的多模态 part 会被文本收割器丢掉（不报畸形，避免误伤 assistant 回复），assistant 消息里的则让对账整体判不可用。要真正保留多模态内容需要另走附件通道。

**日期口径**：`live:` 一律按本机时区（UTC+8）记日期；矩阵校验用 `time.strftime("%Y-%m-%d")` 取本地日期，不按 UTC 折算。审查若按 UTC 看到「日期在未来」，属时区差异，不是伪造。