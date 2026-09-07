# StreamerStatusMonitor

在云服务器上监测 Twitch 开播、YouTube 开播/预约/影片更新，并通过 QQ 官方机器人向绑定账号发送私聊通知。

## 功能

| 模块 | 状态 | 行为 |
| --- | --- | --- |
| QQ 私聊 | 可用 | 一次性配对、访问控制、被动回复测试、主动通知测试 |
| Twitch | 可用 | 批量查询频道状态，每场直播只提醒一次 |
| YouTube | 可用 | 新影片、直播预约、正式开播分别提醒，首次启用不补发历史影片 |
| X | 尚未实现 | 计划监测指定账号的贴文与回复 |

通知失败会按 1、5、15、60 分钟退避重试，查询失败不会覆盖上一次有效状态。所有密钥、QQ 绑定资料和去重状态都保存在本机 `data/`，该目录不会进入 Git。

## 环境要求

- Ubuntu 22.04/24.04 或其他带 systemd 的 Linux
- Python 3.10+
- 可访问 QQ、Twitch 和 YouTube API 的网络
- QQ 开放平台机器人应用
- Twitch Developer 应用（启用 Twitch 监测时）
- YouTube Data API v3 Key（启用 YouTube 监测时）

## 安装

```bash
git clone https://github.com/Tsueipi45/StreamerStatusMonitor.git
cd StreamerStatusMonitor
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

### 1. 配置并绑定 QQ

QQ 开放平台使用 **WebSocket** 接入，不需要配置公网回调地址。执行初始化时，密钥输入不会显示，也不会进入 shell 历史：

```bash
.venv/bin/python qq_private.py init
.venv/bin/python qq_private.py listen
```

首次运行会显示 `绑定 <一次性配对码>`。在 10 分钟内用自己的 QQ 私聊机器人发送完整指令，然后发送：

- `测试`：验证私聊回复。
- `主动测试`：15 秒后尝试发送主动消息。

也可以在监听进程运行时，从另一个终端测试主动通知：

```bash
.venv/bin/python qq_private.py send-test
```

如需更换 QQ 机器人密钥：

```bash
.venv/bin/python qq_private.py replace-credentials
```

### 2. 配置 Twitch

在 [Twitch Developer Console](https://dev.twitch.tv/console/apps) 创建应用并取得 Client ID 与 Client Secret。监测器使用应用访问令牌，不要求主播授权。

```bash
.venv/bin/python twitch_monitor.py init
.venv/bin/python twitch_monitor.py check
```

初始化会询问一个或多个 Twitch 频道名或链接。配置结构可参考 [`config/twitch.example.json`](config/twitch.example.json)。

### 3. 配置 YouTube

在 Google Cloud 项目中启用 YouTube Data API v3 并创建 API Key。复制样例并填写要监测的频道 ID；仓库中不要保存真实 API Key：

```bash
mkdir -p data
cp config/youtube.example.json data/youtube.json
.venv/bin/python youtube_monitor.py set-key
.venv/bin/python youtube_monitor.py check
```

频道 ID 可从频道页面源码、YouTube Data API 或其他可信的频道查询工具取得。每个频道配置包含 `channel_id` 和用于通知展示的 `name`。

## 常驻运行

先完成 QQ 绑定及所需平台配置，再执行：

```bash
chmod +x deploy.sh
./deploy.sh
```

脚本会创建虚拟环境、安装依赖、运行测试，并按当前 Linux 用户和项目目录生成/启用 systemd 服务。只有对应配置文件存在时才会启用 Twitch 或 YouTube 服务。

```bash
sudo systemctl status streamer-qq streamer-twitch streamer-youtube --no-pager
sudo journalctl -u streamer-qq -u streamer-twitch -u streamer-youtube -n 100 --no-pager
```

仓库里的三个 `.service` 文件保留 Ubuntu 默认部署参数；`deploy.sh` 安装时会替换用户名和路径。

## 数据文件

运行后会在 `data/` 产生以下文件：

| 文件 | 内容 |
| --- | --- |
| `config.json` | QQ AppID 与密钥 |
| `owner.json` | 已绑定用户的 OpenID |
| `twitch.json` | Twitch 凭据、频道及轮询间隔 |
| `youtube.json` | YouTube API Key、频道及轮询间隔 |
| `*-state.json` | 去重、状态与重试资料 |

备份或迁移服务器时应保留整个 `data/` 目录，并按敏感凭据保护。它不会被 `deploy.sh` 清理，也已被 `.gitignore` 排除。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试使用模拟 API 验证配对、访问控制、重复事件、通知去重、重试和 YouTube 影片更新，不会向真实 QQ 账号发消息。

## 安全

不要提交 `data/`、`.env`、私钥或 API 密钥。若凭据曾出现在聊天、终端记录或公开仓库中，请在对应平台重新生成，然后更新服务器本地配置。更多说明见 [`SECURITY.md`](SECURITY.md)。
