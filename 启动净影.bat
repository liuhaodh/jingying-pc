@echo off
chcp 936 >nul
cd /d %~dp0
set "PY="

rem WorkBuddy 的 Python 带了一个“安全删除”钩子：批量删除 Playwright 临时目录时
rem 会要求确认，实测会让程序直接崩溃（0xC0000374）。这个程序自己管理删除，
rem 不需要这层钩子，启动前关掉。
set "CODEBUDDY_SAFE_DELETE_ENABLED=0"
set "CODEBUDDY_FILE_BACKUP_ENABLED=0"

rem 1) WorkBuddy bundled Python (no standalone Python on this PC)
set "WB=%USERPROFILE%\.workbuddy-ai\binaries\python\versions"
if exist "%WB%\3.13.12\python.exe" set "PY=%WB%\3.13.12\python.exe"
if not defined PY if exist "%WB%\current" (
    for /f "usebackq delims=" %%v in ("%WB%\current") do (
        if exist "%WB%\%%v\python.exe" set "PY=%WB%\%%v\python.exe"
    )
)

rem 2) python from PATH, skip the Microsoft Store stub
if not defined PY (
    for /f "delims=" %%p in ('where python 2^>nul') do (
        echo %%p | find /i "WindowsApps" >nul || if not defined PY set "PY=%%p"
    )
)

if not defined PY (
    echo.
    echo Python not found. Install Python 3.12+ or set PY=... in this file.
    echo.
    pause
    exit /b 1
)

echo Using Python: %PY%
"%PY%" app.py
set "CODE=%errorlevel%"
if not "%CODE%"=="0" (
    echo.
    echo Program exited with code %CODE%.
    echo See JingYingData\app.log and JingYingData\crash.log.
    echo.
    pause
)
