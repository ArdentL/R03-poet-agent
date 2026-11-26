@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "storyPython=%USERPROFILE%\miniconda3\python.exe"
set "storyPythonOption="
if not exist "%storyPython%" goto try_launcher
"%storyPython%" -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto run_server

:try_launcher
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 goto try_path
set "storyPython=py.exe"
set "storyPythonOption=-3"
goto run_server

:try_path
python.exe -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 goto no_python
set "storyPython=python.exe"

:run_server
"%storyPython%" %storyPythonOption% -B -X utf8 "%~dp0server.py"
set "storyExit=%errorlevel%"
if "%storyExit%"=="0" exit /b 0
echo 故事智能体启动失败，返回码为 %storyExit%。请查看上方的错误信息。
pause
exit /b %storyExit%

:no_python
echo 找不到可运行的 Python 3.10 或更新版本。请先安装 Python 后再打开故事智能体。
pause
exit /b 1
