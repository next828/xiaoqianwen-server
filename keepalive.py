# -*- coding: utf-8 -*-
"""小千问服务器守护进程
- 自动重启崩溃的服务
- 可选：定时自检（让免费平台不休眠）
"""
import os, sys, time, subprocess, threading, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = os.environ.get("PORT", "8090")
SERVER = os.path.join(ROOT, "keyserver.py")
PING_URL = os.environ.get("PING_URL", "").strip()


def run_server():
    while True:
        print("[supervisor] 启动 keyserver …", flush=True)
        try:
            p = subprocess.Popen([sys.executable, SERVER], cwd=ROOT)
            p.wait()
            print("[supervisor] 进程退出 code=%s，3 秒后重启" % p.returncode, flush=True)
        except Exception as exc:
            print("[supervisor] 启动失败：%s" % exc, flush=True)
        time.sleep(3)


def keepalive():
    """如果有人配置了 PING_URL，就定期访问一下防止休眠"""
    if not PING_URL:
        return
    while True:
        time.sleep(600)  # 10 分钟
        try:
            urllib.request.urlopen(PING_URL, timeout=30).read()
            print("[keepalive] ping ok", flush=True)
        except Exception as exc:
            print("[keepalive] ping 失败：%s" % exc, flush=True)


if __name__ == "__main__":
    if PING_URL:
        threading.Thread(target=keepalive, daemon=True).start()
    print("小千问服务器启动（端口 %s）" % PORT, flush=True)
    run_server()
