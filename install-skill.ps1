<#
.SYNOPSIS
    把 mubu-editor 安装成 AI Agent 的 Skill。
.DESCRIPTION
    默认用「链接」方式（改仓库立即生效，适合开发）；-Copy 则复制一份自包含的副本。
    安装目标为 <用户目录>\.<agent>\skills\<名称>。安装后 Agent 会读取该目录下的 SKILL.md。

    依赖：本 Skill 底层是 Python CLI，装完 Skill 后还需安装 Python 依赖：
        pip install -r requirements.txt
.EXAMPLE
    ./install-skill.ps1
    链接安装到 codex（默认）

.EXAMPLE
    ./install-skill.ps1 -Agents codex,claude -Name mubu-editor
    同时装到多个 Agent

.EXAMPLE
    ./install-skill.ps1 -Copy
    复制安装（自包含副本，不含 .git / 缓存 / 本机凭据目录）
#>
[CmdletBinding()]
param(
    [string[]]$Agents = @('codex'),
    [string]$Name = 'mubu-editor',
    [switch]$Copy
)

$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$mode = if ($Copy) { '复制' } else { '链接' }

Write-Host "mubu-editor Skill 安装（$mode 方式）" -ForegroundColor Cyan

foreach ($agent in $Agents) {
    $dest = Join-Path $env:USERPROFILE ".${agent}\skills\${Name}"
    $parent = Split-Path $dest -Parent
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }

    # 先清理已有安装（若是 junction 只删链接，不动目标）
    if (Test-Path $dest) {
        cmd /c rmdir "$dest" 2>$null | Out-Null
        if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
        Write-Host "  清理旧安装: $dest"
    }

    if ($Copy) {
        $xd = @('.git', '__pycache__', '.pytest_cache', '.ruff_cache', 'config')
        $args = @($repo, $dest, '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/XD') + $xd
        & robocopy @args | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "robocopy 失败，退出码 $LASTEXITCODE" }
    } else {
        cmd /c mklink /J "$dest" "$repo" | Out-Null
        if (-not (Test-Path (Join-Path $dest 'SKILL.md'))) { throw "链接创建失败: $dest" }
    }
    Write-Host "  ✅ $agent -> $dest" -ForegroundColor Green
}

Write-Host ""
Write-Host "下一步：安装 Python 依赖" -ForegroundColor Yellow
Write-Host "  pip install -r `"$repo\requirements.txt`""
Write-Host "并配置凭据（环境变量 MUBU_PHONE / MUBU_PASSWORD，或 config/.env.mubu）。"
