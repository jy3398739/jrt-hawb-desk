@echo off
cd /d %~dp0
rem 解释器归口：有 .venv 就用它（与线上同一套包版本），没有才退回全局
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  set "PY=python"
  echo [提示] 没找到 .venv，这次先用全局 Python。它被好几个服务共用、容易被顶坏，建议双击 1_安装依赖.bat 建独立环境。
)
echo 站点取单：同事在分单审核台网页上传分单，识别在这台机器上跑，结果自动回写到网页。
echo.
echo 用之前先确认三件事：
echo   1. .env 里 SITE_URL= 填了站点地址，MODELSCOPE_API_KEY= 有效；
echo   2. 站点 Settings 里有名为 WORKER_TOKEN 的密钥，值和 .env 里的 WORKER_TOKEN 一致；
echo   3. 这台机器开着并且能上网——关机期间任务留在站点队列里，不会丢，开机继续跑。
echo.
echo 这个窗口关掉（或 Ctrl+C）就等于停止取单：已在手上的票 30 分钟后自动回队列。
echo 想只清一遍队列就退出（比如挂计划任务）：7_站点取单.bat --once
echo 想一次少领几张、别占太久：7_站点取单.bat --limit 1
echo.
%PY% site_worker.py %*
echo.
echo 上面若出现"被拒"或"未配置 WORKER_TOKEN"，先核对第 2 条；网络类提示会自动重试。
pause
