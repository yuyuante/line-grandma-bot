@echo off
chcp 65001 >nul
title LINE 長輩圖自動回覆程式 v2.4.1
cd /d "%~dp0"
python auto_reply.py
pause
