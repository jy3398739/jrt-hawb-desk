@echo off
cd /d %~dp0
if not exist input mkdir input
echo 监控已启动：把分单文件拖进 input 文件夹即自动识别，关掉本窗口停止。
echo.
python watch_folder.py input
pause
