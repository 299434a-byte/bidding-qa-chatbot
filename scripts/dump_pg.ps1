<#
.SYNOPSIS
  导出本机 PostgreSQL 的 chatbot 库到 data\backup\chatbot-<日期>.sql，并校验行数。

.DESCRIPTION
  为什么要有这个脚本：部署服务器访问不到本项目的内网数据源（192.168.10.120 的 MySQL），
  业务数据只能靠「本机 dump → 拷文件 → 目标机 restore」搬运。搬完后还要在新环境
  重建知识库（python -m app.ingest），不要把 data\qdrant 目录拷到不同版本的环境里。

  用法（仓库根目录，PowerShell）：
    powershell -ExecutionPolicy Bypass -File scripts\dump_pg.ps1

  连接参数（PG_HOST / PG_PORT / PG_USER / PG_PASSWORD / PG_DATABASE）从仓库根目录 .env 读取，
  密码只进本进程环境变量（PGPASSWORD），不拼进命令行。
#>
[CmdletBinding()]
param(
    # 行数校验基准：procurement_notices 与 projects 搬运前各应有的行数
    [int]$ExpectedRows = 933
)

$ErrorActionPreference = "Stop"

# ---- 仓库根目录（本脚本位于 <root>\scripts\ 下） ----
$Root = Split-Path -Parent $PSScriptRoot

# ---- 读根目录 .env 里的 PG_* 连接参数 ----
$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "[错误] 找不到 $envFile（PG_* 连接参数定义在里面）" -ForegroundColor Red
    exit 1
}
$pg = @{}
foreach ($line in Get-Content $envFile -Encoding UTF8) {
    $line = $line.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { continue }
    $idx = $line.IndexOf("=")
    if ($idx -lt 1) { continue }
    $key = $line.Substring(0, $idx).Trim()
    $val = $line.Substring($idx + 1).Trim()
    $pg[$key] = $val
}
foreach ($need in @("PG_HOST", "PG_PORT", "PG_USER", "PG_PASSWORD", "PG_DATABASE")) {
    if (-not $pg.ContainsKey($need)) {
        Write-Host "[错误] .env 里缺少 $need" -ForegroundColor Red
        exit 1
    }
}

# ---- 定位 pg_dump / psql：优先 PATH，其次 PostgreSQL 默认安装位置 ----
function Find-PgTool([string]$Name) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $guess = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\$Name.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($guess) { return $guess.FullName }
    return $null
}
$pgDump = Find-PgTool "pg_dump"
$psql = Find-PgTool "psql"
if (-not $pgDump -or -not $psql) {
    Write-Host "[错误] 找不到 pg_dump / psql：请安装 PostgreSQL，或把其 bin 目录加入 PATH" -ForegroundColor Red
    exit 1
}

# 密码只写进本进程环境变量；命令行参数会短暂暴露给同机进程列表，不能走那里。
$env:PGPASSWORD = $pg["PG_PASSWORD"]

# ---- 导出 ----
$backupDir = Join-Path $Root "data\backup"
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
$stamp = Get-Date -Format "yyyy-MM-dd"
$outFile = Join-Path $backupDir "chatbot-$stamp.sql"

Write-Host "导出 $($pg['PG_DATABASE']) @ $($pg['PG_HOST']):$($pg['PG_PORT']) → $outFile"
# --no-owner --no-privileges：去掉原库的属主与授权信息。目标机的数据库账号很可能不同，
# 带着属主信息导入会在 restore 时因「角色不存在 / 权限不足」直接失败。
& $pgDump -h $pg["PG_HOST"] -p $pg["PG_PORT"] -U $pg["PG_USER"] -d $pg["PG_DATABASE"] `
    --no-owner --no-privileges -f $outFile
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] pg_dump 退出码 $LASTEXITCODE" -ForegroundColor Red
    exit 1
}
$sizeMB = [math]::Round((Get-Item $outFile).Length / 1MB, 2)
Write-Host "导出完成：$outFile（$sizeMB MB）"

# ---- 行数校验：dump 出来的文件是否真的包含应有的数据 ----
Write-Host "行数校验（期望各 $ExpectedRows 行）："
$failed = $false
foreach ($table in @("procurement_notices", "projects")) {
    $count = & $psql -h $pg["PG_HOST"] -p $pg["PG_PORT"] -U $pg["PG_USER"] -d $pg["PG_DATABASE"] `
        -t -A -c "SELECT COUNT(*) FROM $table"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [失败] $table 查询出错：$count" -ForegroundColor Red
        $failed = $true
        continue
    }
    $count = "$count".Trim()
    if ($count -eq "$ExpectedRows") {
        Write-Host "  [OK] $table = $count" -ForegroundColor Green
    } else {
        Write-Host "  [失败] $table = $count，期望 $ExpectedRows" -ForegroundColor Red
        $failed = $true
    }
}

if ($failed) {
    Write-Host "校验未通过：dump 文件不可用于搬库，先排查数据源。" -ForegroundColor Red
    exit 1
}
Write-Host "全部通过：把 $outFile 拷到目标机，用 scripts\restore_pg.ps1 导入。" -ForegroundColor Green
exit 0
