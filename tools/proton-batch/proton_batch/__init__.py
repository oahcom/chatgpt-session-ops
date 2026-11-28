# -*- coding: utf-8 -*-
"""Proton Mail + ChatGPT 批量注册流水线。

模块划分：
    config      配置合并与路径解析
    store       SQLite 账号库（凭据 DPAPI 加密）
    proxy       代理池与冷却
    httpclient  统一出口、重试、脱敏
    captcha     hCaptcha / Arkose 打码
    sms         sms-activate 接码
    mailbox     IMAP 收码（Proton Bridge / 任意 IMAP）
    proton_api  Proton 注册 API
    chatgpt_api ChatGPT 注册 API（含 sentinel PoW）
    pipeline    编排队列与断点续跑
    cli         命令行入口
"""

__version__ = "1.0.0"
