#!/usr/bin/env sh
# 把 mubu-editor 安装成 AI Agent 的 Skill（Linux / macOS）
#
#   ./install-skill.sh                    # 链接安装到 codex（默认）
#   AGENTS=codex,claude ./install-skill.sh
#   MODE=copy ./install-skill.sh          # 复制安装
#
# 安装后 Agent 会读取 <目标目录>/SKILL.md；底层是 Python CLI，还需：
#   pip install -r requirements.txt
set -eu

AGENTS="${AGENTS:-codex}"
NAME="${NAME:-mubu-editor}"
MODE="${MODE:-link}"
REPO="$(cd "$(dirname "$0")" && pwd)"

case "$MODE" in link|copy) ;; *) echo "MODE 只能是 link 或 copy" >&2; exit 1 ;; esac

echo "mubu-editor Skill 安装（$MODE 方式）"

for agent in $(echo "$AGENTS" | tr ',' ' '); do
    dest="$HOME/.$agent/skills/$NAME"
    mkdir -p "$(dirname "$dest")"

    # 清理旧安装：对符号链接 rm 只删链接本身，不会跟随
    [ -e "$dest" ] || [ -L "$dest" ] && rm -rf "$dest"

    if [ "$MODE" = "copy" ]; then
        mkdir -p "$dest"
        # 排除版本库、缓存与本机凭据目录
        tar -C "$REPO"             --exclude='.git' --exclude='__pycache__' --exclude='.pytest_cache'             --exclude='.ruff_cache' --exclude='config'             -cf - . | tar -C "$dest" -xf -
    else
        ln -s "$REPO" "$dest"
    fi

    if [ ! -f "$dest/SKILL.md" ]; then echo "安装失败: $dest" >&2; exit 1; fi
    printf '  ✅ %s -> %s\n' "$agent" "$dest"
done

echo
echo "下一步：pip install -r $REPO/requirements.txt"
echo "并配置凭据（MUBU_PHONE / MUBU_PASSWORD，或 config/.env.mubu）。"
