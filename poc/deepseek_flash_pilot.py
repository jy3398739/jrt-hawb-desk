# -*- coding: utf-8 -*-
"""DeepSeek-flash 两票横评：同一份 L1 转录喂给 deepseek-flash（纯文本、温度 0、不带工具），
跑出 raw → to_air，与本机主链的 output/air 逐列对比。只读：不写 output、不发公司。
跑法：在 release/hawb_extractor 下 `python poc/deepseek_flash_pilot.py`，key 从 .env 的 DEEPSEEK_API_KEY 读。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os
os.environ.setdefault("HAWB_EORI_BG", "0")
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
          "NO_PROXY", "no_proxy"):
    os.environ.pop(k, None)   # venv 的 httpx2 解析 NO_PROXY 里的 [::1] 会炸（端口解析 bug）；直连本来就通

import config
import to_air
import vlm_extract
from openai import OpenAI

KEY = [l.split("=", 1)[1] for l in (ROOT / ".env").read_text(encoding="utf-8").splitlines()
       if l.startswith("DEEPSEEK_API_KEY=")][0]
CLIENT = OpenAI(api_key=KEY, base_url="https://api.deepseek.com")

TICKETS = ["999-95764373分", "016-82445790"]

def _find(kind, stem):
    # 文件名里的空格是不换行空格（ ），按前缀匹配
    hit = [f for f in (ROOT / "output" / kind).glob("*.json")
           if f.name.startswith(stem) and not f.stem.endswith("副本")
           and not f.name.endswith("(1).json")]
    assert len(hit) == 1, (kind, stem, [f.name for f in hit])
    return hit[0]


def one(stem: str) -> dict:
    tr = json.loads(_find("transcript", stem).read_text(encoding="utf-8"))
    tr = {k: tr[k] for k in ("lines", "full_text") if k in tr}
    ours = json.loads(_find("air", stem).read_text(encoding="utf-8"))

    body = "\n".join(f"{x['i']}: {x['text']}" for x in tr["lines"])
    prompt = vlm_extract.PROMPT_TEXT_ONLY + vlm_extract.PROMPT_HEAD + vlm_extract.PROMPT_TRANSCRIPT + body
    raw = vlm_extract._chat_json(prompt, [], vlm_extract.FIELDS, stem,
                                 client=CLIENT, model_id="deepseek-flash",
                                 extra_body={}, stage="DS试点")
    theirs = to_air.to_air(raw)

    cols = [c for c in ours if c in theirs]
    diffs = []
    for c in cols:
        a, b = str(ours.get(c) or "").strip(), str(theirs.get(c) or "").strip()
        if a != b:
            diffs.append((c, a, b))
    filled_ours = sum(1 for c in cols if str(ours.get(c) or "").strip())
    filled_theirs = sum(1 for c in cols if str(theirs.get(c) or "").strip())
    return {"stem": stem, "cols": len(cols), "diffs": diffs,
            "filled": (filled_ours, filled_theirs)}


for stem in TICKETS:
    r = one(stem)
    print(f"\n===== {r['stem']}：{r['cols']} 列，主链有值 {r['filled'][0]} / flash 有值 {r['filled'][1]}，"
          f"差异 {len(r['diffs'])} 格 =====")
    for c, a, b in r["diffs"]:
        print(f"  [{c}]")
        print(f"    主链: {a[:120]!r}")
        print(f"    flash: {b[:120]!r}")
