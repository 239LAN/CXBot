# CXBot

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.1.0-blue.svg)](#)

一个运行在 QQ 上的智能助手：接入 QQ Bot API v2，使用 DeepSeek 生成回复，支持流式消息、上下文管理、工具调用、文件空间与积分额度。

## 功能

- 私聊流式输出，群聊普通分条回复（QQ 群聊不支持流式）
- 回复以 QQ Markdown 渲染，发送失败自动回退纯文本；`/markdown off` 可关闭渲染并让模型改用纯文本
- 上下文隔离：私聊按用户独立，群聊按群共享
- 工具调用
  - 会话文件空间操作：列出、读取、写入、追加、替换、按行替换、删除、复制、重命名、建目录
  - 保存当前消息的附件（文档 / 图片 / 语音）到会话空间，查看空间占用
  - 在会话空间内执行 shell 命令（Linux 默认 bubblewrap 沙箱，已修复 DNS 配置）
  - 把空间中的文件发送给用户，超过 5MB 自动分片上传
  - 必应网页搜索、网页抓取（优先 Playwright 等待 JS 加载，回退 curl）
  - 群聊同样开放文件空间与沙箱命令，按群共享
- 流式过程显示（默认关闭，`/progress on` 开启）：【阅读了 x 个文件】【执行了 1 条命令】等
- 长回复截断续传：超出单条上限时先发 `……` 与 `发送[/继续]继续`，1 小时内可继续
- 多模态输入：图片按文件头校正 MIME，大图自动压缩后传给 DeepSeek-Flash 原生视觉理解；文档自动提取文本（txt / md / csv / json / pdf / docx / xlsx / pptx）
- 只发文件 / 图片时会先提示上传成功，10 分钟内的下一条提示词会合并处理这些附件
- 语音消息：优先使用 QQ 自带识别文本，对模型只呈现为 `[语音]内容`；Fun-ASR 第三方识别保留但默认关闭（`ASR_ENABLED=on` 开启）
- 积分额度：按真实 token 用量与发送文件大小计费，用户/群与全站分别限制日、周、月额度
- 管理员名单：不限额度、不计费、工具轮数不限，输出上限 393216 tokens，上下文预算 1048576 tokens，空间上限 1GB
- 重置码：一次性恢复自己的额度；管理员可在聊天中生成
- 黑名单：管理员可拉黑用户，禁止其继续使用
- Minecraft Java 服务器状态探测

## 指令

| 指令 | 说明 |
| --- | --- |
| `/help` | 查看帮助 |
| `/about` | 查看版本信息 |
| `/reset` | 清空当前对话上下文 |
| `/continue` | 继续发送被截断的内容（1 小时内有效） |
| `/quota` | 查看剩余额度 |
| `/quota detail` | 查看各周期已用 / 上限 |
| `/workspace` | 查看会话空间占用（私聊 / 群聊） |
| `/progress` | 查看过程显示状态 |
| `/progress on\|off\|default` | 开 / 关工具过程记录（仅私聊流式） |
| `/markdown` | 查看 Markdown 渲染状态 |
| `/markdown on\|off\|default` | 开 / 关 Markdown 渲染 |
| `/thinking` | 查看思考强度 |
| `/thinking off\|low\|medium\|high\|max\|default` | 调整思考强度 |
| `/stream` | 查看流式输出状态 |
| `/stream on\|off\|default` | 开 / 关流式输出（仅私聊） |
| `/motd <地址>` | 探测 Minecraft Java 服务器 |
| `/redeem <重置码>` | 使用重置码恢复额度 |
| `/id` | 查看当前会话标识 |
| `/gencode [数量] [天数]` | 管理员：生成重置码 |
| `/resetquota [群标识]` | 管理员：直接重置群聊额度 |
| `/block <用户标识>` | 管理员：拉黑用户 |
| `/unblock <用户标识>` | 管理员：解除拉黑 |

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

Python 3.10+。图片处理依赖 Pillow（大图与非常规格式会转成 JPEG）。网页抓取如需等待 JS 加载，可额外安装 Playwright 并执行 `playwright install chromium`。第三方语音识别默认关闭；设置 `ASR_ENABLED=on` 后使用 Fun-ASR 异步接口，直接提交 QQ 语音附件的公网地址，无需本地转码。

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
| `ASR_ENABLED` | 是否启用第三方 Fun-ASR，默认关闭；开启后需配置 `ASR_API_KEY` |
| `ASR_BASE_URL` / `ASR_API_KEY` / `ASR_MODEL` | Fun-ASR 录音文件转写接口；`ASR_API_KEY` 留空时只使用 QQ 自带识别文本 |
| `USE_QQ_ASR_REFER` | 是否优先使用 QQ 语音参考文本，默认开启 |
| `VOICE_MAX_COUNT` / `VOICE_MAX_MB` | 每条消息最多处理的语音数量与单条大小上限 |
| `WORKSPACE_QUOTA_MB` | 每个会话（私聊用户 / 群）的文件空间上限，默认 50 |
| `WORKSPACE_ADMIN_QUOTA_MB` | 管理员文件空间上限，默认 1024（1GB） |
| `SANDBOX_NETWORK` | 沙箱命令是否允许联网，默认开启 |
| `QUOTA_USER_DAILY/WEEKLY/MONTHLY` | 用户/群额度，默认 80000 / 320000 / 800000 |
| `QUOTA_GLOBAL_DAILY/WEEKLY/MONTHLY` | 全站额度，默认 500000 / 3000000 / 10000000 |
| `ADMIN_FILE` | 管理员名单文件，默认 `data/admins.json` |
| `BLOCK_FILE` | 黑名单文件，默认 `data/blocked.json` |
| `ADMIN_MAX_TOKENS` | 管理员单次输出上限，默认 393216 |
| `ADMIN_CONTEXT_TOKENS` | 管理员上下文 token 预算，默认 1048576 |

计费规则：1 积分 = 1 输出 token = 4 输入 token = 1/10 MB 文件，各维度向上取整。

## 管理

管理员名单（名单内用户不限额度、不计费、工具轮数不限）：

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
