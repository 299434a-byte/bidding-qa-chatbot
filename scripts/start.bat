@echo off
setlocal
title 招投标智能问答 - 启动中

rem ====================================================================
rem 【编码说明】本脚本用 GBK（CP936）编码保存，且不切换代码页：
rem   中文 Windows 的 cmd 默认就按 CP936 解析批处理，GBK 可逐字节稳定解析；
rem   若用 UTF-8 + chcp 65001，cmd 存在偶发的解析错位缺陷（实测约 1% 会把
rem   上面的注释行拆成命令执行），故统一采用 GBK。
rem 一键启动脚本（双击运行）：
rem   第 1 步 预检 —— venv / node_modules / .env 缺谁提示谁，不往下跑；
rem   第 2 步 启动 —— 分别开「新窗口」跑后端与前端（cmd /k，本脚本退出后
rem                   它们继续运行，方便随时看日志）；端口已有监听则跳过，
rem                   因此重复双击是安全的；
rem   第 3 步 等待 —— 轮询 /api/health 直到 200（最多 60 秒），
rem                   就绪后打印 kb_source 与知识库点数；
rem   第 4 步 打开 —— 自动打开浏览器 localhost:3000。
rem
rem 【为什么用 ping 计时而不是 timeout】
rem   timeout 在「输入被重定向 / 非交互」的环境下会直接报错不等待；
rem   ping 本机两次的耗时约 1 秒，在任何环境下都能用。
rem ====================================================================

set "ROOT=%~dp0.."
set "HEALTH_URL=http://127.0.0.1:8000/api/health"

rem ---------- 第 1 步：预检（缺谁提示谁） ----------
if not exist "%ROOT%\.venv\Scripts\python.exe" (
    echo [预检失败] 没找到后端虚拟环境：%ROOT%\.venv
    echo           在项目根目录执行：python -m venv .venv
    echo           然后安装依赖：.venv\Scripts\python.exe -m pip install -r requirements.txt
    goto :endfail
)
if not exist "%ROOT%\frontend\node_modules" (
    echo [预检失败] 没找到 frontend\node_modules
    echo           在 frontend 目录执行：npm install
    goto :endfail
)
if not exist "%ROOT%\.env" (
    echo [预检失败] 没找到根目录 .env 配置文件
    echo           复制模板：copy .env.example .env ，然后按需填写里面的值
    goto :endfail
)

rem ---------- 第 2 步：启动前后端（端口已有监听就跳过） ----------
netstat -ano | findstr "LISTENING" | findstr ":8000 " >nul
if errorlevel 1 (
    start "招投标-后端:8000" cmd /k "cd /d "%ROOT%\backend" && "%ROOT%\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
    echo [启动] 后端已在新窗口打开（uvicorn 127.0.0.1:8000）
) else (
    echo [跳过] 8000 端口已有服务在监听（若是上次残留的进程，先运行 stop.bat）
)

netstat -ano | findstr "LISTENING" | findstr ":3000 " >nul
if errorlevel 1 (
    start "招投标-前端:3000" cmd /k "cd /d "%ROOT%\frontend" && npm run dev"
    echo [启动] 前端已在新窗口打开（Next.js dev 127.0.0.1:3000）
) else (
    echo [跳过] 3000 端口已有服务在监听
)

rem ---------- 第 3 步：轮询健康检查（最多 60 秒） ----------
echo [等待] 正在等待后端就绪（最多 60 秒）...
set /a waited=0
:poll
ping -n 2 127.0.0.1 >nul
set /a waited+=1
curl -s -o nul -w "%%{http_code}" "%HEALTH_URL%" 2>nul | findstr "200" >nul
if not errorlevel 1 goto :healthy
if %waited% geq 60 goto :failed
goto :poll

rem ---------- 就绪：打印关键信息并打开浏览器 ----------
:healthy
echo [就绪] 后端健康检查通过。
"%ROOT%\.venv\Scripts\python.exe" -c "import json,urllib.request as u;h=json.load(u.urlopen('http://127.0.0.1:8000/api/health'));k=json.load(u.urlopen('http://127.0.0.1:8000/api/kb/stats'));print('       kb_source = ' + str(h.get('kb_source')));print('       知识库点数 = ' + str(k.get('points')))"
echo [打开] 正在打开浏览器 http://localhost:3000
start "" "http://localhost:3000"
echo.
echo 全部启动完成。停止时请双击 scripts\stop.bat
pause
exit /b 0

rem ---------- 超时：给出三种最常见原因 ----------
:failed
echo.
echo [失败] 60 秒内没有等到 /api/health 返回 200。最常见的三种原因：
echo   1^) 8000 端口被占用 —— 执行 netstat -ano ^| findstr :8000 查看占用者；
echo      若是上次残留的后端，先双击 scripts\stop.bat 释放端口再重试。
echo   2^) Qdrant 数据目录被另一个进程占用 —— 向量库是本地嵌入式模式，
echo      对 data\qdrant 目录单进程独占；残留进程没退干净时新后端打不开库。
echo   3^) venv 依赖没装全 —— 在项目根目录执行：
echo      .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
echo 后端窗口仍保持打开，可查看其中的报错信息；排障后重新双击本脚本。
pause
exit /b 1

rem ---------- 预检失败出口 ----------
:endfail
echo.
echo 预检未通过，未启动任何服务。解决上述问题后重新双击本脚本。
pause
exit /b 1
