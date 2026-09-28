# -*- coding: utf-8 -*-
"""主单链独立模型（B）：主单默认走 qwen3.8-flash，分单仍走审核台选的那个。

理由不是"快"这么简单——同一条主单输入实测 intern-s2-preview 取到 2/36 列、qwen3.8-flash
取到 17/36 列。两边同时解析时不能互相顶掉渠道，所以客户端按端点各存一份。
回归全程离线：MASTER_VLM_MODEL 被 run_tests 钉成空串（= 跟分单同渠道），客户端也是假的。
"""
import os

import config
import vlm_extract


def _bundle_of(choice):
    old = config.MASTER_VLM_MODEL
    config.MASTER_VLM_MODEL = choice
    try:
        return config.master_model_bundle()
    finally:
        config.MASTER_VLM_MODEL = old


def test_master_chain_has_its_own_default_preset():
    assert config.DEFAULT_MASTER_MODEL_KEY == "qwen38-flash-bailian"
    b = _bundle_of(config.DEFAULT_MASTER_MODEL_KEY)
    assert b["model"] == "qwen3.8-flash" and b["base_url"].startswith("https://dashscope")
    assert b["api_key_env"] == "DASHSCOPE_API_KEY" and b["max_tokens"] >= 8192
    assert b["extra_body"] == {"enable_thinking": False}, "主单是纯文本任务，思考开着既慢又贵"


def test_empty_master_choice_follows_the_house_channel():
    """空 = 不独立，跟分单同一个模型：回归与"就想两边一起换"的部署都走这条路。"""
    b = _bundle_of("")
    assert b["model"] == config.VLM_MODEL and b["base_url"] == config.vlm_base_url()
    assert b["api_key_env"] is None or b["api_key_env"] == config._active_preset().get("api_key_env")


def test_unknown_master_choice_is_carried_as_custom_id():
    b = _bundle_of("some-org/some-model")
    assert b["model"] == "some-org/some-model" and b["vision"] is True


def test_extract_master_uses_the_master_client_not_the_global_one():
    """两条链各自发请求：主单那份走 master bundle 的端点，且绝不因为分单在跑就串渠道。"""
    calls = {"global": 0, "master": 0}

    def fake(reply):
        from types import SimpleNamespace

        class C:
            def __init__(self):
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

            def _create(self, **kw):
                calls[reply] += 1
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content='{"MAWB_NO": "176-62400004"}'))],
                    usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))
        return C()

    old = (vlm_extract._client, vlm_extract._client_channel, dict(vlm_extract._extra_clients))
    vlm_extract._client, vlm_extract._client_channel = fake("global"), config.vlm_base_url()
    mb = _bundle_of(config.DEFAULT_MASTER_MODEL_KEY)
    vlm_extract._extra_clients[mb["base_url"]] = fake("master")
    tr = {"lines": [{"i": 1, "text": "MASTER_NO: 176-62400004"}], "full_text": "MASTER_NO: 176-62400004"}
    try:
        data = vlm_extract.extract_master(tr, model=mb)
        assert data["MAWB_NO"] == "176-62400004"
        assert calls == {"global": 0, "master": 1}, "主单必须发在它的渠道上，不能蹭分单那份客户端"
    finally:
        vlm_extract._client, vlm_extract._client_channel = old[0], old[1]
        vlm_extract._extra_clients.clear()
        vlm_extract._extra_clients.update(old[2])


def test_master_prompt_is_exposed_for_fingerprinting():
    """缓存指纹要盖住提示词：改了 prompt 必须让旧缓存失效，否则页面一直在喂过期结果。"""
    tr = {"lines": [{"i": 1, "text": "MASTER_NO: 176-62400004"}], "full_text": "MASTER_NO: 176-62400004"}
    p = vlm_extract.master_prompt(tr)
    assert "MAWB_NO" in p and "MASTER_NO: 176-62400004" in p
    assert p == vlm_extract.master_prompt(tr), "同一份资料要能算出同一个指纹"


def test_env_pin_keeps_the_regression_offline():
    assert "MASTER_VLM_MODEL" in os.environ and os.environ["MASTER_VLM_MODEL"] == "", \
        "回归入口必须把它钉空，否则测试会真打百炼"
