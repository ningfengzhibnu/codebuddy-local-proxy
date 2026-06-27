@echo off
chcp 65001 >nul
title CodeBuddy Local Proxy
echo ============================================
echo   CodeBuddy Local Proxy
echo   管理后台: http://127.0.0.1:19090/admin/dashboard
echo ============================================
python local_codebuddy_proxy.py
pause
