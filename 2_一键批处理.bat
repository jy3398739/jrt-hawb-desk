@echo off
cd /d %~dp0
rem 解释器归口：有 .venv 就用它（与线上同一套包版本），没有才退回全局
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  set "PY=python"
  echo [提示] 没找到 .venv，这次先用全局 Python。它被好几个服务共用、容易被顶坏，建议双击 1_安装依赖.bat 建独立环境。
)
if not exist .env (
  echo [提示] 未找到 .env，请先复制 .env.example 为 .env 并填入魔搭令牌。
  pause
  exit /b
)
if not exist input mkdir input
echo 把分单文件（图片/PDF/XLSX）放进 input 文件夹，然后继续。
pause
%PY% run_batch.py input
echo.
echo 处理结束，结果在 output\raw 和 output\air。
pause
