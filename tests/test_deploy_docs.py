# -*- coding: utf-8 -*-
"""部署面回归（设计文档 §5）：服务器锁文件、Ubuntu 部署说明、unit/nginx 片段三者都不能说谎。

为什么拿测试盯文档：这套东西跑在共享的生产机上，接手的第二个人只会照 README 与 unit 里的
注释做。写"配合 SSH 隧道访问、不暴露到网络"这种已经过时的话，比不写更危险。
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "requirements-server.lock.txt"
REQ = ROOT / "requirements.txt"
README = ROOT / "deploy" / "README-ubuntu.md"
UNIT = ROOT / "deploy" / "hawb-desk.service"
NGINX = ROOT / "deploy" / "nginx-hawb-location.conf"

# 顶层第三方模块名 → 发行包名（不一致的那些才需要映射）
ALIASES = {"PIL": "pillow", "fitz": "pymupdf", "dotenv": "python-dotenv",
           "multipart": "python-multipart", "yaml": "pyyaml"}
STDLIB = set(sys.stdlib_module_names)     # 手抄标准库清单必漏（第一版就漏了 hmac/urllib/concurrent）


def _pkg(name: str) -> str:
    return ALIASES.get(name, name).lower().replace("_", "-")


def _third_party_imports() -> set:
    local = {p.stem for p in ROOT.glob("*.py")}
    out = set()
    for p in sorted(ROOT.glob("*.py")):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module.split(".")[0]] if node.module and node.level == 0 else []
            else:
                continue
            out |= {n for n in names if n not in local and n not in STDLIB}
    return out


def test_server_lock_pins_everything_the_code_imports():
    """锁文件漏一个包 = 换机时装不全，服务起不来，而报错信息一点不像"少装依赖"。"""
    assert LOCK.is_file(), "缺 requirements-server.lock.txt"
    lock = LOCK.read_text(encoding="utf-8")
    pinned = {_pkg(m.group(1)) for m in re.finditer(r"^([A-Za-z0-9_.-]+)==", lock, re.M)}
    assert pinned, "锁文件里一个版本都没钉，那不叫锁"
    missing = sorted(p for p in (_pkg(m) for m in _third_party_imports()) if p not in pinned)
    assert not missing, f"代码 import 了但锁文件没有的包：{missing}（重生成：见锁文件头部的 pip freeze 命令）"
    for line in lock.splitlines():
        if line.startswith("#"):
            continue
        assert "==" in line, f"锁文件必须是精确版本，这行不是：{line}"


def test_lock_and_requirements_disagree_on_purpose_says_so():
    """两份文件并存必须解释清楚为什么，否则下一个人会以为其中一份是忘更新的。"""
    lock = LOCK.read_text(encoding="utf-8")
    assert "pip freeze" in lock and ("venv" in lock or "VENV" in lock), "锁文件要写清来源与重生成方法"
    req = REQ.read_text(encoding="utf-8")
    assert "requirements-server.lock.txt" in req, "requirements.txt 要指到锁文件，说清服务器该装哪份"
    for dead in ("rapidocr", "opencv", "onnxruntime", "xlrd", "pdfplumber"):
        assert not re.search(r"^\s*%s" % dead, req, re.M), f"{dead} 随退役规则层一起摘掉了，别又回到依赖表里"


def test_readme_covers_the_steps_someone_else_would_need():
    assert README.is_file(), "缺 deploy/README-ubuntu.md"
    t = README.read_text(encoding="utf-8")
    need = ["libreoffice", "fonts-noto-cjk", "venv", "requirements-server.lock.txt",
            ".env", "INTERNLM_API_KEY", "users.json", "admin123", "systemctl",
            "127.0.0.1:8020", "nginx", "run_tests.py", "sync.sh", "--no-restart",
            "journalctl", "stale_files", "SEND_STATUS", "MemoryMax", "git ls-files",
            "今日台账", "整表写回"]
    miss = [k for k in need if k not in t]
    assert not miss, f"部署说明少了这些必答项：{miss}"
    assert "chmod 600 .env" in t, ".env 里有令牌：新建就要收权限，别等人问"


def test_unit_and_nginx_comments_match_reality():
    u = UNIT.read_text(encoding="utf-8")
    assert "HTTP_API_KEY" not in u, "接口密钥早在 2026-09-22 就删了，注释还留着会误导接手的人"
    assert "SSH 隧道" not in u and "不暴露到网络" not in u, \
        "现在公网经 nginx 可达（https://<ip>/hawb/），注释说不暴露等于给下一个人错误的安全感"
    assert "127.0.0.1" in u and "MemoryHigh" in u and "MemoryMax" in u, "回环绑定与内存闸是这台机的前提"
    n = NGINX.read_text(encoding="utf-8")
    assert re.search(r"proxy_pass\s+http://127\.0\.0\.1:8020/;", n), "结尾斜杠剥前缀，少了它整站 404"
    assert "proxy_read_timeout" in n, "解析是长调用，默认 60s 会掐断上传"
