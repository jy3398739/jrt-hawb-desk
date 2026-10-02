# -*- coding: utf-8 -*-
"""统一配置：所有密钥/路径/连接串从环境变量或同目录 .env 读取，禁止写死在代码里。

在公司机器上：复制 .env.example 为 .env，填入官方书生API 令牌（INTERNLM_API_KEY）即可。
"""
import os
import subprocess
import sys
from pathlib import Path

import atomic

# 版本号：发一次改一次（deploy/release.sh 负责改号 + 跑回归 + 打 tag + 推服务器 + 核对）。
# 1.x = 现役形态（票面 PDF/图片 → L1 转录 → L2 提取 → 审核台核对 → 回传公司 j9 AMS）。
# FORM 是"形态"标记，不是版本：V2（对接真实网页制单）已于 2026-09-29 删除，
# 万一再出现两台并存的形态，靠它分辨，别把它塞进版本号里。
APP_VERSION = "1.0.5"
FORM = "V1"

ENV_FILE = Path(__file__).with_name(".env")

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
except Exception:
    pass  # 没装 python-dotenv 时直接读系统环境变量


def git_commit() -> str:
    """当前代码的 git 短哈希。服务器只收 tar 推过去的文件、没有仓库，此时安静返回空串——
    /health 拿它和 built_at（源码最新 mtime）搭配着看，就能说出"这台跑的是哪一版"。"""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(BASE_DIR),
                             capture_output=True, text=True, timeout=2)
        return (out.stdout or "").strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def fix_console() -> None:
    """CLI 入口先调它。票面文件名常带不间断空格 U+00A0，Windows 控制台/重定向默认 GBK，
    print 这类名字会抛 UnicodeEncodeError 把整批处理打断——日志不该有资格搞死任务。

    只改 errors，不改 encoding：这里曾是 reconfigure(encoding="utf-8")，结果在 cp936 的
    cmd 窗口里打出**一屏乱码**（双击 bat 的人看到的就是那个）。编码跟着控制台走才看得懂，
    编不出的字符换成 ? 继续跑。服务器那侧 locale 是 UTF-8，这条改动对它无感。"""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:
            pass  # 被替换成非标准流（如某些测试夹具）时跳过

# === 模型渠道：默认端点/密钥回退用魔搭（仅当 VLM_MODEL 填原始 org/模型 id 时才用到）===
MODELSCOPE_API_KEY = os.getenv("MODELSCOPE_API_KEY", "").strip()
MODELSCOPE_BASE_URL = os.getenv("MODELSCOPE_BASE_URL", "https://api-inference.modelscope.cn/v1").strip()

# 模型预设：.env 的 VLM_MODEL 与审核台下拉都用这套键，也允许直接写任意 org/模型 id。
# vision 决定要不要把票面图片发给它。纯文本链路机制保留：原始模型 id + VLM_VISION=0 强制
# 关视觉——纯文本模型收到图片既不报错也不拒答，而是把票面**编**成别的内容，假数据比报错危险。
# 每个预设都要带 max_tokens 下限：Intern-S2 实测（2026-09-21，GL26090330 票）单次推理烧掉
# 19309 输出 token，无下限时被 8192 掐死 → content 空 → 整票连败三次。
# 可选键 base_url / api_key_env：默认走魔搭端点与 MODELSCOPE_API_KEY；填了就把这一张卡
# 整体切到别的服务商，密钥从 api_key_env 指的环境变量读（官方书生API 即以此接线）。
MODEL_PRESETS = {
    # 官方 书生/InternLM API（令牌在 https://internlm.intern-ai.org.cn/api/tokens 申请，端点 chat.intern-ai.org.cn/api/v1）。
    # 2026-09-21 实测：Intern-S2-Preview 真读图，普通票 CLA26090022 与生产基线零字段差异、保真 21/21、10 秒；
    # 难票 GL26090330 漏主单被保真漏抄核查抓住（非静默）、签发日期正确留空。无配额墙、无 choices:null 网关抖动。
    # 用户 2026-09-21 定案：删除魔搭（intern-s2/qwen-flash/deepseek）与北龙（blsc-s2）全部预设，只留官方这一个。
    # 换渠道靠 base_url + api_key_env 两键（模型 id 无 org 前缀，密钥从 INTERNLM_API_KEY 读，与魔搭互不通用）。
    "intern-s2-official": {"model": "intern-s2-preview", "vision": True, "max_tokens": 32768,
                           "label": "Intern-S2-Preview · 官方书生API · 视觉",
                           "base_url": "https://chat.intern-ai.org.cn/api/v1",
                           "api_key_env": "INTERNLM_API_KEY",
                           "key_hint": "https://internlm.intern-ai.org.cn/api/tokens"},
    # 阿里云百炼 Qwen3.8-Flash（OpenAI 兼容端点 https://dashscope.aliyuncs.com/compatible-mode/v1，
    # 密钥在百炼控制台申请，按 token 计费）。2026-09-24 接入前三项实测：
    # ① 模型清单里只有 qwen3.8-flash 这一个 id，qwen3.8-flash-next 之类写法回 404 ⇒ 钉死；
    # ② 也是思考型：关思考前单票会多烧几千 reasoning token（同族 qwen3.8-27b 实测），
    #    与 S2/MiMo 一样需要 max_tokens 下限，否则 finish_reason=length、content 空；
    # ③ 关思考两种写法都有效：{"enable_thinking":false} 与 MiMo 那套 {"thinking":{"type":"disabled"}}
    #    实测都能把 reasoning 归零，取文档通用的前者。
    # 20 票（同一批 L1、各自会话内重跑）横评：保真 409/410 与红旗 9 条，均优于 S2 的 409/412 与 12 条；
    # 解掉 S2 两处静默缺陷（FAX 槽塞电话号、漏主单号），零新增静默错，长尾比 MiMo 轻（MiMo 有 47s 那一档）；
    # 代价：ANGB 两票丢 TO1=AMS（与 S2 关思考时同款，红旗会抓）+ 均值比 S2 慢约 3 秒/票。
    # 用户 2026-09-24 定案：默认仍是 intern-s2-official，本渠道排第二供切换。
    "qwen38-flash-bailian": {"model": "qwen3.8-flash", "vision": True, "max_tokens": 32768,
                             "label": "Qwen3.8-Flash · 阿里云百炼 · 视觉（已关思考）",
                             "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                             "api_key_env": "DASHSCOPE_API_KEY",
                             "key_hint": "百炼控制台 https://bailian.console.aliyun.com/?tab=model#/api-key（按 token 计费，余额不足会 4xx）",
                             "extra_body": {"enable_thinking": False}},
    # 小米 MiMo API（https://api.xiaomimimo.com/v1，密钥在 https://platform.xiaomimimo.com/#/console/api-keys 申请，付费）。
    # 2026-09-24 接入前四项实测：① id 大小写敏感——文档示例里的 MiMo-V2.6-Flash 写法回 400 Unsupported model，
    # 清单（GET /models）给的是全小写 mimo-v2.6-flash，故钉小写；② 真读图（420x220 测试票面三行照出）；
    # ③ 默认开思考且很贵——同 5 票开思考 49.6s/票（单票最高 101s、9487 输出 token 里 8850 是 reasoning）
    #    vs 关思考 7.5s/票，字段质量没有差别，所以预设直接把思考关掉；
    # ④ 关思考只认 {"thinking":{"type":"disabled"}}（或 reasoning_effort:"none"），
    #    官方文档那种 enable_thinking:false 的写法实测无效——照样烧 reasoning token。
    "mimo-v2.6-flash": {"model": "mimo-v2.6-flash", "vision": True, "max_tokens": 32768,
                        "label": "MiMo-V2.6-Flash · 小米MiMo · 视觉（已关思考）",
                        "base_url": "https://api.xiaomimimo.com/v1",
                        "api_key_env": "MIMO_API_KEY",
                        "key_hint": "https://platform.xiaomimimo.com/#/console/api-keys（该渠道按 token 计费，账户余额为 0 时所有调用回 402）",
                        "extra_body": {"thinking": {"type": "disabled"}}},
    # 火山方舟 Doubao-Seed-2.1-Lite（2026-09-24 接入，用户给测试密钥）。三条实测事实：
    # ① 密钥可直接用模型名调用（不需要建 ep- 接入点）；无日期的 "doubao-seed-2-1-lite" 报 404，
    #    必须带版本后缀 -260915，所以这里钉死带日期的 id；② 真读图（420x120 小图两行字照抄无误）；
    #    ③ 是思考型：那发 85 个输出 token 里 57 个是 reasoning，故与 Intern-S2 同待给 max_tokens 下限。
    # 它仍然没有关思考版那么快：开思考 97~124s/票，实测只能当"第二只眼睛"（用户 2026-09-24 未接该流程）。
    # 排在末位是因为用户 2026-09-24 点名的顺序是 S2 → qwen3.8-flash → 小米，没提它——不删除，只挪到最后。
    "doubao-seed-2-1-lite": {"model": "doubao-seed-2-1-lite-260915", "vision": True, "max_tokens": 32768,
                             "label": "Doubao-Seed-2.1-Lite · 火山方舟 · 视觉",
                             "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                             "api_key_env": "ARK_API_KEY",
                             "key_hint": "方舟控制台 https://console.volcengine.com/ark（API Key 与模型开通在同一账号下）"},
}
DEFAULT_MODEL_KEY = "intern-s2-official"


def resolve_model(choice: str = "") -> dict:
    """选择（预设键或 org/模型 id，大小写不敏感）→ {choice, model, vision, label}。
    未知 id 按有视觉处理，维持本机制上线前的行为；确有纯文本模型可用 VLM_VISION=0 强制。"""
    c = (choice or "").strip()
    preset = MODEL_PRESETS.get(c.lower()) if c else MODEL_PRESETS[DEFAULT_MODEL_KEY]
    if preset is None:
        info = {"choice": c, "model": c, "vision": True, "label": f"自定义 {c}"}
    else:
        info = {"choice": c or DEFAULT_MODEL_KEY, "model": preset["model"],
                "vision": bool(preset["vision"]), "label": preset["label"]}
    forced = os.getenv("VLM_VISION", "").strip()
    if forced in ("0", "1"):
        info["vision"] = forced == "1"      # 调试/新模型兜底：手动强制，不让它自己猜
    return info


_MODEL_INFO = resolve_model(os.getenv("VLM_MODEL", ""))
VLM_MODEL_CHOICE = _MODEL_INFO["choice"]   # .env / 审核台里存的那个（预设键或原始 id）
VLM_MODEL = _MODEL_INFO["model"]           # 真正发出去的模型 id
MODEL_VISION = _MODEL_INFO["vision"]       # False = 只发文字，绝不见图


def model_max_tokens() -> int:
    """推理型模型（DeepSeek / Intern-S2）先花几千 token 想再吐结果：用预设里的下限兜底，
    否则真票上 finish_reason=length、content 为空，看着像模型挂了。
    上限不额外计费，抬高不亏。"""
    preset = MODEL_PRESETS.get(str(VLM_MODEL_CHOICE).lower()) or {}
    return max(VLM_MAX_TOKENS, int(preset.get("max_tokens", 0)))


def _active_preset() -> dict:
    """当前生效选择对应的预设；自定义 id 没有预设 → 空 dict → 走魔搭默认渠道。"""
    return MODEL_PRESETS.get(str(VLM_MODEL_CHOICE).lower()) or {}


def model_extra_body() -> dict:
    """预设声明的额外请求体参数（如 MiMo 关思考），随每次 create 原样发出。
    没有则返回空 dict——不能给没声明的预设凭空塞参数，各家开关写法互不通用
    （MiMo 认 thinking.type，豆包认同名写法，百炼两种都吃，官方书生压根不接受）。"""
    return dict(_active_preset().get("extra_body") or {})


def vlm_base_url() -> str:
    """当前渠道的 OpenAI 兼容端点：预设带 base_url 就用它，否则维持魔搭。"""
    return (_active_preset().get("base_url") or MODELSCOPE_BASE_URL).strip()


# === 主单（MAWB）链的模型（2026-09-29 实测定案）===
# 同一条主单输入：intern-s2-preview 取到 2/36 列（97.8s），qwen3.8-flash 取到 17/36 列（7s）。
# 主单资料是几百字符的拼接文本、不是整张票面，S2 在这种输入上不稳，所以两条链分开选模型。
# 设成空串 = 不独立，跟审核台当前选择走（回归就是这么钉的，保证测试不出网）。
DEFAULT_MASTER_MODEL_KEY = "qwen38-flash-bailian"
_raw_master_model = os.getenv("MASTER_VLM_MODEL")
MASTER_VLM_MODEL = (DEFAULT_MASTER_MODEL_KEY if _raw_master_model is None
                    else _raw_master_model).strip()


def master_model_bundle() -> dict:
    """主单链这次该用哪个模型/端点/参数。选择为空就沿用当前生效选择。"""
    if not MASTER_VLM_MODEL:
        return {"choice": VLM_MODEL_CHOICE, "model": VLM_MODEL, "vision": MODEL_VISION,
                "base_url": vlm_base_url(), "api_key_env": _active_preset().get("api_key_env"),
                "max_tokens": model_max_tokens(), "extra_body": model_extra_body()}
    info = resolve_model(MASTER_VLM_MODEL)
    preset = MODEL_PRESETS.get(str(info["choice"]).lower()) or {}
    return {"choice": info["choice"], "model": info["model"], "vision": info["vision"],
            "label": info.get("label") or preset.get("label", ""),
            "base_url": (preset.get("base_url") or MODELSCOPE_BASE_URL).strip(),
            "api_key_env": preset.get("api_key_env"),
            "max_tokens": max(VLM_MAX_TOKENS, int(preset.get("max_tokens", 0))),
            "extra_body": dict(preset.get("extra_body") or {})}


def set_master_model(choice: str) -> dict:
    """运行时切换主单链的模型（审核台用）。空串=不独立，跟分单当前选择走。"""
    global MASTER_VLM_MODEL
    MASTER_VLM_MODEL = (choice or "").strip()
    return master_model_bundle()


def master_api_key_configured() -> bool:
    """/health 用：主单链渠道有没有密钥，只报有没有，不碰值本身。"""
    env = master_model_bundle().get("api_key_env")
    return bool(os.getenv(env, "").strip()) if env else vlm_api_key_configured()


def master_api_key(bundle: dict) -> str:
    """主单链渠道的密钥。缺了就明确说该配哪个变量，并给出"退回分单同一渠道"这条路。"""
    env = bundle.get("api_key_env")
    if not env:
        return require_api_key()
    key = os.getenv(env, "").strip()
    if not key:
        raise SystemExit(f"主单链渠道缺密钥：请在 .env 配 {env}；"
                         "或把 MASTER_VLM_MODEL 设成空，让主单跟分单用同一个模型")
    return key


def require_vlm_api_key() -> str:
    """当前渠道的密钥。预设声明了 api_key_env 就从那个环境变量读（如官方书生API 的 INTERNLM_API_KEY），
    没声明即魔搭。切渠道后调用方拿到的 key 与 vlm_base_url() 始终配套。"""
    env = _active_preset().get("api_key_env")
    if not env:
        return require_api_key()
    key = os.getenv(env, "").strip()
    if not key:
        preset = _active_preset()
        hint = preset.get("key_hint") or "https://internlm.intern-ai.org.cn/api/tokens"
        raise SystemExit(
            f"未配置当前模型渠道（{VLM_MODEL_CHOICE}）的密钥："
            f"请在 .env 或环境变量中设置 {env}\n"
            f"密钥获取: {hint}（令牌勿写进代码或聊天记录）"
        )
    return key


def vlm_api_key_configured() -> bool:
    """/health 用：只报当前渠道有没有密钥，不碰、不外传值本身。"""
    env = _active_preset().get("api_key_env")
    return bool(os.getenv(env, "").strip()) if env else bool(MODELSCOPE_API_KEY)


def set_model(choice: str) -> dict:
    """运行时切换（审核台 POST /model 用）：本进程立即生效，不必重启。"""
    global VLM_MODEL_CHOICE, VLM_MODEL, MODEL_VISION
    info = resolve_model(choice)
    VLM_MODEL_CHOICE, VLM_MODEL, MODEL_VISION = info["choice"], info["model"], info["vision"]
    return info


def persist_model_choice(choice: str, env_key: str = "VLM_MODEL") -> None:
    """把选择写回 .env 的某一行（默认 VLM_MODEL；主单链传 MASTER_VLM_MODEL）。
    其余行逐字保留，写临时文件再原子替换，重启后与 CLI/批处理都照它走。
    换行风格照抄原文件：Windows 上 write_text 会把 \\n 统统翻成 \\r\\n，把一份 LF 的 .env 整篇改脏。"""
    raw = ""
    if ENV_FILE.exists():
        with ENV_FILE.open("r", encoding="utf-8", newline="") as f:
            raw = f.read()
    nl = "\r\n" if "\r\n" in raw else "\n"
    out, hit = [], False
    for ln in raw.splitlines():
        if not ln.lstrip().startswith("#") and ln.strip().startswith(env_key + "="):
            out.append(f"{env_key}={choice}")
            hit = True
        else:
            out.append(ln)
    if not hit:
        if out and out[-1].strip():
            out.append("")
        out.append(f"{env_key}={choice}")
    atomic.write_text(ENV_FILE, nl.join(out) + nl, mode=0o600)   # .env 里放着令牌：新建就收 600

# === 目录 ===
BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = Path(os.getenv("INPUT_DIR", str(BASE_DIR / "input")))
ARCHIVE_DIR = Path(os.getenv("ARCHIVE_DIR", str(BASE_DIR / "output" / "archive")))   # L0 原件归档
TRANSCRIPT_DIR = Path(os.getenv("TRANSCRIPT_DIR", str(BASE_DIR / "output" / "transcript")))  # L1 逐字转录
OUTPUT_RAW_DIR = Path(os.getenv("OUTPUT_RAW_DIR", str(BASE_DIR / "output" / "raw")))  # L2 原文口径 全字段
OUTPUT_AIR_DIR = Path(os.getenv("OUTPUT_AIR_DIR", str(BASE_DIR / "output" / "air")))  # L3 航空口径 全字段
OUTPUT_QC_DIR = Path(os.getenv("OUTPUT_QC_DIR", str(BASE_DIR / "output" / "qc")))     # 质检红旗，Excel/DB 按名联结
PREVIEW_DIR = Path(os.getenv("PREVIEW_DIR", str(BASE_DIR / "output" / "preview")))    # 电子单转出的 PDF，审核台回看票面用
# 提交台账：制单员点提交、回传公司成功后逐张记一笔（stem→{主单号,分单号,复合键,复核人,时间,回执}）。
# "仅人工提交才回传"，机批/监控/站点都只落盘不进这里；(主单号|分单号)↔原件 的索引也只从这张表构建。
SUBMIT_LEDGER = Path(os.getenv("SUBMIT_LEDGER", str(BASE_DIR / "output" / "submitted.json")))
# 提交幂等锁：j9 没有幂等键，重复点一次就是第二次整表写回，所以同内容的重复提交在锁定期内
# 直接回上次回执。一锁一文件（键的哈希命名），跨进程也认；测试要隔离它就跟台账一起指到临时目录。
SUBMIT_GUARD_DIR = Path(os.getenv("SUBMIT_GUARD_DIR", str(BASE_DIR / "output" / "submit_guard")))
# 主单(MAWB)原件落盘处：按归一化主单号建子目录，里面放公司给的主单 PDF/电子单。
# 公司主单原件接口待 IT——先支持人工放入同一目录跑通录入员"对票面核对"，真接口到位后
# 由 company_api 把取回的主单原件缓存进这里，路由与前端一行都不用改。
MAWB_SOURCE_DIR = Path(os.getenv("MAWB_SOURCE_DIR", str(BASE_DIR / "output" / "mawb_source")))
# 主单解析结果缓存：按归一化主单号一份（含 L1 文本、L2/L3 可编辑面、质检与资料指纹 text_md5）。
# 主单不是分单——不进 output/raw|air|qc，也不进 Excel/数据库。
MASTER_DIR = Path(os.getenv("MASTER_DIR", str(BASE_DIR / "output" / "master")))
# 主单提交台账：mawb2 回传成功后逐条记（主单号→复核人/时间/回执/确认过的红旗/提交前快照）。
# 单独一张表是因为分单台账以 stem 为键，而主单没有 stem。
MASTER_LEDGER = Path(os.getenv("MASTER_LEDGER", str(BASE_DIR / "output" / "master_submitted.json")))

# === 行为参数 ===
VLM_MAX_TOKENS = int(os.getenv("VLM_MAX_TOKENS", "8192"))
VLM_RETRIES = int(os.getenv("VLM_RETRIES", "3"))
# 单次 API 调用超时（秒）。魔搭上真有模型收下请求后长时间不回：实测 deepseek-pro-0813
# 同一账号下小 prompt 1.6 秒就回，换成真实票 prompt 则 14 分钟无响应。不设上限时 SDK
# 默认 600s 且自己再重试 2 次，叠上 VLM_RETRIES=3 最坏要静默等 90 分钟才报错；显式给定
# 并关掉 SDK 层重试后，重试只剩我们这一层。
# 300s 是"慢但成功"与"不再拖死人"的折中：慢模型单票实测 90-260 秒，留 15% 余量。
# 关键约束是**超时只再试一次**（VLM_TIMEOUT_TRIES）：慢模型重跑一遍五分钟往往还是慢，
# 按 VLM_RETRIES 连撞三次就是 15 分钟起，那才是审核台"点了没反应"的来源。
VLM_TIMEOUT = float(os.getenv("VLM_TIMEOUT", "300"))
VLM_TIMEOUT_TRIES = int(os.getenv("VLM_TIMEOUT_TRIES", "2"))
IMAGE_LONG_EDGE = int(os.getenv("IMAGE_LONG_EDGE", "0"))  # 0=原图(魔搭接受PNG)；网络慢可设1600

# === 电子单 Excel → PDF（LibreOffice 无头转换，转完走 VLM 通道）===
SOFFICE_PATH = os.getenv("SOFFICE_PATH", "").strip()  # 留空则自动探测常见安装位置
XLSX_PDF_TIMEOUT = int(os.getenv("XLSX_PDF_TIMEOUT", "120"))
# 同时最多开几个 LibreOffice 子进程（每个约 300MB）。机器上还跑着别的服务，默认 1 个。
XLSX_PDF_SLOTS = int(os.getenv("XLSX_PDF_SLOTS", "1"))
# 解析并发上限：一张票 = LibreOffice 子进程 + 全页渲染 + 最多两次模型调用。
# 8 人 × 30 单/人/天 ≈ 每分钟 1 张，2 个槽位足够；一旦看到排队，多半是模型或公司在抖。
DESK_CONCURRENCY = int(os.getenv("DESK_CONCURRENCY", "2"))
# 排到这个数就拒收（429），不再让每个人都等到超时
DESK_QUEUE_MAX = int(os.getenv("DESK_QUEUE_MAX", "200"))

# PDF 文字层少于此字符数即视为照片型扫描件，L1 退回 VLM 转录
L1_TEXT_MIN_CHARS = int(os.getenv("L1_TEXT_MIN_CHARS", "200"))

# === 数据库（可选；不填则不写库，只出 JSON/Excel）===
# 例: mysql+pymysql://user:pwd@host:3306/dbname
DB_URL = os.getenv("DB_URL", "").strip()

# === HTTP 服务（形态③）===
# 访问门禁已改为登录（见 auth.py / server.py 的 require_web / require_admin），不再用接口密钥。
UPLOAD_MAX_MB = int(os.getenv("UPLOAD_MAX_MB", "30"))

# === 站点取单（site_worker.py）===
# 分单审核台站点的地址与取单令牌：站点 Settings 里同名密钥的值必须与 WORKER_TOKEN 一致。
SITE_URL = os.getenv("SITE_URL", "").strip().rstrip("/")
WORKER_TOKEN = os.getenv("WORKER_TOKEN", "").strip()

# === 公司系统接口（主分单审核台·录入员检索 / 提交回传）===
# 契约已到（2026-09-26，IT《AMS录入接口调用说明》）：j9 AMS 录入接口，主单/分单各一把 key，
# POST + JSON + 请求头 X-Api-Key，公网 http://j9aiaeapi.justrightlog.com:18080（必须带端口）。
# 限流每把 key 10 次/秒、200 次/分、3600 次/时。MODE=live 时 search_mawb/submit_order 真连；
# 密钥只放 .env（COMPANY_MAWB_KEY / COMPANY_HAWB_KEY），绝不写死、绝不进前端。
COMPANY_API_MODE = (os.getenv("COMPANY_API_MODE", "mock") or "mock").strip().lower()
COMPANY_API_URL = os.getenv("COMPANY_API_URL", "").strip().rstrip("/")
COMPANY_MAWB_KEY = os.getenv("COMPANY_MAWB_KEY", "")   # 主单组（mawb//mawb2/）
COMPANY_HAWB_KEY = os.getenv("COMPANY_HAWB_KEY", "")   # 分单组（hawb/hawb2），两把互不通用
# 一次超时就换不掉的东西别拖成年人：读接口 10s、写接口 20s（写要落库），对端抖动(429/5xx/连不上)
# 退避后最多再试 J9_RETRIES-1 次；400 这类业务错不重试（是我们报文的问题）。
# 限流按文档 10 次/秒留两成余量——超了先在自己这边等，比让公司回 429 再人肉重点一次好。
J9_TIMEOUT_READ = float(os.getenv("J9_TIMEOUT_READ", "10"))
J9_TIMEOUT_WRITE = float(os.getenv("J9_TIMEOUT_WRITE", "20"))
J9_RETRIES = int(os.getenv("J9_RETRIES", "3"))
J9_RATE_PER_SEC = float(os.getenv("J9_RATE_PER_SEC", "8"))


# 支持的输入类型
IMG_EXTS = {".png", ".bmp", ".jpg", ".jpeg", ".tif", ".tiff", ".pdf"}
XLS_EXTS = {".xlsx", ".xlsm", ".xls"}
ALL_EXTS = IMG_EXTS | XLS_EXTS


def require_api_key() -> str:
    if not MODELSCOPE_API_KEY:
        raise SystemExit(
            "未配置魔搭令牌：请在 .env 或环境变量中设置 MODELSCOPE_API_KEY\n"
            "获取地址: https://modelscope.cn/my/myaccesstoken （需先绑定阿里云账号）"
        )
    return MODELSCOPE_API_KEY
