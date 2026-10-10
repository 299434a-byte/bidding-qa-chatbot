@echo off
setlocal
title 招投标智能问答 - 停止中

rem ====================================================================
rem 【编码说明】本脚本用 GBK（CP936）编码保存，且不切换代码页：
rem   中文 Windows 的 cmd 默认就按 CP936 解析批处理，GBK 可逐字节稳定解析；
rem   若用 UTF-8 + chcp 65001，cmd 存在偶发的解析错位缺陷（实测约 1% 会把
rem   上面的注释行拆成命令执行），故统一采用 GBK。
rem 一键停止脚本（双击运行）：
rem   1) 按端口找进程 —— netstat 找出 8000 / 3000 上处于 LISTENING 的 PID，
rem      逐个 taskkill /F /T 结束（/T 连带子进程，避免留下孤儿）；
rem   2) 关窗口 —— 顺带按窗口标题关闭 start.bat 打开的两个控制台窗口；
rem   3) 复核 —— 等 1 秒后再用 netstat 确认两个端口都没有监听才算完成。
rem
rem 【为什么必须干净停止，而不是直接关窗口】
rem   向量库 Qdrant 是「本地嵌入式模式」，对 data\qdrant 目录单进程独占：
rem   进程若没干净退出、目录锁残留，下次 start.bat 启动的后端就打不开库
rem   （这正是 start.bat 失败提示里的第 2 号原因）。
rem   按端口定位监听进程 → 结束它 → 确认端口释放，才能把目录锁一并还回去。
rem ====================================================================

echo 正在停止 8000 / 3000 端口上的进程...

for %%P in (8000 3000) do (
    for /f "tokens=5" %%I in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":%%P "') do (
        echo   结束进程 PID %%I（监听 %%P）
        taskkill /PID %%I /F /T >nul 2>&1
    )
)

rem 关掉 start.bat 开的两个窗口（按窗口标题匹配，没开就静默忽略）
taskkill /FI "WINDOWTITLE eq 招投标-后端*" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq 招投标-前端*" /F >nul 2>&1

rem 给操作系统一点时间释放端口，再复核
ping -n 2 127.0.0.1 >nul
netstat -ano | findstr "LISTENING" | findstr ":8000 :3000" >nul
if errorlevel 1 (
    echo [完成] 8000 与 3000 均已无监听，端口已释放。
) else (
    echo [注意] 仍有端口未释放，执行 netstat -ano ^| findstr LISTENING 查看占用 PID，
    echo        再 taskkill /PID 那个PID /F 强制结束。
)
pause
