# -*- coding: utf-8 -*-
"""定向向 VLM 提问核对票面疑点（第二意见）。
用法: python ask_vision.py <图片或PDF> "你的问题，要求逐字符读出"
例:   python ask_vision.py 1-DSV.bmp "票面主单号3位前缀+8位数字逐字符读出"
"""
import sys
from pathlib import Path
from openai import OpenAI

import config
from vlm_extract import img_to_data_urls


def ask(path, question: str, max_tokens=2000) -> str:
    if not config.MODEL_VISION:
        raise RuntimeError(
            f"当前模型不支持图片输入（{config.VLM_MODEL}），看不了票面："
            "本工具要靠视觉核对疑点，请先切到视觉模型（intern-s2-official）")
    client = OpenAI(api_key=config.require_vlm_api_key(), base_url=config.vlm_base_url(),
                    timeout=config.VLM_TIMEOUT, max_retries=0)
    content = [{"type": "image_url", "image_url": {"url": u}} for u in img_to_data_urls(Path(path))]
    content.append({"type": "text", "text": question})
    resp = client.chat.completions.create(
        model=config.VLM_MODEL,
        messages=[{"role": "user", "content": content}],
        max_tokens=max(max_tokens, config.model_max_tokens()), temperature=0.0,
    )
    return resp.choices[0].message.content


if __name__ == "__main__":
    config.fix_console()
    if len(sys.argv) < 3:
        raise SystemExit('用法: python ask_vision.py <图片> "问题"')
    print(ask(sys.argv[1], sys.argv[2]))
