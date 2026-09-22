@echo off
chcp 65001 >nul
cd /d %~dp0
if not exist .env (
  echo [提示] 未找到 .env，请先复制 .env.example 为 .env 并填入魔搭令牌。
  pause
  exit /b
)
if not exist input mkdir input
echo 把分单文件（图片/PDF/XLSX）放进 input 文件夹，然后继续。
pause
python run_batch.py input
echo.
echo 处理结束，结果在 output\raw 和 output\air。
pause
