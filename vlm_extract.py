# -*- coding: utf-8 -*-
"""L2 结构化原文层：魔搭 Intern-S2-Preview 直出第一遍 39 字段 JSON。
保真口径：所有字段值必须逐字取自票面/转录文本，禁止任何改写
（不还原国家码、不统一大小写、不去数字空格、不补全公司名空格、不纠正疑似印刷错误）。
图片/PDF base64 内联，OpenAI 兼容协议；失败自动重试。
"""
import base64, json, io, re
from pathlib import Path
from PIL import Image
from openai import OpenAI

import config
import retry

PROMPT_HEAD = r"""你是航空运单(HAWB)信息提取员。下面给你两样东西：①分单图片 ②该图片的逐字转录文本（OCR原始结果）。
请提取39个字段，输出纯JSON（不要markdown代码块，不要解释文字）。

【保真铁律 —— 第一遍原文口径，违反即为错误】
A. 每个字段值必须是票面文字的逐字摘录，必须能在"逐字转录文本"中找到完全相同的字符串（忽略换行差异）。
B. 禁止任何形式的改写：
   - 国家照抄票面写法：票面是"CN"/"SE"两字母码就填两字母码，票面是"Italy"就填"Italy"，票面是"ITALY"才填"ITALY"，严禁互相转换；
   - 主单号照抄票面数字分组：票面"999-0686 5456"带空格就带空格，"020|PEK|18124470"竖线机场码格式则去掉PEK段输出"020-18124470"（仅此一种允许的改写）；
   - 公司名/地址照抄票面原样，禁止自行加空格、改标点、纠正疑似印刷错误（如 CO.1 不得改成 CO.,）；
   - 电话/传真/EORI 照抄票面字符与空格，不做格式化；
   - 城市照抄票面原文（如 Milano 不得改成 MILAN）。
C. 不猜不补：找不到的字段字符串填""，数值填null。只有 SHIPPER_INFO/CONSIGNEE_INFO 两个合并串允许你按规定格式拼接（标注：该串为系统拼接，非票面原文行）。
D. CREATE_TIME 是唯一允许的格式转换：把票面签发日期统一成"YYYY-MM-DD"（如 29-MAY-2024→2024-05-29）；只取签发日期（Executed at ... on / 签发日期 栏），禁止取航班日期。
   两位年要放对位置："20-Sep-26" = 2026年9月20日（日-月-年），26 是年不是日，严禁写成 2026-09-26；"19-9月-26" 同理 = 2026-09-19。

【字段定义】
1. MAWB_NO - 主单号：必须是"3位承运人数字前缀+8位数字"共11位（前缀如999国航CA、020汉莎LH、131日航JL、074荷航KL、016美联航UA、057法航AF、125英航BA）。
   ① 票面空格分组(如"020-1902 5661")必须整组照抄为"020-1902 5661"(凑满11位)，严禁只取空格前的"020-1902"；
   ② 左上竖线格式"016 | BJS | 72884490"(3位数字|3字母机场码|8位数字)是MAWB，丢弃中间机场码，输出"016-72884490"；
   ③ 右上角/页底孤立的8位数字(无3位前缀,如15892188)不是MAWB，那通常是HAWB；
   ④ 可用"By First Carrier"2字母承运人码交叉验证(UA→016、CA→999、JL→131、LH→020)；
   ⑤ 宁可留空也不要填不足11位的截断号。
2. HAWB_NO - 分单号：货代编号，通常带字母前缀(SIA-8240 5250、BJS-7288 4490、UUB-0003 3165、TAO-5808 9795)或字母数字串(BJS7062889、THI142400099)。带空格分组的分单号必须整组照抄(如"TAO-5808 9795"严禁截成"TAO-5808")；MAWB同行其后的"字母前缀-数字 数字"组合通常是HAWB(如"020-1902 5661 SJCTAO TAO-5808 9795"中TAO-5808 9795是HAWB，中间SJCTAO是航线代码不要)。
3. SHIPPER_INFO - 发货人合并串(系统拼接):"公司名, 完整地址, TEL:/FAX: 号码"，各片段逐字摘自票面；TEL与FAX同一号码记"TEL/FAX: 号码"
4. CONSIGNEE_INFO - 收货人合并串，格式同上
5. ORIGIN_NAME - 起运港名称（票面原文）
6. TO1 - 航路第一跳：只取"To:"栏后的3字母机场码(FRA/MXP/GOT/DXB)；"By First Carrier:"后的2字母承运人码(JL/LH/AF)严禁填入
7. TO2 - 航路第二跳3字母码，无则空
8. TO3 - 航路第三跳3字母码，无则空
9. DEST_NAME - 目的港名称（票面原文，取首行）
10. GOODS_INFO - 货物描述（品名+尺寸，照抄票面文字）
11. GOODS_HS_CODE - 海关编码（票面货物描述里 HS CODE/HS Codes 标签后的号码，只填号码本身，点号去掉，多个用英文逗号连接，如"8471300000,8517120000"；票面没有则填空串）
12. PIECES - 件数整数（各行件数列之和）
13. WEIGHT - 毛重浮点数（只取Gross Weight栏K行，如2517.0；禁止取费率25.50/体积重）
14. SLAC - 票面显式"SLAC n"填n，否则默认等于PIECES
15. CREATE_TIME - 签发日期YYYY-MM-DD（见铁律D）
16. SEND_STATUS - 固定"PENDING"
17-28 发货人子字段：SHIPPER_INFO_COMP_NAME公司名 / SHIPPER_INFO_COMP_ADDRESS完整地址(逐字,含城市邮编国家) / SHIPPER_INFO_CITY城市(原文) / SHIPPER_INFO_COUNTRY国家(票面原样!) / SHIPPER_INFO_STATE州省 / SHIPPER_INFO_POSTAL邮编 / SHIPPER_INFO_TEL电话(原文) / SHIPPER_INFO_FAX传真(原文,与电话同号则填同值) / SHIPPER_INFO_EORI / SHIPPER_INFO_AEO / SHIPPER_INFO_EMAIL / SHIPPER_INFO_TAX_ID税号(票面 USCI/统一社会信用代码/CNPJ/RFC/GST/TAX ID/VAT NO 标签后的号码，照抄)
29-40 收货人子字段（注意城市字段拼写为CITTY双T）：CONSIGNEE_INFO_COMP_NAME / CONSIGNEE_INFO_COMP_ADDRESS / CONSIGNEE_INFO_CITTY城市(原文!) / CONSIGNEE_INFO_COUNTRY国家(票面原样!) / CONSIGNEE_INFO_STATE / CONSIGNEE_INFO_POSTAL / CONSIGNEE_INFO_TEL / CONSIGNEE_INFO_FAX / CONSIGNEE_INFO_EORI(常印页底customs区如SE5563646560) / CONSIGNEE_INFO_AEO / CONSIGNEE_INFO_EMAIL / CONSIGNEE_INFO_TAX_ID税号(同上)
其他规则：纯数字账号(如6409583920)不是公司名也不是电话；税号只填 _TAX_ID —— "USCI"+18位(如USCI: 911201117706402073)是中国统一社会信用代码，"CNPJ"是巴西税号，它们不是电话也不是 EORI 也不是 AEO（EORI 一定是两位国家字母开头的字母数字串）；同一格内税号紧跟电话时（如"TE +862258388999 USCI: 911201117706402073"），TEL 只取 +862258388999，税号填进 TAX_ID，两个都要填，谁都不能丢；页底 customs 区的 VAT/EORI 若属于收货人，填收货人 _EORI；TEL/FAX 只填号码本身（+、区号、数字及其间的空格横线），票面同一格里号码后面紧跟的人名、CNPJ/VAT/税号、公司名一律不要带进 TEL/FAX 字段（如票面"TE +559240091129 Carla Os CNPJ: 00280273000137"，TEL 只取 +559240091129，CNPJ 那串取进 TAX_ID）；TEL栏无真实号码时留空；公司名取完整勿因换行截断；TEL/FAX共标签时两字段都填；子字段票面没有则填空串；PIECES/SLAC整数、WEIGHT浮点数。
只输出JSON。"""

PROMPT_TRANSCRIPT = "\n\n【逐字转录文本（行号: 内容；字段值必须来自这些行）】\n"

PROMPT_TEXT_ONLY = """【本次没有图片】当前模型不支持图片输入，票面图片未随请求下发，只有下面的逐字转录文本。
下文凡提到"分单图片"的地方一律以转录文本为准；字段值仍必须逐字取自它。

"""

# ── 主单（MAWB）侧：字段表由 master_fields 单一真源生成，prompt 与提交体不可能各说一套 ──
import master_fields as mf  # noqa: E402  （放在常量之后：prompt 需要用到上面的转录段）

_MASTER_COLS_TEXT = "\n".join(f"- {c}（{lab}）" for c, lab, _g in mf.MASTER_FIELDS)
_MASTER_COUNT = len(mf.MASTER_COLS)

MASTER_PROMPT_HEAD = f"""你是航空主单(MAWB)资料提取员。下面给你的是公司系统里这条主单的资料文本（每行形如「列名: 值」，另有几坨拼接资料块），本次没有票面图片，也没有分单。
请只输出下面列出的 {_MASTER_COUNT} 个主单列，输出纯JSON（不要markdown代码块、不要解释文字、不要新增列）。

【主单口径 —— 与分单不同】
① 这是主单资料，不是分单票面：下面清单之外的东西一律不出现，尤其不要凭空造列、不要从发货人城市之类推算任何值。
② 通知人（NOTIFY 开头那些列）是独立一组，只能从 NOTIFY_INFO 那段资料里取；严禁把通知人并进收货人 CONSIGNEE_*，也严禁反过来。
③ 主单表没有税号列，公司把中国的统一社会信用代码放 EORI：USCI（18 位字母数字）填进**同主体**那一组的 *_INFO_EORI（发货人的号进 SHIPPER_INFO_EORI，收货人的进 CONSIGNEE_INFO_EORI，通知人的进 NOTIFY_INFO_EORI）。CNPJ/RFC/GST/VAT 一类其它税号不要塞进 EORI（除 USCI 外，EORI 只能是两位国家字母开头的号码），留空由质检提示人工定夺。
④ 列名照公司现在的写法：通知人国家列叫 NOTIFY_INFO_COUNTRY，收货人城市叫 CONSIGNEE_INFO_CITY（单 T）——列名以清单为准，别自创也别改拼写。
⑤ GOODS_INFO_HSCODE 只填号码本身：资料里的「HS CODE:8526109」取 8526109，多个码用英文逗号连接；不要把 "HS CODE" 这类标签一起抄进值里。

【保真铁律】
A. 每个字段值必须是资料文本的逐字摘录，能在上面找到完全相同的字符串（忽略换行与多余空白差异）。
B. 禁止改写：国家照抄原文写法、公司名/地址照抄原样（不加空格、不改标点、不纠正疑似印刷错误）、电话/传真/EORI 照抄字符。
C. 找不到的列填 null，不要猜、不要用常识补全。
D. 唯一允许的改写：主单号统一成「3位数字-横杠-8位数字」。

【列清单】
{_MASTER_COLS_TEXT}

只输出JSON。"""


def master_prompt(transcript: dict) -> str:
    """主单这次的完整提示词。单独暴露出来是给缓存算指纹用的：改了提示词或列面，
    旧解析结果必须失效，否则页面会一直投喂过期结果。"""
    lines = (transcript or {}).get("lines") or []
    body = "\n".join(f"{x['i']}: {x['text']}" for x in lines)
    return MASTER_PROMPT_HEAD + PROMPT_TRANSCRIPT + body


def extract_master(transcript: dict, model: dict | None = None) -> dict:
    """主单 L2：公司资料文本 → 36 列原文口径。

    主单侧没有版式原件，所以**任何模型都不下发图片**（视觉模型也一样）——给一张不相干的
    图或空图只会诱发它照图编字段。没有资料文本就直接失败，不能让模型凭空生成一条主单。
    模型默认取 `MASTER_VLM_MODEL`（主单链独立渠道，见 config 里的实测注记）。"""
    if not (transcript or {}).get("lines"):
        raise RuntimeError("主单资料文本为空，无从解析：公司接口没返回这条主单的资料")
    model = model or config.master_model_bundle()
    return _chat_json(master_prompt(transcript), [], mf.MASTER_COLS, "主单", numeric=("SLAC",),
                      client=_client_for(model), model_id=model["model"],
                      max_tokens=model["max_tokens"], extra_body=model["extra_body"])


_extra_clients: dict[str, object] = {}


def _client_for(bundle: dict):
    """按端点各存一份客户端：主单链跑在另一个渠道时不能蹭分单那份，两条链同时在解析
    也不能互相把对方的渠道顶掉。同端点就复用审核台那份（热切模型时不会各留一份僵尸连接）。"""
    if bundle["base_url"] == config.vlm_base_url():
        return _get_client()
    c = _extra_clients.get(bundle["base_url"])
    if c is None:
        c = OpenAI(api_key=config.master_api_key(bundle), base_url=bundle["base_url"],
                   timeout=config.VLM_TIMEOUT, max_retries=0)
        _extra_clients[bundle["base_url"]] = c
    return c


def _chat_json(prompt: str, urls: list[str], want: list[str], label: str,
               numeric: tuple = ("PIECES", "SLAC", "WEIGHT"), client=None,
               model_id: str | None = None, max_tokens: int | None = None,
               extra_body: dict | None = None) -> dict:
    """发一次提取请求并解析 JSON，缺列补齐（数值列补 None，其余补空串）。失败按 VLM_RETRIES 重试。"""
    client = client or _get_client()
    content = [{"type": "image_url", "image_url": {"url": u}} for u in urls]
    content.append({"type": "text", "text": prompt})
    last_err = None
    timeout_tries = 0
    for attempt in range(config.VLM_RETRIES):
        try:
            resp = client.chat.completions.create(
                model=model_id or config.VLM_MODEL,
                messages=[{"role": "user", "content": content}],
                max_tokens=max_tokens if max_tokens is not None else config.model_max_tokens(),
                temperature=0.0,
                extra_body=config.model_extra_body() if extra_body is None else extra_body,
            )
            text = resp.choices[0].message.content.strip()
            if text.startswith("```"):
                text = "\n".join(l for l in text.split("\n") if not l.strip().startswith("```"))
            data = json.loads(text)
            # L2 保真：不对任何字段做格式化（归一化全部留到 L3/清洗）
            for f in want:
                if f not in data:
                    data[f] = None if f in numeric else ""
            return data
        except Exception as e:
            last_err = e
            if "timeout" in type(e).__name__.lower():
                timeout_tries += 1
                # 慢模型重跑一遍还是五分钟，再等一轮只是把人钉在等待上；超时最多试 VLM_TIMEOUT_TRIES 次
                if timeout_tries >= config.VLM_TIMEOUT_TRIES:
                    break
            if attempt < config.VLM_RETRIES - 1:
                retry.wait(attempt)
    raise RuntimeError(f"L2 提取失败 {label}: {last_err}")

FIELDS = [
    "MAWB_NO", "HAWB_NO", "SHIPPER_INFO", "CONSIGNEE_INFO", "ORIGIN_NAME",
    "TO1", "TO2", "TO3", "DEST_NAME", "GOODS_INFO", "GOODS_HS_CODE",
    "PIECES", "WEIGHT", "SLAC", "CREATE_TIME", "SEND_STATUS",
    "SHIPPER_INFO_COMP_NAME", "SHIPPER_INFO_COMP_ADDRESS", "SHIPPER_INFO_CITY",
    "SHIPPER_INFO_COUNTRY", "SHIPPER_INFO_STATE", "SHIPPER_INFO_POSTAL",
    "SHIPPER_INFO_TEL", "SHIPPER_INFO_FAX", "SHIPPER_INFO_EORI",
    "SHIPPER_INFO_AEO", "SHIPPER_INFO_EMAIL", "SHIPPER_INFO_TAX_ID",
    "CONSIGNEE_INFO_COMP_NAME", "CONSIGNEE_INFO_COMP_ADDRESS", "CONSIGNEE_INFO_CITTY",
    "CONSIGNEE_INFO_COUNTRY", "CONSIGNEE_INFO_STATE", "CONSIGNEE_INFO_POSTAL",
    "CONSIGNEE_INFO_TEL", "CONSIGNEE_INFO_FAX", "CONSIGNEE_INFO_EORI",
    "CONSIGNEE_INFO_AEO", "CONSIGNEE_INFO_EMAIL", "CONSIGNEE_INFO_TAX_ID",
]

_client = None
_client_channel = None


def _get_client():
    """客户端按端点缓存：审核台热切模型可能连渠道一起换（魔搭↔北龙），端点一变就重建，
    绝不让整批票继续跑在旧渠道的客户端上。密钥只在构造时取，缺密钥照常抛 SystemExit。"""
    global _client, _client_channel
    channel = config.vlm_base_url()
    if _client is None or _client_channel != channel:
        _client = OpenAI(api_key=config.require_vlm_api_key(), base_url=channel,
                         timeout=config.VLM_TIMEOUT, max_retries=0)
        _client_channel = channel
    return _client


def _encode(img: Image.Image) -> str:
    """PIL 图 → data URL。默认 PNG base64（魔搭接受）；配置 IMAGE_LONG_EDGE>0 时缩放转 JPEG。"""
    if img.mode != "RGB":
        img = img.convert("RGB")
    edge = config.IMAGE_LONG_EDGE
    if edge and max(img.size) > edge:
        w, h = img.size
        s = edge / max(w, h)
        img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        mime = "jpeg"
    else:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        mime = "png"
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/{mime};base64,{b64}"


def img_to_data_urls(path) -> list[str]:
    """读图片/PDF，每页一个 data URL。多页 PDF 全部下发，避免第二页字段丢失。

    **逐页渲染 → 编码 → 丢**：原来先把每页都攒成 PIL 对象再整批编码，一张四页票同时握着
    pixmap + PIL + base64 三份。省下来的不是流量，是内存闸（撞满 MemoryMax 时 systemd 杀的是
    整个服务，不是那一张票）。这里不降采样——IMAGE_LONG_EDGE 是过去所有模型横评的前提，
    动它属于改准确率口径，不属于"行为不变的加固"。"""
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        import pymupdf
        out = []
        doc = pymupdf.open(str(path))
        try:
            for page in doc:
                pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))
                out.append(_encode(Image.frombytes("RGB", (pix.width, pix.height), pix.samples)))
        finally:
            doc.close()
        return out
    with Image.open(path) as img:
        return [_encode(img)]


def extract_one(path, transcript: dict | None = None) -> dict:
    """L2 提取单张图片/PDF，返回 39 字段 dict（保真原文口径）。
    传入 L1 转录结果时，转录文本随 prompt 下发，强制字段值逐字来自转录。
    当前模型无视觉（config.MODEL_VISION=False）时不下发票面图片、只吃转录文本——
    没有转录就直接报错：纯文本模型收图不会拒答而是顺着编（实测 DeepSeek-V4-Pro），
    宁可这张票失败，也不能拿编出来的字段当真值。"""
    path = Path(path)
    vision = config.MODEL_VISION
    has_tr = bool(transcript and transcript.get("lines"))
    if not vision and not has_tr:
        raise RuntimeError(
            f"当前模型不支持图片输入（{config.VLM_MODEL}），这张票又没有文字层转录："
            "请在审核台顶栏或 .env 的 VLM_MODEL 切到视觉模型（intern-s2-official），"
            "或改用电子单、文字层 PDF 再提取")
    prompt = PROMPT_HEAD
    if not vision:
        prompt = PROMPT_TEXT_ONLY + prompt
    if has_tr:
        body = "\n".join(f"{x['i']}: {x['text']}" for x in transcript["lines"])
        prompt += PROMPT_TRANSCRIPT + body
    urls = img_to_data_urls(path) if vision else []
    return _chat_json(prompt, urls, FIELDS, path.name)
