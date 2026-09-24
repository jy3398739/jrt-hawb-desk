# -*- coding: utf-8 -*-
"""模型自由切换的回归：预设解析、纯文本链路（绝不发图、没转录就报错）、.env 持久化。
不产生任何 API 调用——vlm_extract 的客户端整个换成假的。
"""
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

import config
import transcribe
import vlm_extract


class _FakeClient:
    """替掉 OpenAI 客户端：记下每次请求参数，回一段指定内容。"""

    def __init__(self, reply="{}"):
        self.calls = []
        self._reply = reply
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self._reply))])


class _ModelState:
    """保存/恢复 config 里的生效模型：用例都在一个进程里跑，函数会改模块全局。"""

    def __enter__(self):
        self.old = (config.VLM_MODEL_CHOICE, config.VLM_MODEL, config.MODEL_VISION)
        return self

    def __exit__(self, *exc):
        config.VLM_MODEL_CHOICE, config.VLM_MODEL, config.MODEL_VISION = self.old
        return False


class _ForcedVision:
    """临时设置 VLM_VISION 环境变量。预设里已无纯文本模型，纯文本链路改用
    "原始模型 id + VLM_VISION=0" 来测（机制本身保留，见 config.resolve_model）。"""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.old = os.environ.get("VLM_VISION")
        os.environ["VLM_VISION"] = self.value

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop("VLM_VISION", None)
        else:
            os.environ["VLM_VISION"] = self.old
        return False


def _with_fake_client(reply="{}"):
    fake = _FakeClient(reply)
    old = (vlm_extract._client, vlm_extract._client_channel)
    vlm_extract._client = fake
    # 渠道钉成当前端点：_get_client 见渠道没变就直接用这个假客户端，不碰真密钥
    vlm_extract._client_channel = config.vlm_base_url()
    return fake, old


def _restore_client(slot):
    vlm_extract._client, vlm_extract._client_channel = slot


def test_every_preset_carries_model_vision_and_label():
    for key, p in config.MODEL_PRESETS.items():
        assert isinstance(p["vision"], bool) and p.get("label"), key
        if p.get("base_url"):
            assert p["base_url"].startswith("https://"), f"{key} 的第三方端点必须是 https"
            assert p.get("api_key_env"), f"{key} 换了端点就得声明独立密钥环境变量"
        else:
            assert p["model"].count("/") == 1, f"{key} 的魔搭模型 id 应是 org/模型 两段: {p['model']}"
    # 契约：2026-09-21 用户定案删除魔搭三预设（intern-s2/qwen-flash/deepseek）与北龙（blsc-s2），
    # 只留官方书生API 一个；2026-09-24 起按用户要求加回第二个渠道（火山方舟 Doubao-Seed-2.1-Lite，
    # 接线前实测：模型名可直调且必须带版本后缀、小图两行字照抄正确=真读图、输出含 reasoning token）。
    # 剔除原因见 config 注释：纯文本模型收图不报错直接照编；北龙配额未开通（400）；
    # GLM-4V-Flash max_tokens 上限 1024 且难票上对调 MAWB/HAWB + 凭空编签发日期。
    # 纯文本链路机制仍保留：原始 id + VLM_VISION=0。预设表要变动请连同这条一起改。
    # 2026-09-24 顺序 = 用户点名顺序：默认 S2，其后 qwen3.8-flash、小米 MiMo；未被点名的方舟留末位。
    assert list(config.MODEL_PRESETS) == ["intern-s2-official", "qwen38-flash-bailian",
                                          "mimo-v2.6-flash", "doubao-seed-2-1-lite"], "预设键或顺序变了"
    assert all(p["vision"] for p in config.MODEL_PRESETS.values()), "预设里不该再有纯文本模型"
    assert config.resolve_model("intern-s2-official")["model"] == "intern-s2-preview"
    assert config.resolve_model("doubao-seed-2-1-lite")["model"] == "doubao-seed-2-1-lite-260915", \
        "方舟不带版本后缀的模型名会 404，预设必须钉死带日期的 id"
    assert config.MODEL_PRESETS["doubao-seed-2-1-lite"]["max_tokens"] >= 32768, \
        "它是思考型（实测 85 输出 token 里 57 个是 reasoning），没有下限就会整票空返回"
    assert config.resolve_model("INTERN-S2-OFFICIAL")["vision"] is True, "预设键要大小写不敏感"
    assert config.resolve_model("")["model"] == config.MODEL_PRESETS[config.DEFAULT_MODEL_KEY]["model"]


def test_official_preset_switches_endpoint_and_key_env_together():
    """官方书生API 渠道：切过去后端点、密钥来源、模型 id 三样一起换，切回自定义 id 三样一起回。
    真发请求不在测试里做——只验接线，密钥用假值。"""
    with _ModelState():
        old_key = config.MODELSCOPE_API_KEY
        config.MODELSCOPE_API_KEY = "test-key"
        try:
            config.set_model("SomeOrg/Custom-Vision")
            assert config.vlm_base_url() == config.MODELSCOPE_BASE_URL, "自定义 id 必须回落到魔搭端点"
            assert config.require_vlm_api_key() == "test-key"
            config.set_model("intern-s2-official")
            assert config.vlm_base_url() == "https://chat.intern-ai.org.cn/api/v1"
            assert config.VLM_MODEL == "intern-s2-preview"
            os.environ.pop("INTERNLM_API_KEY", None)
            assert not config.vlm_api_key_configured(), "没配官方密钥时 /health 不该报已配置"
            try:
                config.require_vlm_api_key()
                raise AssertionError("缺 INTERNLM_API_KEY 时居然放行了")
            except SystemExit as e:
                assert "INTERNLM_API_KEY" in str(e), f"报错要点名该配哪个变量: {e}"
            os.environ["INTERNLM_API_KEY"] = "sk-fake"
            assert config.require_vlm_api_key() == "sk-fake"
            assert config.vlm_api_key_configured()
            config.set_model("SomeOrg/Custom-Vision")
            assert config.vlm_base_url() == config.MODELSCOPE_BASE_URL, "切回魔搭渠道 must 换回魔搭端点"
            assert config.require_vlm_api_key() == "test-key"
        finally:
            config.MODELSCOPE_API_KEY = old_key
            os.environ.pop("INTERNLM_API_KEY", None)


def test_client_is_rebuilt_when_channel_switches():
    """热切模型换了服务商，缓存的 OpenAI 客户端必须跟着重建——否则整批票还发去旧端点。"""
    recorded = []

    class _FakeOpenAI:
        def __init__(self, **kw):
            recorded.append(kw)

    old_cls, old_client, old_channel = (vlm_extract.OpenAI, vlm_extract._client,
                                        vlm_extract._client_channel)
    vlm_extract.OpenAI = _FakeOpenAI
    vlm_extract._client = None
    vlm_extract._client_channel = None
    old_key = config.MODELSCOPE_API_KEY
    config.MODELSCOPE_API_KEY = "test-key"
    os.environ["INTERNLM_API_KEY"] = "sk-fake"
    try:
        with _ModelState():
            config.set_model("SomeOrg/Custom-Vision")
            vlm_extract._get_client()
            config.set_model("intern-s2-official")
            vlm_extract._get_client()
            vlm_extract._get_client()          # 渠道没再变：不该多构造一次
    finally:
        vlm_extract.OpenAI, vlm_extract._client = old_cls, old_client
        vlm_extract._client_channel = old_channel
        config.MODELSCOPE_API_KEY = old_key
        os.environ.pop("INTERNLM_API_KEY", None)
    assert len(recorded) == 2, f"端点变了要重建、没变不该重建，实际构造 {len(recorded)} 次"
    assert recorded[0]["base_url"] == config.MODELSCOPE_BASE_URL
    assert recorded[1]["base_url"] == "https://chat.intern-ai.org.cn/api/v1"
    assert recorded[1]["api_key"] == "sk-fake", "官方渠道必须用 INTERNLM_API_KEY，不能拿魔搭令牌去撞"


def test_raw_model_id_defaults_to_vision_and_env_can_force_it():
    info = config.resolve_model("Qwen/Qwen3.5-27B")
    assert info["model"] == "Qwen/Qwen3.5-27B" and info["vision"] is True, "未知 id 按老行为当有视觉"
    old = os.environ.get("VLM_VISION")
    os.environ["VLM_VISION"] = "0"
    try:
        assert config.resolve_model("Qwen/Qwen3.5-27B")["vision"] is False, "VLM_VISION=0 要能强制关掉视觉"
        assert config.resolve_model("intern-s2-official")["vision"] is False, "强制覆盖对预设也要生效（新模型兜底用）"
    finally:
        if old is None:
            os.environ.pop("VLM_VISION", None)
        else:
            os.environ["VLM_VISION"] = old


def test_reasoning_model_gets_a_bigger_token_budget():
    with _ModelState():
        config.set_model("intern-s2-official")
        assert config.model_max_tokens() >= 32768, "Intern-S2 实测单票推理烧掉 19309 token，8192/16384 都不够"
        config.set_model("custom/Not-A-Preset")
        assert config.model_max_tokens() == config.VLM_MAX_TOKENS, "没配 max_tokens 的预设/自定义 id 不该被抬高"


def test_text_only_model_never_sends_images_and_needs_a_transcript():
    with _ModelState(), _ForcedVision("0"):
        config.set_model("deepseek-ai/DeepSeek-V4-Pro")   # 预设已无纯文本模型：原始 id + 强制关视觉
        missing = Path(tempfile.gettempdir()) / "不存在的票.pdf"
        try:
            vlm_extract.extract_one(missing, transcript=None)
            raise AssertionError("纯文本模型没有任何转录也去提了")
        except RuntimeError as e:
            assert "不支持图片" in str(e), e
        fake, old = _with_fake_client("{}")
        try:
            tr = {"lines": [{"i": 1, "text": "MAWB 016-72884490", "bbox": [0, 0, 10, 10]}],
                  "full_text": "MAWB 016-72884490"}
            data = vlm_extract.extract_one(missing, transcript=tr)   # 文件不存在：一旦发图就会炸
            assert set(data) == set(vlm_extract.FIELDS)
        finally:
            _restore_client(old)
        assert fake.calls, "没发起请求"
        req = fake.calls[0]
        blocks = req["messages"][0]["content"]
        assert not [b for b in blocks if b["type"] == "image_url"], "纯文本模型收到了图片，它会顺着编内容"
        text = blocks[-1]["text"]
        assert "本次没有图片" in text and "016-72884490" in text, "转录文本没随 prompt 下发"
        assert req["model"] == "deepseek-ai/DeepSeek-V4-Pro"


def test_vision_path_still_sends_the_image():
    """回归：视觉模型照旧发图——别为了纯文本路径把原来的图片路径改没了。"""
    with tempfile.TemporaryDirectory() as d:
        png = Path(d) / "t.png"
        Image.new("RGB", (40, 40), "white").save(png)
        with _ModelState():
            config.set_model("intern-s2-official")
            fake, old = _with_fake_client("{}")
            try:
                vlm_extract.extract_one(png, transcript=None)
            finally:
                _restore_client(old)
        blocks = fake.calls[0]["messages"][0]["content"]
        assert blocks[0]["type"] == "image_url", "视觉模型反而没收到票面图片"
        assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_mimo_preset_pins_id_and_carries_thinking_off():
    """小米 MiMo-V2.6-Flash 接入前三项实测（2026-09-24）：
    ① 模型 id 大小写敏感——`MiMo-V2.6-Flash` 回 400 Unsupported model，只有全小写能用；
    ② 默认开思考：同 5 票 49.6s/票 vs 关思考 7.5s/票，质量没提升 ⇒ 预设该带上关思考；
    ③ 官方文档写的 enable_thinking:false 实测无效（照样烧 reasoning token），
       能归零的是 extra_body 里的 thinking.type=disabled ⇒ 参数得随预设一起发出去。"""
    with _ModelState():
        config.set_model("mimo-v2.6-flash")
        assert config.VLM_MODEL == "mimo-v2.6-flash", "预设必须钉全小写 id，大写会 400"
        assert config.MODEL_VISION is True, "它真读图（420x220 测试票面三行照出来）"
        assert config.vlm_base_url() == "https://api.xiaomimimo.com/v1"
        assert config.model_extra_body() == {"thinking": {"type": "disabled"}}, \
            "预设没声明关思考参数——不带的(default)话 MiMo 会跑 49.6s/票那一档"
        config.set_model("intern-s2-official")
        assert config.model_extra_body() == {}, "没声明该参数的预设，请求里不该凭空多出东西"


def test_qwen_preset_pins_id_and_carries_thinking_off():
    """阿里云百炼 Qwen3.8-Flash 接入前实测（2026-09-24）：
    ① 清单里的 id 就是 `qwen3.8-flash`，`qwen3.8-flash-next` 那类写法回 404 ⇒ 预设钉死这一个；
    ② 真读图（测试票面照抄正确），也是思考型 ⇒ 与 S2/MiMo 同样要 max_tokens 下限；
    ③ 关思考两种写法都认（{"enable_thinking":false} 与 {"thinking":{"type":"disabled"}}），
       取文档里通用的 enable_thinking:false；关思考后 20 票实测与 S2 同速、比 MiMo 长尾轻。"""
    with _ModelState():
        config.set_model("qwen38-flash-bailian")
        assert config.VLM_MODEL == "qwen3.8-flash", "百炼只服务这一个 id，写 qwen3.8-flash-next 会 404"
        assert config.MODEL_VISION is True
        assert config.vlm_base_url() == "https://dashscope.aliyuncs.com/compatible-mode/v1"
        assert config.model_extra_body() == {"enable_thinking": False}, \
            "预设没声明关思考——带思考那一档会在输出里多烧几千 reasoning token"
        assert config.MODEL_PRESETS["qwen38-flash-bailian"]["max_tokens"] >= 32768, \
            "它是思考型，没有下限会 finish_reason=length、content 空"
        os.environ.pop("DASHSCOPE_API_KEY", None)
        try:
            config.require_vlm_api_key()
            raise AssertionError("没配 DASHSCOPE_API_KEY 时居然放行了")
        except SystemExit as e:
            assert "DASHSCOPE_API_KEY" in str(e), f"报错要点名该配哪个变量: {e}"


def test_preset_extra_body_reaches_every_model_call():
    """接线要接到真发出去的那次调用：extra_body 没进 create 的 kwargs，跑批就是慢 6 倍那一档。
    三个入口都验（L2 提取 / L1 转录 / 审核台问模型）；断言只回显 extra_body 一项——kwargs 里有 api_key。"""
    with tempfile.TemporaryDirectory() as d:
        png = Path(d) / "t.png"
        Image.new("RGB", (40, 40), "white").save(png)
        with _ModelState():
            config.set_model("mimo-v2.6-flash")
            fake, old = _with_fake_client("{}")
            try:
                vlm_extract.extract_one(png, transcript=None)
            finally:
                _restore_client(old)
        assert fake.calls, "没发起请求"
        eb = fake.calls[0].get("extra_body")
        assert eb == {"thinking": {"type": "disabled"}}, f"extra_body 没随 L2 请求发出: {eb!r}"

        # L1 转录（扫描件）是另一个入口、另一个客户端缓存，漏接就等于扫描件仍跑慢 6 倍那一档
        tfake = _FakeClient('{"lines":[]}')
        told = (transcribe._client, transcribe._client_channel)
        try:
            with _ModelState():
                config.set_model("mimo-v2.6-flash")
                # 渠道要在切完模型之后再钉：不然 _get_client 认为渠道变了，会去构造真客户端要密钥
                transcribe._client = tfake
                transcribe._client_channel = config.vlm_base_url()
                transcribe.transcribe_one(png)
        finally:
            transcribe._client, transcribe._client_channel = told
        teb = (tfake.calls[0] or {}).get("extra_body")
        assert teb == {"thinking": {"type": "disabled"}}, f"extra_body 没随 L1 转录请求发出: {teb!r}"

        # 审核台的"问模型"是第三个入口（每次现构造客户端），漏接就表现为提取快、问答慢
        import ask_vision
        real_openai = ask_vision.OpenAI
        seen = []

        def _spy(**kw):
            seen.append(kw)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="OK"))])

        ask_vision.OpenAI = lambda **kw: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_spy)))
        # 这个入口每次现构造客户端，构造参数里就要读密钥（假客户端拦不住）——给个假值
        os.environ["MIMO_API_KEY"] = "sk-fake"
        try:
            with _ModelState():
                config.set_model("mimo-v2.6-flash")
                ask_vision.ask(png, "这票主单号是多少？")
        finally:
            ask_vision.OpenAI = real_openai
            os.environ.pop("MIMO_API_KEY", None)
        aeb = (seen[0] if seen else {}).get("extra_body")
        assert aeb == {"thinking": {"type": "disabled"}}, f"extra_body 没随问模型请求发出: {aeb!r}"


def test_scan_transcription_refuses_without_vision():
    with _ModelState(), _ForcedVision("0"):
        config.set_model("deepseek-ai/DeepSeek-V4-Pro-0813")   # 同上：原始 id + 强制关视觉
        try:
            transcribe.transcribe_one(Path(tempfile.gettempdir()) / "扫描件.png")
            raise AssertionError("纯文本模型居然去转录扫描件了")
        except RuntimeError as e:
            assert "不支持图片" in str(e) and "intern-s2-official" in str(e), e


def test_api_clients_are_bounded_by_timeout_and_a_single_retry_layer():
    """挂住的模型不能把整批拖成静默等待：三个入口的 OpenAI 客户端都要显式带 VLM_TIMEOUT
    并关掉 SDK 自带重试（重试只留 VLM_RETRIES 一层）。实测 deepseek-pro-0813 小 prompt 1.6s
    就回、真实票 prompt 14 分钟不回，两层重试相乘最坏是 90 分钟静默。"""
    import ask_vision
    recorded = []

    class _FakeOpenAI:
        def __init__(self, **kw):
            recorded.append(kw)
            raise RuntimeError("构造后立刻炸，阻止真发请求")

    mods = (vlm_extract, transcribe, ask_vision)
    saved = [(m, m.OpenAI, getattr(m, "_client", None), getattr(m, "_client_channel", None))
             for m in mods]
    for m, _, _, _ in saved:
        m.OpenAI = _FakeOpenAI
        if hasattr(m, "_client"):
            m._client = None
            m._client_channel = None
    try:
        with _ModelState():
            config.set_model("intern-s2-official")   # ask_vision 先过视觉守卫才会构造客户端
            # 服务器上的 .env 故意不放密钥，构造客户端会撞 require_vlm_api_key 的 SystemExit——
            # 这里垫个假令牌：反正下面用的是假 OpenAI，真值无关紧要。
            os.environ["INTERNLM_API_KEY"] = "sk-fake"
            try:
                for m in (vlm_extract, transcribe):
                    try:
                        m._get_client()
                    except RuntimeError as e:
                        assert "构造后立刻炸" in str(e), e
                try:
                    ask_vision.ask(Path(tempfile.gettempdir()) / "x.png", "主单号读一下")
                except RuntimeError as e:
                    assert "构造后立刻炸" in str(e), e
                else:
                    raise AssertionError("ask_vision 没构造客户端")
            finally:
                os.environ.pop("INTERNLM_API_KEY", None)
    finally:
        for m, oa, oc, och in saved:
            m.OpenAI = oa
            if hasattr(m, "_client"):
                m._client, m._client_channel = oc, och
    assert len(recorded) == 3, f"三个入口都要有界客户端，实际只构造了 {len(recorded)} 次"
    for kw in recorded:
        # 断言消息只回显 timeout/max_retries/base_url：整个 kwargs 里有 api_key，
        # 失败时不能把 token 打进日志
        assert kw.get("timeout") == config.VLM_TIMEOUT, \
            f"没带超时，挂住的请求会无限等: timeout={kw.get('timeout')!r}"
        assert kw.get("max_retries") == 0, \
            f"SDK 层重试没关，会和 VLM_RETRIES 相乘: max_retries={kw.get('max_retries')!r}"
        assert kw.get("base_url") == config.vlm_base_url(), \
            f"客户端没按当前渠道的端点构造: base_url={kw.get('base_url')!r}"


def test_persist_model_choice_keeps_the_dotenv_file_mode():
    """服务器上 .env 是 600（里面有令牌）：原子替换不能把权限放宽。
    Windows 的 chmod 只认只读位，模式断言没意义，这条实际由 VM 上的回归覆盖。"""
    if os.name != "posix":
        return
    with tempfile.TemporaryDirectory() as d:
        env = Path(d) / ".env"
        env.write_text("MODELSCOPE_API_KEY=\n", encoding="utf-8")
        os.chmod(env, 0o600)
        old = config.ENV_FILE
        config.ENV_FILE = env
        try:
            config.persist_model_choice("intern-s2-official")
            mode = env.stat().st_mode & 0o777
            assert mode == 0o600, f"切模型把 .env 权限从 600 改成了 {oct(mode)}"
        finally:
            config.ENV_FILE = old


def test_persist_model_choice_rewrites_only_that_line():
    with tempfile.TemporaryDirectory() as d:
        env = Path(d) / ".env"
        env.write_text("INTERNLM_API_KEY=sk-xxx\n"
                       "# VLM_MODEL=intern-s2-official\n"
                       "SOME_OTHER_KEY=k1\n", encoding="utf-8")
        old = config.ENV_FILE
        config.ENV_FILE = env
        try:
            config.persist_model_choice("intern-s2-official")
            text = env.read_text(encoding="utf-8")
            assert "INTERNLM_API_KEY=sk-xxx" in text and "SOME_OTHER_KEY=k1" in text, "别的行被动了"
            assert "# VLM_MODEL=intern-s2-official" in text, "注释掉的示例行不该当配置行改"
            assert "\nVLM_MODEL=intern-s2-official\n" in text, f"新行没写上: {text!r}"
            config.persist_model_choice("SomeOrg/Custom-Vision")
            lines = [l for l in env.read_text(encoding="utf-8").splitlines()
                     if l.strip().startswith("VLM_MODEL=")]
            assert lines == ["VLM_MODEL=SomeOrg/Custom-Vision"], f"第二次切换要就地替换而不是再追加一行: {lines}"
            assert not (Path(d) / ".env.tmp").exists(), "临时文件要原子替换掉，别留在旁边"
            env.write_bytes("INTERNLM_API_KEY=sk-y\r\nSOME_OTHER_KEY=k2\r\n".encode("utf-8"))
            config.persist_model_choice("intern-s2-official")
            raw = env.read_bytes()
            assert raw.count(b"\n") >= 3 and raw.count(b"\r\n") == raw.count(b"\n"), \
                f"原本 CRLF 的 .env 被翻成了混合行尾（Windows 上 write_text 会悄悄全转 CRLF）: {raw!r}"
        finally:
            config.ENV_FILE = old
