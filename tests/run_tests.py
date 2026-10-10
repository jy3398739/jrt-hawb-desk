# -*- coding: utf-8 -*-
"""HAWB 提取器回归测试入口（零 API 调用、纯本地，改完代码随时跑）。

用法（在 release/hawb_extractor 目录下）:
  python tests/run_tests.py                  # 跑全部
  python tests/run_tests.py fidelity         # 只跑名字含 fidelity 的模块
  python tests/run_tests.py --update-baseline # 有意改了 to_air/码表后，重算 L3 期望值并打印差异

覆盖三层确定性逻辑（VLM 本身不在此列，那部分靠 output/fidelity_report.json 人工复核）：
  L1 文字层直读 / L2→L3 归一化 / 保真正反查 / 规则校验器分工。
新增样本：把该单的 L2 JSON 拷进 fixtures/l3/input/，跑 --update-baseline 复核差异即可入基线。
"""
import argparse
import importlib.util
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

sys.stdout.reconfigure(errors="replace")  # 编码跟着控制台走（强改 UTF-8 会在 GBK 窗口里成一屏乱码），编不出的字符换 ? 继续跑

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))    # 用例可共享 tests/ 里的小辅助（web_src 等）

# 回归必须与运维机上的 .env 无关：那台机器可能把 COMPANY_API_MODE 设成 live（真连公司系统），
# 而用例的默认期望是 mock（不外发）。live 用例自己会在用例内显式切模式并假装 HTTP 层。
# 端点与两把 key 一并钉空：曾有旧用例把 mode 改成 live 后真打了一次公司接口（只读也是外发），
# 钉空后任何"忘了打桩的 live 调用"都会立刻抛 CompanyNotConfigured，而不是悄悄出网。
os.environ["COMPANY_API_MODE"] = "mock"
os.environ["COMPANY_API_URL"] = ""
os.environ["COMPANY_MAWB_KEY"] = ""
os.environ["COMPANY_HAWB_KEY"] = ""
# 主单链默认走另一个渠道（MASTER_VLM_MODEL=qwen38-flash-bailian）。回归里钉成空串=跟分单同渠道，
# 这样假客户端能拦住真实请求；不钉的话测试会真打百炼、真花钱。
os.environ["MASTER_VLM_MODEL"] = ""
# 思考档也钉成"跟随预设"。不钉的话套件就在读这台机器的偏好：2026-10-10 有人在生产审核台把
# 思考拨到"开"（写进 .env 的 MODEL_THINKING=1），当天部署时服务器上 4 条用例齐红、发版被闸门
# 拦下——**运维动作不该能让回归变红**，正如它不该能让用例真打公司接口。要测三档本身，
# 用例里显式 set_thinking("1"/"0"/"")（test_model_switch 那三条就是这么办的）。
os.environ["MODEL_THINKING"] = ""
# 提交幂等锁同样必须离开真 output/：不钉的话，用例提交一次就把锁落到运维机的 output/submit_guard，
# 下次回放同一张票会被判"重复提交"直接吞掉——测试假绿，现场却少了提交。
GUARD_DIR = Path(tempfile.mkdtemp(prefix="hawb_guard_"))
os.environ["SUBMIT_GUARD_DIR"] = str(GUARD_DIR)


def _load_modules(only: str):
    for p in sorted(TESTS.glob("test_*.py")):
        if only and only.lower() not in p.stem.lower():
            continue
        for stale in Path(os.environ["SUBMIT_GUARD_DIR"]).glob("*.json"):
            stale.unlink()      # 模块之间不共享锁；模块内"连点两次"的语义照测
        spec = importlib.util.spec_from_file_location(p.stem, p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        yield p, m


def update_baseline():
    """有意改了 L3 逻辑后重算期望值，逐字段打印变化让人确认，防止误把回归当预期。"""
    import to_air
    inp, exp = TESTS / "fixtures" / "l3" / "input", TESTS / "fixtures" / "l3" / "expected"
    changed = 0
    for p in sorted(inp.glob("*.json")):
        new = to_air.to_air(json.loads(p.read_text(encoding="utf-8")))
        ep = exp / p.name
        old = json.loads(ep.read_text(encoding="utf-8")) if ep.exists() else {}
        keys = [k for k in new if str(old.get(k, "") or "") != str(new.get(k, "") or "")]
        if not keys:
            continue
        changed += 1
        print(f"\n== {p.name}")
        for k in keys:
            print(f"   {k}\n     旧 {str(old.get(k, ''))[:120]!r}\n     新 {str(new.get(k, ''))[:120]!r}")
        ep.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")
    miss = {p.stem for p in inp.glob("*.json")} ^ {p.stem for p in exp.glob("*.json")}
    print(f"\n更新 {changed} 份期望值；input/expected 不匹配的夹具: {sorted(miss) or '无'}")
    print("请逐条确认上面这些差异确实是本次改动想要的，再提交。")


def _machine_files():
    """跑回归之前先记住这台机器的真配置与真数据的**清单**：它们不是测试夹具。

    两个来源都是踩过的坑：
    ① 2026-10-08 `_ModelState` 只存了模块全局、没重定向 ENV_FILE，于是每个 set_model() 都写进
       真 .env；在服务器上跑就等于每次部署都把模型选择改回去（光比字节不够，写回同一个值查不出来，
       所以 mtime 也一起比：原子替换一定会动它）。
    ② 同日另一条：一条提交用例忘了把 OUTPUT_RAW_DIR 指开，往真 output/raw 里落了一份假 L2，
       于是后面三条用例读到了它、报出凭空的红旗。output/ 不比内容，比"有哪些文件"——
       测试写进真输出目录的动作就是新建/删掉一个文件，这一层足够抓到，而且不用扫全树哈希。
    """
    import hashlib
    import os
    out = {}
    for name, p in (("config.ENV_FILE", ROOT / ".env"), ("auth.USERS_FILE", ROOT / "users.json")):
        if os.path.isfile(p):
            out[name] = (hashlib.sha256(p.read_bytes()).hexdigest(), os.stat(p).st_mtime_ns)
        else:
            out[name] = None
    odir = ROOT / "output"
    out["output/ 文件清单"] = sorted(str(p.relative_to(odir)).replace("\\", "/")
                                    for p in odir.rglob("*") if p.is_file()) if odir.is_dir() else []
    return out


def main():
    ap = argparse.ArgumentParser(description="HAWB 提取器离线回归测试")
    ap.add_argument("filter", nargs="?", default="", help="只跑模块名含此串的用例")
    ap.add_argument("--update-baseline", action="store_true", help="重算 L3 期望值（改了 to_air/码表后用）")
    args = ap.parse_args()
    if args.update_baseline:
        return update_baseline()

    before = _machine_files()
    passed = failed = ran = 0
    for p, m in _load_modules(args.filter):
        print(f"\n[{p.stem}]")
        for name in [x for x in dir(m) if x.startswith("test_")]:
            ran += 1
            try:
                getattr(m, name)()
                passed += 1
                print(f"  ok    {name}")
            except AssertionError as e:
                failed += 1
                print(f"  FAIL  {name}: {e}")
            except Exception:
                failed += 1
                print(f"  ERROR {name}:\n{traceback.format_exc()}")
    print("\n" + "=" * 56)
    if not ran:
        print(f"没有任何用例被跑到（过滤词 {args.filter!r} 没匹配上 test_*.py），这不算通过。")
        return 1
    after = _machine_files()
    changed = [k for k, v in before.items() if after[k] != v]
    if changed:
        bits = []
        for k in changed:
            if isinstance(before[k], list):
                bits.append(f"{k} 新增 {sorted(set(after[k]) - set(before[k]))[:5] or '无'}、"
                            f"少了 {sorted(set(before[k]) - set(after[k]))[:5] or '无'}")
            else:
                bits.append(k)
        print(f"❌ 这套回归改了这台机器的真东西：{'；'.join(bits)}"
              f"（用例必须指到自己的临时文件：在服务器上跑就等于每次部署都改掉线上配置，"
              f"往真 output/ 里落一份假结果则会让后面的用例读到凭空的红旗）")
        return 1
    print(f"通过 {passed}，失败 {failed}" + ("  ✅ 全部绿灯" if not failed else "  ❌ 有回归"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
