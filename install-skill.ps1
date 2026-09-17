<#
.SYNOPSIS
    把 mubu-editor 安装成 AI Agent 的 Skill。
.DESCRIPTION
    Skill 没有包管理器："安装"= 把含 SKILL.md 的目录放进 Agent 会扫描的 skills
    目录。本脚本替你完成这一步：默认建目录联接（改仓库立即生效），-Copy 则复制
    一份自包含副本。

    目标目录由你决定，脚本不预设任何 Agent。
.EXAMPLE
    ./install-skill.ps1 -Destination "$env:USERPROFILE\.codex\skills\mubu-editor"

.EXAMPLE
    ./install-skill.ps1 -Destination "$env:USERPROFILE\.claude\skills\mubu-editor" -Copy
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, HelpMessage = '安装到哪个目录，例如 $env:USERPROFILE\.codex\skills\mubu-editor')]
    [string]$Destination,
    [switch]$Copy
)

$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$mode = if ($Copy) { '复制' } else { '链接' }

# 归一化为绝对路径
$dest = [System.IO.Path]::GetFullPath($Destination)
$repoFull = [System.IO.Path]::GetFullPath($repo)

# ---- 安全护栏：绝不递归删除危险路径 ----
$home_ = [System.IO.Path]::GetFullPath($env:USERPROFILE)
if ($dest -eq $repoFull) { throw "拒绝：目标不能是仓库本身（$dest）" }
if ($dest -eq [System.IO.Path]::GetPathRoot($dest)) { throw "拒绝：目标是磁盘根目录（$dest）" }
if ($dest -eq $home_ -or $home_.StartsWith($dest.TrimEnd('\') + '\')) { throw "拒绝：目标目录不安全（$dest）" }

Write-Host "mubu-editor Skill 安装（$mode 方式）-> $dest" -ForegroundColor Cyan

$parent = Split-Path $dest -Parent
if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }

# 清理旧安装（junction 用 rmdir 只删链接）
if (Test-Path $dest) {
    cmd /c rmdir "$dest" 2>$null | Out-Null
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    Write-Host "  已清理旧安装"
}

if ($Copy) {
    $excl = @('.git', '__pycache__', '.pytest_cache', '.ruff_cache', 'config')
    & robocopy $repoFull $dest /E /NFL /NDL /NJH /NJS /NP /XD @excl | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy 失败，退出码 $LASTEXITCODE" }
} else {
    cmd /c mklink /J "$dest" "$repoFull" | Out-Null
}

$skill = Join-Path $dest 'SKILL.md'
if (-not (Test-Path $skill)) { throw "安装失败：$dest 下没有 SKILL.md" }

Write-Host "  ✅ 完成" -ForegroundColor Green
Write-Host ""
Write-Host "下一步：pip install -r `"$repoFull\requirements.txt`""
Write-Host "并配置凭据（环境变量 MUBU_PHONE / MUBU_PASSWORD，或 config/.env.mubu）。"
