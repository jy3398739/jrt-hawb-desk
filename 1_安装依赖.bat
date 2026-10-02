@echo off
cd /d %~dp0
echo 正在建独立环境 .venv，并按服务器锁装依赖（只需第一次运行）...
if not exist ".venv\Scripts\python.exe" python -m venv .venv
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
%PY% -m pip install -r requirements-server.lock.txt
echo.
if exist "C:\Program Files\LibreOffice\program\soffice.exe" (
    echo 已检测到 LibreOffice（Excel 电子单转 PDF 需要）。
) else (
    echo 未检测到 LibreOffice。Excel 电子单需先转 PDF 再走 VLM，
    echo 请下载安装: https://www.libreoffice.org/download/
    echo 如已装在非常规路径，在 .env 里设 SOFFICE_PATH 指向 soffice.exe。
)
echo.
echo 完成。环境就在本目录 .venv，包版本与线上那一套一致；如报错请确认已装 Python 3.12 并勾选 Add to PATH。
pause
