@echo off
cd /d %~dp0
rem 解释器归口：有 .venv 就用它（与线上同一套包版本），没有才退回全局
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  set "PY=python"
  echo [提示] 没找到 .venv，这次先用全局 Python。它被好几个服务共用、容易被顶坏，建议双击 1_安装依赖.bat 建独立环境。
)
%PY% export_excel.py
echo.
echo Excel 已生成在 output 文件夹（hawb_raw.xlsx / hawb_air.xlsx）。
pause
