@echo off
chcp 65001 >nul
cd /d %~dp0
python export_excel.py
echo.
echo Excel 已生成在 output 文件夹（hawb_raw.xlsx / hawb_air.xlsx）。
pause
