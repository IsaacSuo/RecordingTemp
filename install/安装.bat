@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "SETUP=%~dp0mvsdk-python\install\setup_recorder.py"
if not exist "%SETUP%" set "SETUP=%~dp0setup_recorder.py"

set "PY="
where py >nul 2>nul && py -3.14 -c "import sys" >nul 2>nul && set "PY=py -3.14"
if not defined PY (
  python -c "import sys; raise SystemExit(0 if sys.version_info[:2]==(3,14) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo.
  echo 未找到 Python 3.14（64 位）。
  echo 请先按“安装说明.txt”第 4 步安装 Python 3.14，安装时勾选 Add python.exe to PATH，然后重新双击本文件。
  echo.
  pause
  exit /b 1
)

echo 使用 %PY%
%PY% "%SETUP%" %*
echo.
pause
