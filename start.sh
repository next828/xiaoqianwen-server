#!/bin/bash
# 小千问服务器 - Linux 一键启动
cd "$(dirname "$0")"
export PORT="${PORT:-8090}"
echo "启动中，端口 $PORT …"
python3 keepalive.py
