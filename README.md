# 小千问服务器

网页客户端 + 社区服务端（纯 Python 标准库，无第三方依赖）。

## 本地运行

```bash
python keyserver.py
# 打开 http://127.0.0.1:8090
```

## Render 部署

1. 新建 Web Service，选本仓库
2. Region: Singapore
3. Build Command: `echo no build`
4. Start Command: `python keyserver.py`
5. Plan: Free

## 默认管理密码

首次启动自动生成，见 `ai-web/keys.json` 的 admin 字段。

## 数据说明

免费平台磁盘是临时的，重启后 users.json / posts.json / uploads 会丢失。
