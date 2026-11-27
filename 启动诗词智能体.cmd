@echo off
setlocal EnableExtensions
chcp 65001 >nul
pushd "%~dp0"

set "poetPythonOption="
set "poetPython=%~dp0.venv\Scripts\python.exe"
if exist "%poetPython%" goto run_server

set "poetPython=%USERPROFILE%\miniconda3\python.exe"
if not exist "%poetPython%" goto try_launcher
"%poetPython%" -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto run_server

:try_launcher
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 goto try_path
set "poetPython=py.exe"
set "poetPythonOption=-3"
goto run_server

:try_path
python.exe -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 goto no_python
set "poetPython=python.exe"

:run_server
"%poetPython%" %poetPythonOption% -c "import pydantic, webrtcvad, imageio_ffmpeg" >nul 2>&1
if errorlevel 1 goto missing_dependencies
"%poetPython%" %poetPythonOption% -B -X utf8 "%~dp0server.py"
set "poetExit=%errorlevel%"
if "%poetExit%"=="0" exit /b 0
echo 诗词智能体启动失败，返回码为 %poetExit%。请查看上方的错误信息。
pause
exit /b %poetExit%

:missing_dependencies
echo 首次运行请先安装依赖：
echo "%poetPython%" %poetPythonOption% -m pip install -r "%~dp0requirements.txt"
pause
exit /b 1

:no_python
echo 找不到可运行的 Python 3.10 或更新版本。请先安装 Python 后再打开诗词智能体。
pause
exit /b 1
