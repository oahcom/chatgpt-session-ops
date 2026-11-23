# 未跟踪本机脚本清单（49 项）

本清单由 ChatGPT 决策会话 #95 产出，经由独立验收方 #94 指定的格式与分类口径，操控层逐字落盘。分类为治理记录，不等于逐文件源码审计完成。

| 文件名 | 实际用途 / 入口行为 | 主要副作用或访问对象 | 分类 | 分类依据 |
|---|---|---|---|---|
| chrome_keeper.py | 浏览器维护类脚本；具体入口行为待核验 | 本机浏览器及其运行状态 | 待核验 | #88 将其列入待核验；需补充完整源码、调用方及实际运行记录 |
| cleanup_connectors.py | 连接器清理用途尚未取得逐文件证据 | 可能涉及本机连接器配置或服务端连接器；具体对象待核验 | 待核验 | 缺少该文件完整源码、调用关系及清理目标证据 |
| do_reconnect.py | 本机重新连接操作；具体连接对象待核验 | 本机账号、连接状态或认证会话 | 本机运维 | #88 将此类重连操作归入 A 专用运行脚本；不作为正式仓库发布内容 |
| extract_account.py | 提取本机账号信息 | 本机账号资料或认证状态 | 本机运维 | #88 已将账号提取类脚本归入 A 专用；涉及本机账号数据 |
| fetch_conv_tmp.py | 通过 API 获取完整会话并逐字导出 assistant 内容 | GitHub API、会话数据、硬编码的 GitHub OAuth token | 本机运维 | 本轮实测确认其用途及硬编码凭据；不入库，须先净化并处理凭据泄露 |
| fix_login.py | 本机登录修复操作；具体修复步骤待核验 | 本机登录状态、账号或浏览器认证信息 | 本机运维 | #88 已将登录修复类脚本归入 A 专用；涉及本机认证环境 |
| gen_206.py | 具体用途与入口行为未充分确认 | 副作用及访问对象未知 | 待核验 | #88 已将其列为待核验；需补充完整源码、调用方及运行证据 |
| link_once.py | 单次连接或授权操作；具体协议与目标待核验 | 本机账号、连接器或授权状态 | 本机运维 | #90 已确认其属于含 Keycloak 明文凭据的本机脚本整改范围；不得随正式仓库发布 |
| login_probe.py | 登录探测类脚本 | 本机登录会话、账号或认证端点 | 本机运维 | #88 将登录探测类脚本归入 A 专用；不作为通用正式功能发布 |
| mcp_create.py | MCP 创建流程脚本；具体创建入口待核验 | MCP 服务端及本机认证状态 | 本机运维 | #88 将 `mcp_create*.py` 归入 A 专用；不发布本机创建流程脚本 |
| mcp_create_dcr.py | MCP 动态客户端注册创建流程；具体请求行为待核验 | MCP 服务端、客户端注册及认证状态 | 本机运维 | #88 将 `mcp_create*.py` 归入 A 专用；不发布本机创建流程脚本 |
| mcp_create_ui.py | MCP UI 创建流程脚本；具体交互行为待核验 | 本机浏览器、MCP 服务端及认证状态 | 本机运维 | #88 将 `mcp_create*.py` 归入 A 专用；不发布本机创建流程脚本 |
| mcp_discovery.py | MCP 服务发现及本机代理拓扑探测 | 固定 MCP endpoint、本机 `127.0.0.1:8790` 代理 | 本机运维 | #89 已明确裁决：含本机专属 endpoint 和代理拓扑，不入库 |
| mcp_link.py | MCP 连接/授权流程脚本 | MCP 服务端、本机账号及认证状态 | 本机运维 | #88 将 `mcp_link.py` 归入 A 专用；#90 又确认其属于 Keycloak 凭据净化范围 |
| mcp_register.py | MCP 注册流程脚本 | MCP 服务端、客户端注册及认证状态 | 本机运维 | #88 将注册类流程归入 A 专用；#90 确认其属于 Keycloak 凭据净化范围 |
| oauth_continue.py | OAuth 登录/授权流程续接 | 本机账号、OAuth 会话及认证端点 | 本机运维 | #88 将 `oauth_*.py` 归入 A 专用；不发布本机授权流程脚本 |
| oauth_login.py | OAuth 登录流程 | 本机账号、OAuth 会话及认证端点 | 本机运维 | #88 将 `oauth_*.py` 归入 A 专用；#90 确认其属于 Keycloak 凭据净化范围 |
| open_plugin.py | 插件打开/连接流程；具体行为待核验 | 本机浏览器、插件页面或连接器状态 | 待核验 | #88 已将其列为待核验；需补充完整源码、调用方及实际运行证据 |
| probe.py | 通用探测脚本；具体探测目标待核验 | 本机认证状态或服务端探测目标 | 本机运维 | #88 将 `probe*.py` 归入 A 专用探测脚本组；不发布本机诊断工具 |
| probe_all_ids.py | ID 批量探测；具体 ID 类型与目标待核验 | 账号或服务端资源标识 | 本机运维 | #88 将 `probe*.py` 归入 A 专用探测脚本组；不发布本机诊断工具 |
| probe_bearer.py | Bearer 认证探测 | 本机认证凭据及认证端点 | 本机运维 | #88 将 `probe*.py` 归入 A 专用探测脚本组；不发布本机认证诊断工具 |
| probe_compare_auth.py | 认证方式对比探测 | 本机认证状态及认证端点 | 本机运维 | #88 将 `probe*.py` 归入 A 专用探测脚本组；不发布本机认证诊断工具 |
| probe_connector_detail.py | 连接器详情探测 | 连接器服务端及本机认证状态 | 本机运维 | #88 将 `probe*.py` 归入 A 专用探测脚本组；不发布本机连接器诊断工具 |
| probe_create_bearer.py | Bearer 认证创建流程探测 | 认证端点、Bearer 凭据及服务端状态 | 本机运维 | #88 将 `probe*.py` 归入 A 专用探测脚本组；不发布本机认证诊断工具 |
| probe_create_connector.py | 连接器创建流程探测 | 连接器服务端及本机认证状态 | 本机运维 | #88 将 `probe*.py` 归入 A 专用探测脚本组；不发布本机连接器诊断工具 |
| probe_create_full.py | 完整创建流程探测 | 服务端创建接口及本机认证状态 | 本机运维 | #88 将 `probe*.py` 归入 A 专用探测脚本组；不发布本机诊断工具 |
| probe_create_static.py | 静态创建流程探测 | 服务端创建接口及本机认证状态 | 本机运维 | #88 将 `probe*.py` 归入 A 专用探测脚本组；不发布本机诊断工具 |
| probe_emails.py | 邮箱相关探测；具体数据来源待核验 | 账号标识或邮箱数据 | 本机运维 | #88 将 `probe*.py` 归入 A 专用探测脚本组；涉及本机账号诊断，不发布 |
| probe_last.py | 最近状态探测；具体对象待核验 | 本机运行状态或服务端状态 | 本机运维 | #88 将 `probe*.py` 归入 A 专用探测脚本组；不发布本机诊断工具 |
| probe_link_refresh.py | 连接刷新流程探测 | 连接器、OAuth 会话及认证状态 | 本机运维 | #88 将 `probe*.py` 归入 A 专用探测脚本组；不发布本机连接器诊断工具 |
| probe_links_list.py | 连接列表探测 | 连接器服务端及本机认证状态 | 本机运维 | #88 将 `probe*.py` 归入 A 专用探测脚本组；不发布本机连接器诊断工具 |
| probe_links_oauth.py | OAuth 连接列表探测 | OAuth 连接器及本机认证状态 | 本机运维 | #88 将 `probe*.py` 归入 A 专用探测脚本组；不发布本机 OAuth 诊断工具 |
| probe_list_accessible.py | 可访问资源列表探测 | 服务端资源列表及本机认证状态 | 本机运维 | #88 将 `probe*.py` 归入 A 专用探测脚本组；不发布本机诊断工具 |
| probe_mcp_cf.py | MCP/Cloudflare 相关探测；具体请求行为待核验 | MCP endpoint、网络代理或认证状态 | 本机运维 | #88 将 `probe*.py` 归入 A 专用探测脚本组；属于本机诊断工具，不发布 |
| probe_mcp_handshake.py | MCP 握手探测 | 本机代理、Keycloak 认证及 MCP 专属 endpoint | 本机运维 | #89 已明确裁决：本机专属脚本，原版隔离备份并净化明文密码，不入库 |
| probe_menu.py | 菜单/界面探测；具体对象待核验 | 本机浏览器或插件 UI | 本机运维 | #88 将 `probe*.py` 归入 A 专用探测脚本组；不发布本机 UI 诊断工具 |
| probe_oauth.py | OAuth 流程探测 | 本机 OAuth 会话及认证端点 | 本机运维 | #88 将 `probe*.py` 归入 A 专用探测脚本组；不发布本机认证诊断工具 |
| probe_own_link.py | 自有连接探测；具体目标待核验 | 本机连接器及账号授权状态 | 本机运维 | #88 将 `probe*.py` 归入 A 专用探测脚本组；不发布本机连接器诊断工具 |
| probe_refresh.py | 刷新流程探测 | 本机令牌、认证会话或连接状态 | 本机运维 | #88 将 `probe*.py` 归入 A 专用探测脚本组；不发布本机认证诊断工具 |
| probe_token_flow.py | Token 流程探测 | 本机认证令牌及认证端点 | 本机运维 | #88 将 `probe*.py` 归入 A 专用探测脚本组；#90 确认其属于 Keycloak 凭据净化范围 |
| probe_user.py | 用户信息探测 | 本机账号信息及服务端用户接口 | 本机运维 | #88 将 `probe*.py` 归入 A 专用探测脚本组；不发布本机账号诊断工具 |
| readtail.py | 日志尾部读取；具体日志路径待核验 | 本机日志文件 | 待核验 | 缺少实际读取路径、调用方及是否包含敏感信息的证据 |
| restore_login.py | 本机登录状态恢复 | 本机账号、浏览器配置或认证会话 | 本机运维 | #88 将登录恢复类脚本归入 A 专用；涉及本机认证状态 |
| trigger_mcp.py | 触发 MCP 操作；具体触发动作待核验 | MCP 服务端及本机认证状态 | 本机运维 | #88 将 MCP 操作流程脚本归入 A 专用；未列入正式发布白名单 |
| ui_open.py | 打开本机 UI/浏览器页面 | 本机浏览器及插件 UI | 本机运维 | #88 将 UI 操作类脚本归入 A 专用；依赖本机交互环境 |
| verify_all.py | 批量验证流程；具体验证对象待核验 | 本机账号、连接器或服务端状态 | 本机运维 | #88 将 `probe*.py` 等本机验证工具归入 A 专用组；该文件仍需结合源码确认验证范围 |
| which_account.py | 识别当前账号 | 本机账号及认证状态 | 本机运维 | #88 将账号诊断类脚本归入 A 专用；依赖本机账号上下文 |
| which_google.py | 识别 Google 账号或登录状态 | 本机浏览器、Google 账号会话 | 本机运维 | #88 将账号诊断类脚本归入 A 专用；依赖本机浏览器与账号上下文 |
| who.py | 识别当前身份/账号；具体输出字段待核验 | 本机账号或认证状态 | 本机运维 | #88 将账号诊断类脚本归入 A 专用；需避免输出凭据或个人敏感信息 |

## 增量记录 — 2026-11-23

本记录补充当前 A（WorkBuddy Skill 运行副本）脚本集合相对于 #95 历史快照的变化。
本记录不修改、不覆盖 #95 的原始 49 行分类快照。历史快照与本增量共同构成分类证据。

### 1. 当前文件集合变化

| 文件名 | 实际用途 / 入口行为 | 主要副作用或访问对象 | 分类 | 分类依据 |
|---|---|---|---|---|
| mcp_link.py | 当前 A/scripts 中已不存在；原历史快照记录其为 MCP 连接/授权流程脚本 | 历史用途涉及 MCP 服务端、本机账号及认证状态 | 本机运维 | #95 历史快照记录为本机运维；2026-11-23 实测当前 A/scripts/mcp_link.py 不存在。保留原分类作为历史记录，不代表该文件当前仍存在 |

### 2. 本轮删除的临时探索脚本（18 项）

下列文件均为 2026-11-23 为摸清 MCP OAuth connector 契约而临时写入 A/scripts 的一次性探索脚本，
在契约确认并固化为 `references/mcp-oauth-connector-contract.md` 后已全部删除。
它们从未进入 #95 的 49 行快照，也从未提交到本仓库；在此登记以免后续误判为"曾经存在但下落不明"。

| 文件名 | 删除前用途 | 删除原因 | 分类 |
|---|---|---|---|
| browser_link.py | 用 Playwright 走浏览器完成 connector 关联 | 契约已确认，改为 `link_once.py` 走 API，浏览器路径无用 | 已废弃 |
| cap_callback.py | 抓取回调请求的完整参数 | 回调端点已确认为 `links/oauth/callback` | 已废弃 |
| cap_create.py | 抓取创建 connector 的请求体 | 已确认为 `{"mcp_url", "auth_request": {"supported_auth": [...]}, "logo_url": null}` | 已废弃 |
| cap_mcp_create.py | 同上，侧重 UI 表单提交 | 同上 | 已废弃 |
| capture_full.py | 抓取完整响应（含 header） | `WWW-Authenticate` 契约已确认，无需再抓 | 已废弃 |
| capture_oauth_req.py | 抓取 OAuth 授权链路请求 | 四步链路已固化进 reference | 已废弃 |
| dump_dialog.py | 打印 UI 弹窗 DOM | `/plugins` 入口已确认，UI 探索结束 | 已废弃 |
| fill_ui_create.py | 表单填值提交创建 | 改为 API 创建，UI 路径不再使用 | 已废弃 |
| find_cb_endpoint.py | 穷举回调落库端点 | 端点已确认，穷举无意义 | 已废弃 |
| get_code.py | 从授权跳转链路里提取 `code` | 已并入 `link_once.py` | 已废弃 |
| mcp_create_oauth.py | 创建带 oauth 的 connector（早期猜测写法） | 平铺 `auth_request` 是错误写法，已由 `mcp_create_ui.py` 取代 | 已废弃 |
| mcp_create_oauth2.py | 同上，第二版猜测 | 同上 | 已废弃 |
| mcp_create_oauth3.py | 同上，第三版猜测 | 同上 | 已废弃 |
| mcp_link.py | MCP 连接/授权流程脚本（完整链路雏形） | 拆分为 `link_once.py` + `verify_all.py` | 已废弃 |
| mcp_link2.py | 同上，第二版 | 同上 | 已废弃 |
| post_callback.py | 手工 POST 回调 | 已并入 `link_once.py` | 已废弃 |
| submit_ui_create.py | UI 提交按钮点击 | 改为 API 创建 | 已废弃 |
| who2.py | 早期身份探测 | 已由 `scripts/chatgpt_send.py` 的 me 命令覆盖 | 已废弃 |

`del_connector.py`（尝试用各种猜测端点删除 connector）同样已删除，删因是"缺 sentinel 头导致误判无删除端点"；
其结论已更正并写入 `references/mcp-oauth-connector-contract.md` 第 5 节。

### 3. 新增文件

| 文件名 | 实际用途 / 入口行为 | 主要副作用或访问对象 | 分类 | 分类依据 |
|---|---|---|---|---|
| audit_rmc.py | 遍历指定账号（未传入账号参数时使用脚本内默认账号列表），通过 ChatGPTAPI 查询各账号可访问的 connector，筛选固定 RMC MCP endpoint，并输出 connector 名称、认证方式及 action 数量 | 通过本机 chatgpt_send.ChatGPTAPI 使用账号 cookies/token；访问 ChatGPT backend API 的 connector links/list_accessible 和 connector detail 接口；匹配固定 MCP endpoint `mcp.oachom0831.xyz` | 本机运维 | 2026-11-23 检查完整源码确认：脚本依赖本机账号认证状态及本机特定 MCP 域名，访问对象和运行环境均具有本机专属性，不属于通用正式发布功能 |
| cleanup_connectors.py | 按 `asdk_app_*` connector id 枚举当前账号的 connector 详情，并按内嵌 STALE 表逐条 `DELETE /backend-api/aip/connectors/<id>`，删除后复查 `GET` 应为 404 | 通过本机账号凭据访问 ChatGPT backend API；删除该账号下的 connector 关联（不可逆） | 本机运维 | #95 快照将其列为"待核验"；2026-11-23 检查完整源码后确认为一次性清理脚本，内嵌特定账号与本机 connector id，属本机专属，不发布 |
| link_once.py | 对单个账号执行"创建/复用 connector → `links/oauth` 取授权 URL → Keycloak 登录取 `code` → 提交 `links/oauth/callback` → `list_accessible` 校验"四步链路 | 使用本机账号 cookies/token 与 sentinel PoW；写 Keycloak 会话；在账号下新建或刷新 connector 关联 | 本机运维 | 2026-11-23 新增；依赖本机账号与本机特定 MCP endpoint，不具备通用性 |
| mcp_create_ui.py | 以正确 body 形状（`auth_request.supported_auth[]` + `logo_url`）创建 MCP connector，并打印返回的 `supported_auth` | 使用本机账号凭据；在账号下创建 connector（可能产生需清理的残留） | 本机运维 | 2026-11-23 新增；创建动作有服务端副作用且绑定本机账号 |
| probe_all_ids.py | 把历史 transcript 中出现过的 `asdk_app_*` id 逐个按账号 GET，输出 connector 名称与 auth 状态 | 使用本机账号凭据只读访问 connector detail 接口；读取本机 transcript 文件 | 本机运维 | #95 快照将其列为"待核验"；2026-11-23 检查完整源码后确认依赖本机 transcript 与账号上下文，属本机运维 |

### 4. 快照与增量的适用范围

- #95 的 49 行记录继续作为原始历史快照，不因文件删除或新增而改写。
- 本增量记录反映 2026-11-23 实测的三类变化：`mcp_link.py` 等 19 个探索脚本被删除、5 个脚本新增或经源码确认、`audit_rmc.py` 为新增脚本。
- 除上述范围外，不重新分类或改写 #95 快照中的其他文件。
- 本记录中的分类沿用既有五类枚举：`本机运维`、`正式候选`、`重复`、`已废弃`、`待核验`。
