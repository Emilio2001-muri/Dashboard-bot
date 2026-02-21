@echo off
title NEXUS Data Pusher
echo ============================================
echo   NEXUS Trading Dashboard - Data Pusher
echo   Pushing MT5 + API data to Supabase...
echo ============================================
echo.
cd /d "%~dp0"
python pusher.py
pause
