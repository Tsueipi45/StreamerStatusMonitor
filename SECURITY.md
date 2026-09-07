# 安全说明

## 凭据

真实凭据和运行状态只应保存在部署服务器的 `data/` 目录。请勿提交以下内容：

- QQ AppSecret、用户 OpenID 和绑定状态
- Twitch Client Secret 或访问令牌
- YouTube API Key
- SSH 私钥、`.env` 文件和运行日志

项目的 `.gitignore` 已排除常见敏感文件，但这不能代替提交前检查。

如果凭据曾经公开，请在对应平台撤销并重新生成。更新 QQ 凭据可运行 `python qq_private.py replace-credentials`；其他平台请更新 `data/` 中对应配置，并重启相关服务。

## 报告问题

请不要在公开 Issue 中粘贴密钥、用户 OpenID、服务器地址或完整日志。报告安全问题时，只提供经过遮盖且足以复现问题的内容。
