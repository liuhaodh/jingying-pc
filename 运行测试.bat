@echo off
chcp 936 >nul
cd /d "%~dp0"
python run_tests.py
if errorlevel 1 (
  echo.
  echo 测试失败，请查看上面的输出。
) else (
  echo.
  echo 全部测试通过。
)
pause