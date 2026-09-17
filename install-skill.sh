#!/usr/bin/env sh
# 把 mubu-editor 安装成 AI Agent 的 Skill。
#
# Skill 没有包管理器："安装"= 把含 SKILL.md 的目录放进 Agent 会扫描的
# skills 目录。本脚本替你完成这一步（建链接，或复制一份自包含副本）。
#
#   ./install-skill.sh <目标目录>            # 链接安装（默认）
#   ./install-skill.sh <目标目录> copy       # 复制安装
#
# 例（目标目录由你决定，脚本不预设任何 Agent）：
#   ./install-skill.sh ~/.codex/skills/mubu-editor
#   ./install-skill.sh ~/.claude/skills/mubu-editor
#   ./install-skill.sh .claude/skills/mubu-editor copy
#
# 底层是 Python CLI，装完还要装依赖：pip install -r requirements.txt
set -eu

DEST="${1:-}"
MODE="${2:-link}"

if [ -z "$DEST" ]; then
    echo "用法: $0 <目标目录> [link|copy]" >&2
    echo "例:   $0 ~/.codex/skills/mubu-editor" >&2
    exit 1
fi

case "$MODE" in
    link|copy) ;;
    *) echo "第二个参数只能是 link 或 copy" >&2; exit 1 ;;
esac

REPO="$(cd "$(dirname "$0")" && pwd)"

# 展开 ~
case "$DEST" in
    '~'/*) DEST="$HOME/${DEST#'~/'}" ;;
esac

# 归一化为绝对路径
_parent="$(dirname "$DEST")"
[ "$_parent" = "." ] && _parent="$(pwd)"
mkdir -p "$_parent"
DEST="$(cd "$_parent" && pwd)/$(basename "$DEST")"

# ---- 安全护栏：绝不 rm -rf 掉危险路径 ----
case "$DEST" in
    "/"|"$HOME"|"$HOME/"*) 
        echo "拒绝：目标目录不安全（$DEST）" >&2; exit 1 ;;
esac
if [ "$DEST" = "$REPO" ]; then
    echo "拒绝：目标不能是仓库本身（$DEST）" >&2; exit 1
fi

# 清理旧安装：对符号链接 rm 只删链接本身，不会跟随
if [ -e "$DEST" ] || [ -L "$DEST" ]; then rm -rf "$DEST"; fi

if [ "$MODE" = "copy" ]; then
    mkdir -p "$DEST"
    tar -C "$REPO" \
        --exclude='.git' --exclude='__pycache__' --exclude='.pytest_cache' \
        --exclude='.ruff_cache' --exclude='config' \
        -cf - . | tar -C "$DEST" -xf -
    echo "✅ 已复制安装到 $DEST"
else
    ln -s "$REPO" "$DEST"
    echo "✅ 已链接安装：$DEST  ->  $REPO"
fi

[ -f "$DEST/SKILL.md" ] || { echo "安装失败：$DEST 下没有 SKILL.md" >&2; exit 1; }
echo
echo "下一步：pip install -r $REPO/requirements.txt"
echo "并配置凭据（MUBU_PHONE / MUBU_PASSWORD，或 config/.env.mubu）。"
