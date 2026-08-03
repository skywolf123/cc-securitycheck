@echo off
chcp 65001 >nul
setlocal
rem ============================================================
rem   cc-securitycheck 启动脚本（Windows）
rem   常用配置改这里即可；留空则用默认值
rem ============================================================
set PROXY_PORT=8008
set UPSTREAM=https://api.deepseek.com/anthropic
rem 留空 = 透传 Claude Code 里的 model；
rem 也可强制统一，如 set MODEL_OVERRIDE=deepseek-v4-flash[1M]
set MODEL_OVERRIDE=
rem ============================================================

python "%~dp0proxy.py"
echo.
pause
