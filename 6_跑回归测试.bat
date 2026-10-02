@echo off
cd /d %~dp0
rem 解释器归口：有 .venv 就用它（与线上同一套包版本），没有才退回全局
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  set "PY=python"
  echo [提示] 没找到 .venv，这次先用全局 Python。它被好几个服务共用、容易被顶坏，建议双击 1_安装依赖.bat 建独立环境。
)
echo 离线回归测试：不联网、不调用魔搭 API，几秒钟跑完。
echo 改动 to_air.py / codes.py 码表 / fidelity.py / validator.py 之后都应该先跑一遍。
echo.
%PY% tests\run_tests.py %*
echo.
echo 退出码 0 = 全部绿灯；非 0 = 有回归，看上面的 FAIL。
echo 有意改了 L3 归一化行为后：%PY% tests\run_tests.py --update-baseline 复核差异并更新期望值。
pause
