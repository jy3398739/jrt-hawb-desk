# -*- coding: utf-8 -*-
"""同一份主单 L1 原文换模型跑一遍：耗时、token、字段差异、红旗、保真。

用法：python poc/master_model_compare.py <主单号> [预设键...]
    不给键就默认拿主单链当前选择（MASTER_VLM_MODEL）跑一次，和缓存里的结果对。
只读比对：不写 output/master/、不碰公司接口。走的是真链路（master_prompt + 该渠道客户端），
所以 usage 能直接读到——之前那版自己包了个假客户端，结果把请求发去了分单的渠道。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                    # noqa: E402
import vlm_extract                               # noqa: E402
import master_pipeline as mp                     # noqa: E402
from master_fields import clean_ams, MASTER_COLS  # noqa: E402
from fidelity import verify_fidelity             # noqa: E402
from validator import validate_master            # noqa: E402

CACHE = Path(__file__).resolve().parent.parent / "output" / "master"


def run(bundle, tr):
    """照 _chat_json 的样子发一次，唯一区别是把 usage 留下来给人看。"""
    client = vlm_extract._client_for(bundle)
    prompt = vlm_extract.master_prompt(tr)
    t0 = time.time()
    resp = client.chat.completions.create(
        model=bundle["model"], messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        max_tokens=bundle["max_tokens"], temperature=0.0, extra_body=bundle["extra_body"])
    dt = round(time.time() - t0, 1)
    text = (resp.choices[0].message.content or "").strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.split("\n") if not l.strip().startswith("```"))
    raw = json.loads(text)
    for c in MASTER_COLS:
        raw.setdefault(c, None if c == "SLAC" else "")
    ams = clean_ams(raw)
    fid = verify_fidelity(raw, tr, short=mp._SHORT, long=mp._LONG,
                          hs_field="GOODS_INFO_HSCODE", tax=False)
    return {"model": bundle["model"], "wall": dt,
            "usage": {"prompt": resp.usage.prompt_tokens, "completion": resp.usage.completion_tokens},
            "ams": ams, "flags": validate_master(ams, tr) + mp._missed_flags(ams, tr)[0],
            "fidelity": {"passed": fid["passed"], "checked": fid["checked"],
                         "failed": [f["field"] for f in fid["failed"]]}}


def main():
    mawb = sys.argv[1] if len(sys.argv) > 1 else "29767561793"
    cached = json.loads((CACHE / f"{mawb.replace('-', '')}.json").read_text(encoding="utf-8"))
    tr = cached["transcript"]
    base = {k: v for k, v in cached["ams"].items() if v not in (None, "")}
    print(f"主单 {mawb} · L1 {len(tr['lines'])} 行 {len(tr['full_text'])} 字符 · "
          f"基准（{cached.get('model')}，{cached.get('elapsed')}s）有值 {len(base)} 列")
    old = config.MASTER_VLM_MODEL
    try:
        for choice in sys.argv[2:] or [old or config.DEFAULT_MASTER_MODEL_KEY]:
            config.MASTER_VLM_MODEL = choice
            r = run(config.master_model_bundle(), tr)
            got = {k: v for k, v in r["ams"].items() if v not in (None, "")}
            print(f"\n== {choice}（{r['model']}） 用时 {r['wall']}s  用量 {r['usage']}")
            print(f"   有值 {len(got)} 列 · 红旗 {len(r['flags'])} · 保真 {r['fidelity']['passed']}/{r['fidelity']['checked']}")
            print("   比基准多取到:", sorted(set(got) - set(base)) or "无")
            print("   比基准少取到:", sorted(set(base) - set(got)) or "无")
            for k in sorted(set(got) & set(base)):
                if str(got[k]) != str(base[k]):
                    print(f"   值不同 {k}\n     基准 {base[k]!r}\n     本次 {got[k]!r}")
            for f in r["flags"]:
                print("   红旗:", f)
    finally:
        config.MASTER_VLM_MODEL = old


if __name__ == "__main__":
    main()
