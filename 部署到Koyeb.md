# 部署到 Koyeb（免费、不用信用卡）

## 一、免费额度

| 项目 | 免费版 |
|---|---|
| 实例 | 1 个 Web Service |
| 配置 | 512 MB 内存 / 0.1 vCPU |
| 区域 | Singapore（离国内最近）|
| 信用卡 | 不需要 |

## 二、创建服务时填这些

| 字段 | 填什么 |
|---|---|
| Service type | Web Service |
| Repository | next828/xiaoqianwen-server |
| Branch | main |
| Builder | Dockerfile |
| Dockerfile path | Dockerfile |
| Port | 8000 |
| Instance type | Free (Nano) |
| Region | Singapore |
| Scaling | 固定 1 个实例（不要 scale to zero）|

## 三、环境变量

| Key | Value |
|---|---|
| PORT | 8000 |

## 四、部署后

地址形如：

    https://xiaoqianwen-xxx.koyeb.app

打开就能用。

## 五、拿管理密码

首次启动自动生成 ai-web/keys.json。
在 Koyeb 控制台的 Console 里执行：

    cat ai-web/keys.json

里面的 admin 字段就是管理密码。

## 六、注意

| 问题 | 说明 |
|---|---|
| 磁盘临时 | 重启后账号/帖子/图片会丢 |
| 性能低 | 512MB，够几十人 |
| 首次构建慢 | 约 2-5 分钟 |

## 七、如果连不上 GitHub

Koyeb 选仓库要授权 github.com。如果那边打不开，就需要用 Docker 镜像方式（本地构建后推到 Docker Hub）。
