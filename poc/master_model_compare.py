# -*- coding: utf-8 -*-
"""同一份主单 L1 原文，换模型跑一遍并和已缓存的结果对比：耗时、token、字段差异、红旗、保真。

只读比对：不写 output/master/、不碰公司接口。用法：
    python poc/master_model_compare.py <模型预设键> [另一个预设键...]
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config          # noqa: E402
import vlm_extract     # noqa: E402
from master_fields import clean_ams, build_transcript   # noqa: E402
from fidelity import verify_fidelity                    # noqa: E402
from validator import validate_master                   # noqa: E402
import master_pipeline as mp                            # noqa: E402

CACHE = Path(__file__).resolve().parent.parent / "output" / "master"


def _instrument():
    """把 OpenAI 客户端包一层，记下每次响应的 usage（extract_master 本身不返回 token 数）。"""
    real = vlm_extract._get_client
    box = []

    class Completions:
        def __init__(self, inner):
            self._inner = inner

        def create(self, **kw):
            t0 = time.time()
            resp = self._inner.create(**kw)
            box.append({"wall": round(time.time() - t0, 1),
                        "prompt": getattr(resp.usage, "prompt_tokens", None),
                        "completion": getattr(resp.usage, "completion_tokens", None)})
            return resp

    class Client:
        def __init__(self, inner):
            self.chat = type("Chat", (), {"completions": Completions(inner.chat.completions)})()

    vlm_extract._get_client = lambda: Client(real())
    return box, (lambda: setattr(vlm_extract, "_get_client", real))


def run(choice, tr):
    config.set_model(choice)
    box, undo = _instrument()
    t0 = time.time()
    try:
        raw = vlm_extract.extract_master(tr)
    finally:
        undo()
    dt = round(time.time() - t0, 1)
    ams = clean_ams(raw)
    fid = verify_fidelity(raw, tr, short=mp._SHORT, long=mp._LONG,
                          hs_field="GOODS_INFO_HSCODE", tax=False)
    return {"choice": choice, "model": config.VLM_MODEL, "wall": dt, "usage": box,
            "ams": ams, "flags": validate_master(ams, tr),
            "fidelity": {"passed": fid["passed"], "checked": fid["checked"],
                         "failed": [f["field"] + ":" + str(f["value"])[:40] for f in fid["failed"]]}}


def main():
    mawb = sys.argv[1] if len(sys.argv) > 1 else "29767561793"
    cached = json.loads((CACHE / f"{mawb}.json").read_text(encoding="utf-8"))
    tr = cached["transcript"] or build_transcript({})
    base = {k: v for k, v in cached["ams"].items() if v not in (None, "")}
    print(f"主单 {mawb} · L1 {len(tr['lines'])} 行 · 基准（{cached.get('model')}，{cached.get('elapsed')}s）有值 {len(base)} 列")
    for choice in sys.argv[2:] or ["qwen38-flash-bailian"]:
        r = run(choice, tr)
        got = {k: v for k, v in r["ams"].items() if v not in (None, "")}
        print(f"\n== {choice}（{r['model']}） 用时 {r['wall']}s  用量 {r['usage']}")
        print(f"   有值 {len(got)} 列 · 红旗 {len(r['flags'])} · 保真 {r['fidelity']['passed']}/{r['fidelity']['checked']}")
        only_new = sorted(set(got) - set(base))
        only_base = sorted(set(base) - set(got))
        diff = sorted(k for k in set(got) & set(base) if str(got[k]) != str(base[k]))
        print("   比基准多取到:", only_new or "无")
        print("   比基准少取到:", only_base or "无")
        for k in diff:
            print(f"   值不同 {k}\n     基准 {base[k]!r}\n     本次 {got[k]!r}")
        for f in r["flags"]:
            print("   红旗:", f)
        for f in r["fidelity"]["failed"]:
            print("   保真未过:", f)


if __name__ == "__main__":
    main()
