@echo off
cd /d %~dp0
rem 解释器归口：有 .venv 就用它（与线上同一套包版本），没有才退回全局
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  set "PY=python"
  echo [提示] 没找到 .venv，这次先用全局 Python。它被好几个服务共用、容易被顶坏，建议双击 1_安装依赖.bat 建独立环境。
)
if not exist input mkdir input
echo 监控已启动：把分单文件拖进 input 文件夹即自动识别，关掉本窗口停止。
echo.
%PY% watch_folder.py input
pause
