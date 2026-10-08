@echo off
chcp 65001 >nul
setlocal
rem ============================================================
rem   cc-securitycheck 启动脚本（Windows）
rem   配置写在同目录 .env 里（复制 .env.example 改）
rem ============================================================
rem   如需临时覆盖，取消下面某行注释即可（环境变量优先于 .env）：
rem set PROXY_PORT=8008
rem set UPSTREAM=https://api.deepseek.com/anthropic
rem set MODEL_OVERRIDE=deepseek-v4-flash[1M]
rem ============================================================

python "%~dp0proxy.py"
echo.
pause
