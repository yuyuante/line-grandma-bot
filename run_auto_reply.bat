@echo off
chcp 65001 >nul
title LINE 長輩圖自動回覆程式 v2.6.0
cd /d "%~dp0"
python auto_reply.py
rem 結束碼 3 = 收到遠端停止指令，直接關閉視窗；其餘情況停在畫面上方便看錯誤訊息
if %errorlevel%==3 exit
pause
