# -*- coding: utf-8 -*-
"""导出最近账号的明文凭据（本地查看用）。"""
import io
import sys

sys.path.insert(0, r"D:\code\chatgpt-session-ops\tools\proton-batch")
from proton_batch.store import Store, decrypt_secret

s = Store("data/accounts.db")
lines = []
for r in sorted(s.list(limit=3000), key=lambda x: x["id"]):
    if r["id"] < 68:
        continue
    lines.append(
        "{id} | {mail} | proton={pw} | chatgpt={cpw} | {st}/{cst}".format(
            id=r["id"],
            mail=r["proton_email"],
            pw=decrypt_secret(r.get("proton_password") or ""),
            cpw=decrypt_secret(r.get("chatgpt_password") or "") or "-",
            st=r["proton_status"],
            cst=r.get("chatgpt_status"),
        )
    )
io.open("pwlist.txt", "w", encoding="utf-8").write("\n".join(lines))
print("rows", len(lines))
