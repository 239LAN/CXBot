#!/usr/bin/env bash
# CXBot 自解压安装脚本（Linux）
# 用法：sudo bash cxbot-install.sh [--prefix /opt/cxbot] [--user cxbot] [--no-start]
set -Eeuo pipefail

APP_NAME="CXBot"
SERVICE_NAME="cxbot"
DEFAULT_PREFIX="/opt/cxbot"
DEFAULT_USER="cxbot"

PREFIX="$DEFAULT_PREFIX"
RUN_USER="$DEFAULT_USER"
START_SERVICE=1

usage() {
    cat <<'EOF'
用法：sudo bash cxbot-install.sh [选项]

选项：
  --prefix DIR   安装目录（默认 /opt/cxbot）
  --user NAME    运行服务的系统用户（默认 cxbot）
  --no-start     安装完成后不启动服务
  -h, --help     显示帮助

升级时保留已有的 .env 和 data/ 目录；.env 会按 .env.example 重排、补齐注释和新增项，原文件备份为 .env.bak。
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix)
            PREFIX="${2:?--prefix 需要参数}"
            shift 2
            ;;
        --user)
            RUN_USER="${2:?--user 需要参数}"
            shift 2
            ;;
        --no-start)
            START_SERVICE=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "未知参数：$1" >&2
            usage
            exit 1
            ;;
    esac
done

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "请使用 root 运行：sudo bash $0" >&2
    exit 1
fi

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "未找到 $PYTHON，请先安装 Python 3.10+。" >&2
    exit 1
fi
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "需要 Python 3.10 或更高版本，当前：$("$PYTHON" -V 2>&1)" >&2
    exit 1
fi
if ! command -v base64 >/dev/null 2>&1; then
    echo "未找到 base64 命令，请安装 coreutils。" >&2
    exit 1
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

PAYLOAD_LINE="$(awk '/^__PAYLOAD_BELOW__$/ { print NR + 1; exit }' "$0")"
if [[ -z "$PAYLOAD_LINE" ]]; then
    echo "安装包损坏：未找到内嵌源码。" >&2
    exit 1
fi

echo "==> 解压源码"
tail -n +"$PAYLOAD_LINE" "$0" | base64 -d > "$TMP_DIR/source.zip"
"$PYTHON" - "$TMP_DIR/source.zip" "$TMP_DIR/source" <<'PY'
import sys
import zipfile

with zipfile.ZipFile(sys.argv[1]) as archive:
    archive.extractall(sys.argv[2])
PY

echo "==> 安装到 $PREFIX"
mkdir -p "$PREFIX"
cp -a "$TMP_DIR/source/." "$PREFIX/"

CREATED_ENV=0
if [[ ! -f "$PREFIX/.env" ]]; then
    cp "$PREFIX/.env.example" "$PREFIX/.env"
    CREATED_ENV=1
else
    echo "==> 整理 .env（按 .env.example 排序并补齐注释）"
    "$PYTHON" - "$PREFIX/.env" "$PREFIX/.env.example" "$PREFIX/.env.bak" <<'PY'
import re
import shutil
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
example_path = Path(sys.argv[2])
backup_path = Path(sys.argv[3])
env_text = env_path.read_text(encoding="utf-8")
example_text = example_path.read_text(encoding="utf-8")

values = {}
for line in env_text.splitlines():
    stripped = line.strip()
    if stripped and not stripped.startswith("#") and "=" in stripped:
        key, value = stripped.split("=", 1)
        values[key.strip()] = value

key_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=")
commented_re = re.compile(r"^#\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")

output = []
known = set()
added = 0
for line in example_text.splitlines():
    stripped = line.strip()
    if not stripped.startswith("#"):
        match = key_re.match(stripped)
        if match:
            key = match.group(1)
            known.add(key)
            if key in values:
                output.append(f"{key}={values[key]}")
            else:
                output.append(line.rstrip())
                added += 1
            continue
    match = commented_re.match(stripped)
    if match:
        key = match.group(1)
        known.add(key)
        if key in values:
            output.append(f"{key}={values[key]}")
            continue
    output.append(line)

unknown = [key for key in values if key not in known]
if unknown:
    output.extend(["", "# ---- 自定义配置 ----"])
    for key in unknown:
        output.append(f"{key}={values[key]}")

shutil.copy2(env_path, backup_path)
env_path.write_text("\n".join(output) + "\n", encoding="utf-8")
print(
    f"==> .env 已重排：保留 {len(values)} 个已有值，"
    f"补入 {added} 个默认项，保留 {len(unknown)} 个自定义项；备份：{backup_path}"
)
PY
fi

echo "==> 创建虚拟环境"
if [[ ! -x "$PREFIX/.venv/bin/python" ]]; then
    if ! "$PYTHON" -m venv "$PREFIX/.venv"; then
        echo "创建虚拟环境失败。Debian/Ubuntu 请先安装：apt install -y python3-venv" >&2
        exit 1
    fi
fi

echo "==> 安装依赖"
"$PREFIX/.venv/bin/python" -m pip install --upgrade pip
"$PREFIX/.venv/bin/python" -m pip install -r "$PREFIX/requirements.txt"

echo "==> 准备运行用户 $RUN_USER"
if ! id -u "$RUN_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "$PREFIX" \
        --shell /usr/sbin/nologin --user-group "$RUN_USER"
fi
RUN_GROUP="$(id -gn "$RUN_USER")"
chown -R "$RUN_USER":"$RUN_GROUP" "$PREFIX"
chmod 600 "$PREFIX/.env"
if [[ -f "$PREFIX/.env.bak" ]]; then
    chmod 600 "$PREFIX/.env.bak"
fi

echo "==> 写入 systemd 服务"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=${APP_NAME} QQ Bot
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${PREFIX}
Environment=PYTHONUNBUFFERED=1
ExecStart=${PREFIX}/.venv/bin/python ${PREFIX}/main.py
Restart=always
RestartSec=3
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

NEEDS_CONFIG=0
if [[ "$CREATED_ENV" -eq 1 ]]; then
    NEEDS_CONFIG=1
fi
if grep -Eq '^[[:space:]]*(QQ_APP_ID|DEEPSEEK_API_KEY)[[:space:]]*=[[:space:]]*$' "$PREFIX/.env"; then
    NEEDS_CONFIG=1
fi

ENABLED=0
if [[ "$START_SERVICE" -eq 0 ]]; then
    echo "==> 已安装，按 --no-start 未启动服务"
elif [[ "$NEEDS_CONFIG" -eq 1 ]]; then
    echo "==> 已安装，请先填写配置后再启动：$PREFIX/.env"
else
    echo "==> 启动服务并设置开机自启"
    systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
    systemctl restart "$SERVICE_NAME"
    ENABLED=1
fi

if command -v bwrap >/dev/null 2>&1; then
    echo "==> 已检测到 bubblewrap，命令沙箱可用"
else
    echo "提示：未检测到 bubblewrap，命令将以受限模式运行；建议安装：apt install -y bubblewrap"
fi

if "$PREFIX/.venv/bin/python" -c 'import playwright' >/dev/null 2>&1; then
    echo "==> 已检测到 Playwright，网页抓取会等待 JS 渲染"
else
    echo "提示：未安装 Playwright，网页抓取使用 curl；需要 JS 渲染可执行："
    echo "  $PREFIX/.venv/bin/pip install playwright && $PREFIX/.venv/bin/playwright install chromium"
fi

if [[ "$ENABLED" -eq 1 ]]; then
    AUTOSTART_LINE="已启用（崩溃后自动重启）"
else
    AUTOSTART_LINE="未启用，完成配置后执行 systemctl enable --now $SERVICE_NAME"
fi

cat <<EOF

安装完成。
  配置文件：$PREFIX/.env
  数据目录：$PREFIX/data
  查看状态：systemctl status $SERVICE_NAME
  实时日志：journalctl -u $SERVICE_NAME -f
  重启服务：systemctl restart $SERVICE_NAME
  开机自启：$AUTOSTART_LINE
EOF

if [[ "$NEEDS_CONFIG" -eq 1 ]]; then
    cat <<EOF

下一步：
  1) 编辑 $PREFIX/.env，填写 QQ_APP_ID、QQ_CLIENT_SECRET、DEEPSEEK_API_KEY
  2) 启动并设置开机自启：systemctl enable --now $SERVICE_NAME
EOF
elif [[ "$ENABLED" -eq 0 ]]; then
    cat <<EOF

下一步：
  启动并设置开机自启：systemctl enable --now $SERVICE_NAME
EOF
fi

exit 0

__PAYLOAD_BELOW__
