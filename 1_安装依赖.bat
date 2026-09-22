@echo off
cd /d %~dp0
echo 正在安装依赖（只需第一次运行）...
python -m pip install -r requirements.txt
echo.
if exist "C:\Program Files\LibreOffice\program\soffice.exe" (
    echo 已检测到 LibreOffice（Excel 电子单转 PDF 需要）。
) else (
    echo 未检测到 LibreOffice。Excel 电子单需先转 PDF 再走 VLM，
    echo 请下载安装: https://www.libreoffice.org/download/
    echo 如已装在非常规路径，在 .env 里设 SOFFICE_PATH 指向 soffice.exe。
)
echo.
echo 完成。如报错请确认已安装 Python 3.10-3.12 并勾选 Add to PATH。
pause
