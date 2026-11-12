@echo off
chcp 936 >nul
cd /d %~dp0
echo 正在安装依赖（requests / PySide6 / playwright）...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo 安装失败，请检查 Python 是否已加入 PATH。
  pause
  exit /b 1
)
echo.
echo 依赖安装完成。双击「启动净影.bat」即可运行。
pause