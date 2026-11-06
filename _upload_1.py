# -*- coding: utf-8 -*-
import base64, json, os, subprocess, sys

ROOT = r"E:\小千问部署"
REPO = "next828/xiaoqianwen-server"
files = []
for dp, dn, fn in os.walk(ROOT):
    if ".git" in dp:
        continue
    for f in fn:
        full = os.path.join(dp, f)
        rel = os.path.relpath(full, ROOT).replace("\\", "/")
        files.append((rel, full))

print("文件数:", len(files))
ok = 0
fail = []
for rel, full in files:
    try:
        with open(full, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode()
        body = {"message": "add " + rel, "content": b64}
        p = subprocess.run(["gh", "api", "-X", "PUT",
                            "/repos/%s/contents/%s" % (REPO, rel),
                            "-f", "message=add " + rel,
                            "-f", "content=" + b64],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode == 0:
            ok += 1
        else:
            fail.append((rel, (p.stderr or "")[:80]))
    except Exception as exc:
        fail.append((rel, str(exc)[:80]))

print("成功:", ok, "失败:", len(fail))
for r, e in fail[:5]:
    print("  ", r, e)
