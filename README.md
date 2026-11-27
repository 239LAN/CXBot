# CXBot

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.1.13-blue.svg)](#)

一个运行在 QQ 上的智能助手：接入 QQ Bot API v2，使用 DeepSeek 生成回复，支持流式消息、上下文管理、工具调用、文件空间与积分额度。

## 功能

- 私聊流式输出，群聊普通分条回复（QQ 群聊不支持流式）
- 回复以 QQ Markdown 渲染，发送失败自动回退纯文本；`/markdown off` 可关闭渲染并让模型改用纯文本
- 上下文隔离：私聊按用户独立，群聊按群共享
- 工具调用
  - 会话文件空间操作：列出、读取、写入、追加、替换、按行替换、删除、复制、重命名、建目录
  - 保存当前消息的附件（文档 / 图片 / 语音）到会话空间，查看空间占用
  - 在会话空间内执行 shell 命令（Linux 默认 bubblewrap 沙箱，DNS 配置兼容 systemd-resolved 软链接）
  - 沙箱资源限制：单任务上限 0.1 核 / 256MB，全局 0.5 核 / 512MB；并发数自动推导，运行中均分资源
  - 把空间中的文件发送给用户，超过 5MB 自动分片上传
  - 必应网页搜索、网页抓取（优先 Playwright 等待 JS 加载，回退 curl）
  - 群聊同样开放文件空间与沙箱命令，按群共享
- 流式过程显示：三档可调（不显示 / 命令与工具调用 / 含输出与结果），默认不显示，`/progress` 切换；开启后工具摘要按发生顺序追加在正文之后
- 流式正文严格追加：首帧固定零宽空格，之后只追加不替换已下发内容，避免 QQ 40111001
- 思考内容显示：私聊可开关，开启后以 `[正在深度思考]` 开头、`[已思考(用时 n 秒)]` 结尾，中间最多展示 800 字思考内容，之后接正式回答；同时推送到网页控制台，`/thinking show on|off`
- 输出长度与上下文预算：`/length`、`/context` 按 token 设置，普通用户上限不超过管理员
- 群聊设置权限：只有群主、群管理员或 Bot 管理员可以修改本群设置
- 长回复截断续传：超出单条上限时先发 `……` 与 `发送[/继续]继续`，1 小时内可继续
- Markdown 结构感知截断：流式正文只追加不改写，续传按实际已发送长度裁剪并自动重开代码围栏、补表格表头
- 多模态输入：图片按文件头校正 MIME，大图自动压缩后传给 DeepSeek-Flash 原生视觉理解；文档自动提取文本（txt / md / csv / json / pdf / docx / xlsx / pptx）
- 只发文件 / 图片时会先提示上传成功，10 分钟内的下一条提示词会合并处理这些附件
- 语音消息：优先使用 QQ 自带识别文本，对模型只呈现为 `[语音]内容`；Fun-ASR 第三方识别保留但默认关闭（`ASR_ENABLED=on` 开启）
- 图片生成（AI 工具）：用户让 AI 画图时自动调用，AI 按任务难度从配置的多个模型中自选，模型名不对外透露、用户也不能指定；扣费 = 基准积分 × 模型倍率 × 图片张数（倍率 0.75x～5x，基准 `IMAGE_GEN_CREDITS` 默认 10000），生成后直接作为图片消息发送（`IMAGE_GEN_ENABLED=on` 开启）
- 群聊工具与私聊一致：网页搜索 / 抓取、会话空间文件操作、保存附件、发送空间文件与图片生成（群聊发送文件需 ≤5MB）
- 积分额度：按真实 token 用量、发送文件大小、沙箱执行时长（100 积分/秒）、沙箱双向流量（1 积分/MB）与图片生成（基准积分 × 模型倍率）计费
- 自动清理超过 30 天未读取 / 写入的文件（管理员空间不清理）
- 网页控制台：默认监听 `0.0.0.0`，管理员在 QQ 发 `/console` 获取 30 秒刷新的验证码登录，可实时观察 AI 思考 / 工具调用 / 生成内容并人工接管回复；支持直接发送文本、图片与文件
- 自动更新：默认关闭，开启后按间隔（默认 1 小时，支持 cron）检查 GitHub Release，发现新版本自动下载安装包并由独立后台助手执行安装；管理员也可用 `/update` 一键检查更新
- 管理员名单：不限额度、不计费、工具轮数不限，输出上限 393216 tokens，上下文预算 1048576 tokens，空间上限 1GB
- 重置码：一次性恢复自己的额度；管理员可在聊天中生成
- 黑名单：管理员可拉黑用户，禁止其继续使用
- 取消任务：`/cancel`（别名 `/c`、`/stop`）停止当前会话正在进行的生成与工具调用；非流式回复会把已生成内容先补发出来，再返回取消结果
- 所有发出的文本消息统一以换行开头，避免首行被 QQ 端吞掉或排版挤压
- 入群与新人欢迎：被拉进群时由 AI 写自我介绍与功能说明，新成员入群时由 AI 写欢迎语；使用事件被动回复（event_id）而非主动消息，默认关闭思考、输出上限 1024 tokens，读取群上下文但不写入，不扣积分
- Minecraft Java 服务器状态探测：支持 `_minecraft._tcp` SRV 记录，显示延迟、版本、在线人数与 MOTD；AI 也可在用户询问时直接查服
- AI 代办对话设置：用户明确要求时，AI 可修改过程显示、Markdown、思考强度与显示、流式输出、输出上限与上下文预算（群聊沿用群主/管理员权限）

## 指令

| 指令 | 说明 |
| --- | --- |
| `/help` | 查看帮助 |
| `/about` | 查看版本、开源协议与当前设置 |
| `/reset` | 清空当前对话上下文（管理员可加会话标识） |
| `/continue` | 继续发送被截断的内容（1 小时内有效） |
| `/cancel` | 取消当前会话正在进行的任务（别名 `/c`、`/stop`） |
| `/quota` | 查看剩余额度 |
| `/quota detail` | 查看各周期已用 / 上限 |
| `/workspace` | 查看会话空间占用（私聊 / 群聊） |
| `/progress` | 查看过程显示档位 |
| `/progress off\|calls\|full\|default` | 不显示 / 命令与工具调用 / 含输出与结果 |
| `/length` | 查看单次输出上限 |
| `/length <tokens>\|default` | 设置输出上限（不超过管理员上限） |
| `/context` | 查看上下文预算 |
| `/context <tokens>\|default` | 设置上下文 token 上限（不超过管理员上限） |
| `/markdown` | 查看 Markdown 渲染状态 |
| `/markdown on\|off\|default` | 开 / 关 Markdown 渲染 |
| `/thinking` | 查看思考强度与思考显示 |
| `/thinking off\|low\|medium\|high\|max\|default` | 调整思考强度 |
| `/thinking show on\|off\|default` | 开 / 关思考内容显示（仅私聊流式） |
| `/stream` | 查看流式输出状态 |
| `/stream on\|off\|default` | 开 / 关流式输出（仅私聊） |
| `/motd <地址>` | 探测 Minecraft Java 服务器（支持 SRV，显示版本与在线人数） |
| `/redeem <重置码>` | 使用重置码恢复额度 |
| `/id` | 查看当前会话标识 |
| `/gencode [数量] [天数]` | 管理员：生成重置码 |
| `/resetquota [群标识]` | 管理员：直接重置群聊额度 |
| `/block <用户标识>` | 管理员：拉黑用户 |
| `/unblock <用户标识>` | 管理员：解除拉黑 |
| `/update` | 管理员：检查并安装最新版本 |

非管理员看不到「管理员」指令段；群聊帮助中不显示私聊专属设置（`/progress`、`/stream`、`/thinking show`）。

## 网页人工回复控制台

机器人启动后，管理员在 QQ 私聊发送 `/console`（或 `/控制台`）获取验证码：

- 验证码每 30 秒刷新，登录有效期默认 12 小时
- 浏览器打开 `http://服务器IP:8080`，输入验证码登录
- 左侧列出所有会话（私聊 / 群聊），右侧查看完整消息记录
- 实时显示 AI 的思考、工具调用与正在生成的内容
- 默认仍由 AI 自动回复；点击「人工接管」后，该会话的新消息只进入网页，不再自动回复
- 在网页输入框直接发送回复，走 QQ 被动回复（受 5 分钟窗口与 4/5 条回复次数限制）
- 点「图片/文件」按钮可直接发送图片或文件（图片按图片消息发送，群聊文件需 ≤5MB），输入框内容会作为附带文字
- 点「退出」或直接关闭网页时，自动解除所有会话的人工接管，恢复 AI 自动回复
- 人工发送的内容超过单条上限时会截断并提示，用户发送 `/继续` 可继续接收剩余部分
- 消息与接管状态持久化到 `data/console.json`，重启不丢
- 同一 IP 每分钟最多尝试 8 次验证码

相关配置：

| 配置 | 说明 |
| --- | --- |
| `CONSOLE_ENABLED` | 是否启动网页控制台，默认 `on` |
| `CONSOLE_HOST` / `CONSOLE_PORT` | 监听地址与端口，默认 `0.0.0.0:8080` |
| `CONSOLE_SESSION_HOURS` | 登录有效期（小时），默认 12 |
| `CONSOLE_FILE` | 消息与接管状态文件，默认 `data/console.json` |
| `CONSOLE_HISTORY` | 每个会话保留的消息条数，默认 200 |
| `CONSOLE_UPLOAD_MB` | 控制台发送图片/文件的大小上限（MB），默认 20 |

## 部署

### 一键安装（推荐）

从 [Releases](https://github.com/239LAN/CXBot/releases/latest) 下载 `cxbot-<版本>-install.sh`，它是内嵌源码的单文件安装包，会创建虚拟环境、安装依赖、写入 systemd 服务并设置开机自启与崩溃重启：

```bash
sudo bash cxbot-1.1.0-install.sh
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
| `QQ_INTENTS` | 事件订阅位，默认 `50331648`（群@消息与单聊消息 + 群成员事件）；入群/新人欢迎需要群成员事件权限 |
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
| `SANDBOX_CPU_PERCENT` | 单任务 CPU 上限（百分比），默认 10（0.1 核） |
| `SANDBOX_MEMORY_MB` | 单任务内存上限，默认 256 |
| `SANDBOX_TOTAL_CPU_PERCENT` | 全局 CPU 上限，默认 50（0.5 核） |
| `SANDBOX_TOTAL_MEMORY_MB` | 全局内存上限，默认 512 |
| `SANDBOX_MAX_PARALLEL` | 并发上限，默认 0 表示自动推导；运行中按并发数均分资源 |
| `WORKSPACE_CLEANUP_DAYS` | 未访问文件自动清理天数，默认 30 |
| `QUOTA_CREDITS_PER_SECOND` | 沙箱每秒积分，默认 100 |
| `QUOTA_CREDITS_PER_MB_NET` | 沙箱每 MB 流量积分（双向），默认 1 |
| `QUOTA_USER_DAILY/WEEKLY/MONTHLY` | 用户/群额度，默认 80000 / 320000 / 800000 |
| `QUOTA_GLOBAL_DAILY/WEEKLY/MONTHLY` | 全站额度，默认 500000 / 3000000 / 10000000 |
| `ADMIN_FILE` | 管理员名单文件，默认 `data/admins.json` |
| `BLOCK_FILE` | 黑名单文件，默认 `data/blocked.json` |
| `CONTEXT_MAX_TOKENS` | 普通用户上下文 token 预算，默认 32000，上限为 `ADMIN_CONTEXT_TOKENS` |
| `ADMIN_MAX_TOKENS` | 管理员单次输出上限，默认 393216，也是 `/length` 的全局上限 |
| `ADMIN_CONTEXT_TOKENS` | 管理员上下文 token 预算，默认 1048576，也是 `/context` 的全局上限 |
| `AUTO_UPDATE_ENABLED` | 是否开启自动更新，默认 `off` |
| `AUTO_UPDATE_SCHEDULE` | 检查间隔，默认 `1h`；也可写 5 段 cron，如 `0 */6 * * *` |

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
