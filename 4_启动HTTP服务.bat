@echo off
chcp 65001 >nul
cd /d %~dp0
echo 启动 HTTP 服务（默认只监听本机 127.0.0.1:8000）
echo   制单员审核台: http://localhost:8000/      （上传分单 - 原文/航空双口径对照 - 改 - 提交）
echo   接口文档:     http://localhost:8000/docs
echo.
echo 让局域网里其他同事也能打开审核台，只需一步：
echo   改用：python server.py --host 0.0.0.0 --port 8000
echo   然后同事在浏览器打开 http://本机IP:8000/ 用账号密码登录即可。
echo   请先用管理员 admin 登录，在「账号管理」里改掉默认口令 admin123，再给制单员下发口令。
echo.
echo 关闭本窗口即停止服务。
start "" cmd /c "timeout /t 3 /nobreak >nul & start http://localhost:8000/"
python server.py --port 8000
pause
