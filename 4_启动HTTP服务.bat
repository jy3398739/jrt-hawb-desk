@echo off
cd /d %~dp0
rem 解释器归口：有 .venv 就用它（与线上同一套包版本），没有才退回全局
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  set "PY=python"
  echo [提示] 没找到 .venv，这次先用全局 Python。它被好几个服务共用、容易被顶坏，建议双击 1_安装依赖.bat 建独立环境。
)
echo 启动 HTTP 服务（默认只监听本机 127.0.0.1:8000）
echo   制单员审核台: http://localhost:8000/      （上传分单 - 原文/航空双口径对照 - 改 - 提交）
echo   接口文档:     http://localhost:8000/docs
echo.
echo 让局域网里其他同事也能打开审核台，只需一步：
echo   改用：%PY% server.py --host 0.0.0.0 --port 8000
echo   然后同事在浏览器打开 http://本机IP:8000/ 用账号密码登录即可。
echo   请先用管理员 admin 登录，在「账号管理」里改掉默认口令 admin123，再给制单员下发口令。
echo.
echo 关闭本窗口即停止服务。
start "" cmd /c "timeout /t 3 /nobreak >nul & start http://localhost:8000/"
%PY% server.py --port 8000
pause
