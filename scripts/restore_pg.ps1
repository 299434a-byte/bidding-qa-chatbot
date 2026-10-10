<#
.SYNOPSIS
  在目标机上创建 chatbot 库并导入 dump 文件，随后校验行数。

.DESCRIPTION
  配套 scripts\dump_pg.ps1 使用：先把 data\backup\chatbot-<日期>.sql 拷到目标机，
  再在仓库根目录执行：

    powershell -ExecutionPolicy Bypass -File scripts\restore_pg.ps1
    # 或显式指定文件：
    powershell -ExecutionPolicy Bypass -File scripts\restore_pg.ps1 -BackupFile D:\tmp\chatbot-2026-10-11.sql

  安全策略：
    · 目标库不存在 → createdb 创建后导入；
    · 目标库已存在且为空（没有两张业务表）→ 直接导入；
    · 目标库已有业务表 → 拒绝导入并退出。dump 没有 --clean，覆盖已有表必然报
      relation already exists；强行 drop 又可能毁掉目标机上的既有数据，所以交给人决定。

  导入完成后校验 procurement_notices / projects 行数，与 dump 侧同一基准比对。
#>
[CmdletBinding()]
param(
    # dump 文件路径；缺省取 data\backup\ 下最新的 chatbot-*.sql
    [string]$BackupFile = "",
    # 行数校验基准：必须与导出时（dump_pg.ps1 -ExpectedRows）一致
    [int]$ExpectedRows = 933
)

$ErrorActionPreference = "Stop"

# ---- 仓库根目录（本脚本位于 <root>\scripts\ 下） ----
$Root = Split-Path -Parent $PSScriptRoot

# ---- 读根目录 .env 里的 PG_* 连接参数（与 dump_pg.ps1 同一套约定） ----
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

# ---- 定位 psql / createdb：优先 PATH，其次 PostgreSQL 默认安装位置 ----
function Find-PgTool([string]$Name) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $guess = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\$Name.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($guess) { return $guess.FullName }
    return $null
}
$psql = Find-PgTool "psql"
$createdb = Find-PgTool "createdb"
if (-not $psql -or -not $createdb) {
    Write-Host "[错误] 找不到 psql / createdb：请安装 PostgreSQL，或把其 bin 目录加入 PATH" -ForegroundColor Red
    exit 1
}

# 密码只写进本进程环境变量；命令行参数会短暂暴露给同机进程列表，不能走那里。
$env:PGPASSWORD = $pg["PG_PASSWORD"]

# ---- 确定要导入的 dump 文件 ----
if ($BackupFile -eq "") {
    $latest = Get-ChildItem (Join-Path $Root "data\backup\chatbot-*.sql") -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $latest) {
        Write-Host "[错误] data\backup\ 下没有 chatbot-*.sql，请先在数据源机器跑 scripts\dump_pg.ps1" -ForegroundColor Red
        exit 1
    }
    $BackupFile = $latest.FullName
}
if (-not (Test-Path $BackupFile)) {
    Write-Host "[错误] 找不到 dump 文件：$BackupFile" -ForegroundColor Red
    exit 1
}
$dbName = $pg["PG_DATABASE"]

# ---- 检查目标库状态，决定能不能安全导入 ----
# 连 postgres 库查库是否存在（新建库前不能连它本身）。
$dbExists = & $psql -h $pg["PG_HOST"] -p $pg["PG_PORT"] -U $pg["PG_USER"] -d postgres `
    -t -A -c "SELECT 1 FROM pg_database WHERE datname = '$dbName'"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] 连不上 PostgreSQL（$($pg['PG_HOST']):$($pg['PG_PORT'])），确认服务与 .env 配置" -ForegroundColor Red
    exit 1
}
$dbExists = "$dbExists".Trim() -eq "1"

if ($dbExists) {
    # 库已存在：看两张业务表是否已存在。有表 = 有数据风险，拒绝覆盖。
    $tables = & $psql -h $pg["PG_HOST"] -p $pg["PG_PORT"] -U $pg["PG_USER"] -d $dbName `
        -t -A -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN ('procurement_notices', 'projects')"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[错误] 查询 $dbName 库状态失败：$tables" -ForegroundColor Red
        exit 1
    }
    if ("$tables".Trim() -ne "0") {
        Write-Host "[拒绝] 库 $dbName 已存在且包含业务表，直接导入会与现有数据冲突。" -ForegroundColor Red
        Write-Host "       确认要覆盖请先手工删除该库（DROP DATABASE $dbName;），再重跑本脚本。" -ForegroundColor Red
        exit 1
    }
    Write-Host "库 $dbName 已存在但为空，直接导入。"
} else {
    Write-Host "创建数据库 $dbName …"
    & $createdb -h $pg["PG_HOST"] -p $pg["PG_PORT"] -U $pg["PG_USER"] $dbName
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[错误] createdb 退出码 $LASTEXITCODE" -ForegroundColor Red
        exit 1
    }
}

# ---- 导入 ----
# ON_ERROR_STOP=1：SQL 中途出错立即停止并返回非零，不带着半截数据继续。
Write-Host "导入 $BackupFile → $dbName …"
& $psql -h $pg["PG_HOST"] -p $pg["PG_PORT"] -U $pg["PG_USER"] -d $dbName -v ON_ERROR_STOP=1 -f $BackupFile
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] psql 导入退出码 $LASTEXITCODE（已开启 ON_ERROR_STOP，导入在报错处停止）" -ForegroundColor Red
    exit 1
}

# ---- 行数校验：与 dump 侧同一基准，两头对上才算搬成功 ----
Write-Host "行数校验（期望各 $ExpectedRows 行）："
$failed = $false
foreach ($table in @("procurement_notices", "projects")) {
    $count = & $psql -h $pg["PG_HOST"] -p $pg["PG_PORT"] -U $pg["PG_USER"] -d $dbName `
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
    Write-Host "校验未通过：数据不完整，不要继续启动服务。" -ForegroundColor Red
    exit 1
}
Write-Host "恢复完成。接下来重建知识库：cd backend && python -m app.ingest（不要拷贝 data\qdrant）" -ForegroundColor Green
exit 0
