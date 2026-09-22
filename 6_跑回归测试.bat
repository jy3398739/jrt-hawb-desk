@echo off
cd /d %~dp0
echo 离线回归测试：不联网、不调用魔搭 API，几秒钟跑完。
echo 改动 to_air.py / hawb2json.py 码表 / fidelity.py / validator.py 之后都应该先跑一遍。
echo.
python tests\run_tests.py %*
echo.
echo 退出码 0 = 全部绿灯；非 0 = 有回归，看上面的 FAIL。
echo 有意改了 L3 归一化行为后：python tests\run_tests.py --update-baseline 复核差异并更新期望值。
pause
