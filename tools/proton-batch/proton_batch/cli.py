# -*- coding: utf-8 -*-
"""命令行入口。

    python -m proton_batch proton  --count 3
    python -m proton_batch chatgpt --limit 3
    python -m proton_batch all     --count 3
    python -m proton_batch status
    python -m proton_batch export  --out accounts.csv
    python -m proton_batch probe
    python -m proton_batch retry   --stage chatgpt --limit 3
"""
from __future__ import annotations

import argparse
import csv
import json
import sys

from .config import load_config, resolve_path
from .chatgpt_api import ChatGPTSignup
from .logging_util import setup_logging
from .mailbox import Mailbox
from .pipeline import Pipeline
from .store import decrypt_secret


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="proton_batch",
                                description="Proton Mail + ChatGPT 批量注册流水线")
    p.add_argument("--config", default=None, help="config.json 路径")
    p.add_argument("--verbose", action="store_true", help="调试日志")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("proton", help="批量注册 Proton 账号")
    sp.add_argument("--count", type=int, default=1)
    sp.add_argument("--prefix", default=None, help="覆盖用户名前缀")

    sc = sub.add_parser("chatgpt", help="用已注册的 Proton 邮箱注册 ChatGPT")
    sc.add_argument("--limit", type=int, default=None)

    sa = sub.add_parser("all", help="先注册 Proton，再注册 ChatGPT")
    sa.add_argument("--count", type=int, default=1)

    ss = sub.add_parser("status", help="查看账号库状态")
    ss.add_argument("--json", action="store_true")

    se = sub.add_parser("export", help="导出账号（含密码，CSV/JSON）")
    se.add_argument("--out", required=True)
    se.add_argument("--format", choices=["csv", "json"], default=None)

    sub.add_parser("probe", help="探测 Proton / ChatGPT 端点与出口")

    sub.add_parser("mailtest", help="IMAP 收信自检（只读，不打印正文）")

    sr = sub.add_parser("retry", help="重试失败阶段")
    sr.add_argument("--stage", choices=["proton", "chatgpt"], required=True)
    sr.add_argument("--limit", type=int, default=None)

    return p


# ---------------------------------------------------------------- 子命令

def cmd_proton(args, cfg, log):
    pipeline = Pipeline(cfg, log)
    created = pipeline.run_proton(args.count, args.prefix)
    log.info("Proton 阶段结束：成功 %d / 目标 %d", len(created), args.count)
    return 0 if len(created) == args.count else 1


def cmd_chatgpt(args, cfg, log):
    pipeline = Pipeline(cfg, log)
    done = pipeline.run_chatgpt(args.limit)
    log.info("ChatGPT 阶段结束：成功 %d", len(done))
    return 0 if done else 1


def cmd_all(args, cfg, log):
    pipeline = Pipeline(cfg, log)
    created = pipeline.run_proton(args.count)
    done = pipeline.run_chatgpt(limit=len(created) or None)
    log.info("全流程结束：Proton %d，ChatGPT %d", len(created), len(done))
    return 0 if created and done else 1


def cmd_status(args, cfg, log):
    pipeline = Pipeline(cfg, log)
    stats = pipeline.store.stats()
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0
    print("Proton :", json.dumps(stats["proton"], ensure_ascii=False))
    print("ChatGPT:", json.dumps(stats["chatgpt"], ensure_ascii=False))
    print("总计   :", stats["total"])
    print()
    print(f"{'ID':>4}  {'Proton 邮箱':<34} {'P-状态':<10} {'ChatGPT-状态':<10}")
    for row in pipeline.store.list(limit=50):
        print(f"{row['id']:>4}  {row['proton_email']:<34} "
              f"{row['proton_status']:<10} {row['chatgpt_status']:<10}")
    return 0


def cmd_export(args, cfg, log):
    pipeline = Pipeline(cfg, log)
    rows = pipeline.export_rows()
    out = resolve_path(cfg, args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fmt = args.format or ("json" if out.suffix.lower() == ".json" else "csv")
    if fmt == "json":
        out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        with out.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else
                                    ["id", "proton_email", "proton_password",
                                     "proton_status", "chatgpt_email",
                                     "chatgpt_password", "chatgpt_status", "created_at"])
            writer.writeheader()
            writer.writerows(rows)
    log.info("导出 %d 条 -> %s", len(rows), out)
    print(out)
    return 0


def cmd_probe(args, cfg, log):
    pipeline = Pipeline(cfg, log)
    http, proton = pipeline._services("probe")
    http.rotate_proxy()
    print("出口代理:", http._proxy or "(直连)")
    try:
        print("Proton API :", json.dumps(proton.probe(), ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - 探测必须报告而非崩溃
        print("Proton API : ERROR", exc)
    try:
        client = ChatGPTSignup(cfg, log, pipeline.pool)
        resp = client._req("GET", "https://chatgpt.com/api/auth/csrf",
                           headers={"Accept": "*/*",
                                    "Referer": "https://chatgpt.com/"})
        has_token = "csrfToken" in resp.text
        print("ChatGPT    :", resp.status_code, "csrf" if has_token else "no-csrf")
    except Exception as exc:  # noqa: BLE001
        print("ChatGPT    : ERROR", exc)
    print("Proton 模式:", cfg["proton"].get("mode"),
          "| 收码来源:", cfg["chatgpt"].get("code_source"))
    return 0


def cmd_mailtest(args, cfg, log):
    mail = Mailbox(cfg, log)
    try:
        rows = mail.probe()
    except Exception as exc:  # noqa: BLE001 - 自检必须报告而非崩溃
        log.error("IMAP 自检失败: %s", exc)
        return 1
    if not rows:
        print("收件箱为空，或当前账号无读取权限")
        return 1
    print(f"最近 {len(rows)} 封（不打印正文）：")
    for row in rows:
        print(f"  {row['date']:<28} {row['from']:<30} {row['subject']}")
    return 0


def cmd_retry(args, cfg, log):
    pipeline = Pipeline(cfg, log)
    if args.stage == "proton":
        rows = pipeline.store.list(proton_status="failed", limit=1000)
        log.info("待重试 Proton: %d 条（需人工确认是否换用户名）", len(rows))
        for row in rows[: args.limit or len(rows)]:
            print(row["id"], row["proton_username"], row.get("proton_error"))
        return 0
    done = pipeline.run_chatgpt(args.limit)
    return 0 if done else 1


HANDLERS = {
    "proton": cmd_proton,
    "chatgpt": cmd_chatgpt,
    "all": cmd_all,
    "status": cmd_status,
    "export": cmd_export,
    "probe": cmd_probe,
    "mailtest": cmd_mailtest,
    "retry": cmd_retry,
}


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，直接输出中文会 UnicodeEncodeError。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    if not cfg.get("_config_found"):
        print("提示：未找到 config.json，使用内置默认值。"
              "请先复制 config.example.json 并填写密钥。", file=sys.stderr)
    log_dir = resolve_path(cfg, cfg["storage"]["log_dir"])
    log = setup_logging(log_dir, args.verbose)
    try:
        return HANDLERS[args.command](args, cfg, log)
    except KeyboardInterrupt:
        log.warning("已中断。数据库保留断点，可重新运行。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
