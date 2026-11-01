# CXBot

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)

一个运行在 QQ 上的智能助手：接入 QQ Bot API v2，使用 DeepSeek 生成回复，支持流式消息、上下文管理、工具调用、文件空间与积分额度。

## 功能

- 私聊流式输出，群聊普通分条回复（QQ 群聊不支持流式）
- 上下文隔离：私聊按用户独立，群聊按群共享
- 工具调用
  - 私有空间文件操作：列出、读取、写入、追加、替换、按行替换、删除、复制、重命名、建目录
  - 在私有空间内执行 shell 命令（Linux 默认 bubblewrap 沙箱）
  - 把私有空间中的文件发送给用户，超过 5MB 自动分片上传
  - 必应网页搜索、网页抓取（优先 Playwright 等待 JS 加载，回退 curl）
- 多模态输入：图片直接传给模型；文档自动提取文本（txt / md / csv / json / pdf / docx / xlsx / pptx）
- 积分额度：按真实 token 用量与发送文件大小计费，用户/群与全站分别限制日、周、月额度
- 管理员名单：不限额度、不计费
- 重置码：一次性恢复自己的额度
- Minecraft Java 服务器状态探测

## 指令

| 指令 | 说明 |
| --- | --- |
| `/help` | 查看帮助 |
| `/reset` | 清空当前对话上下文 |
| `/quota` | 查看剩余额度 |
| `/quota detail` | 查看各周期已用 / 上限 |
| `/thinking` | 查看思考强度 |
| `/thinking off\|low\|medium\|high\|max\|default` | 调整思考强度 |
| `/stream` | 查看流式输出状态 |
| `/stream on\|off\|default` | 开 / 关流式输出（仅私聊） |
| `/motd <地址>` | 探测 Minecraft Java 服务器 |
| `/redeem <重置码>` | 使用重置码恢复额度 |
| `/id` | 查看当前会话标识 |

## 部署

### 一键安装（推荐）

`dist/cxbot-install.sh` 是内嵌源码的单文件安装包，会创建虚拟环境、安装依赖、写入 systemd 服务并设置开机自启与崩溃重启：

```bash
sudo bash dist/cxbot-install.sh
```

安装后编辑 `/opt/cxbot/.env` 填写配置，再执行：

```bash
sudo systemctl restart cxbot
sudo journalctl -u cxbot -f
```

### 手动运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，至少填写 QQ_APP_ID、QQ_CLIENT_SECRET、DEEPSEEK_API_KEY
python main.py
```

Python 3.10+。网页抓取如需等待 JS 加载，可额外安装 Playwright 并执行 `playwright install chromium`。

## 配置

配置集中在 `.env`，完整说明见 [.env.example](.env.example)。常用项：

| 变量 | 说明 |
| --- | --- |
| `QQ_APP_ID` / `QQ_CLIENT_SECRET` | QQ 机器人凭据 |
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥 |
| `DEEPSEEK_MODEL` | 模型名，默认 `deepseek-flash` |
| `DEEPSEEK_THINKING` | 默认是否开启思考模式 |
| `DEEPSEEK_REASONING_EFFORT` | 默认思考强度，默认 `low` |
| `DEEPSEEK_MAX_TOKENS` | 普通回复输出上限，默认 1024 |
| `DEEPSEEK_TOOL_MAX_TOKENS` | 工具调用时的输出上限，默认 4096 |
| `WORKSPACE_QUOTA_MB` | 每个私聊用户的文件空间上限，默认 50 |
| `SANDBOX_NETWORK` | 沙箱命令是否允许联网，默认开启 |
| `QUOTA_USER_DAILY/WEEKLY/MONTHLY` | 用户/群额度，默认 8000 / 32000 / 80000 |
| `QUOTA_GLOBAL_DAILY/WEEKLY/MONTHLY` | 全站额度，默认 50000 / 300000 / 1000000 |
| `ADMIN_FILE` | 管理员名单文件，默认 `data/admins.json` |

计费规则：1 积分 = 1 输出 token = 4 输入 token = 1/10 MB 文件，各维度向上取整。

## 管理

管理员名单（名单内用户不限额度、不计费）：

```bash
python scripts/manage_admins.py list
python scripts/manage_admins.py add <用户标识>
python scripts/manage_admins.py remove <用户标识>
```

生成重置码：

```bash
python scripts/gen_reset_codes.py 1 --days 1
```

重新构建单文件安装包：

```bash
python scripts/build_installer.py
```

## 目录结构

```
bot/        QQ 网关、API、消息处理与指令
services/   模型、上下文、额度、工具、网页、文档、沙箱等
scripts/    安装、管理员、重置码与打包脚本
dist/       构建产物（单文件安装包）
```

## 许可

本项目以 [GNU Affero General Public License v3.0](LICENSE) 发布。
