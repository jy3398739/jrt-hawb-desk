"""双击启动的那几个 .bat：解释器要归口，编码不能被改坏。

两处都是踩过的坑：
1) 它们原先一律裸 `python`，走的是全局解释器；而本机全局 Python 被好几个服务共用，
   2026-09-24 与 2026-10-02 两次被别的包顶坏，一坏就是整台机器点不开。现在归口到
   各 bat 顶部解析出的 %PY%（有 .venv 用 .venv，没有才退回全局），和 deploy/release.sh 同源。
2) 这些文件是 GBK + CRLF。用按 UTF-8 读写的编辑器改它们，中文会变乱码或干脆写不回去，
   所以这里断言"仍能按 GBK 解码且中文没变成替换符"——改坏编码会当场红，而不是等用户
   双击时看到一屏问号。
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
BATS = sorted(ROOT.glob("*.bat"))

# 解析解释器：先指向 .venv，找不到才置为全局 python（写在 if 块里，顺带提醒用户去建环境）
PY_LINE = 'set "PY=.venv\\Scripts\\python.exe"'
PY_FALLBACK = 'set "PY=python"'


def _text(p: pathlib.Path) -> str:
    raw = p.read_bytes()
    assert b"\r\n" in raw, p.name + " 的行尾不是 CRLF（bat 在 cmd 里要靠 CRLF）"
    try:
        t = raw.decode("gbk")
    except UnicodeDecodeError as e:
        raise AssertionError(
            p.name + " 已不是合法 GBK（多半是被按 UTF-8 的编辑器改过，中文会乱码）：" + str(e))
    return t


def test_there_are_launcher_bats_to_guard():
    assert len(BATS) >= 7, "启动脚本少了：现在有 " + str(len(BATS)) + " 个 .bat"


def test_every_python_call_goes_through_the_resolved_interpreter():
    for p in BATS:
        t = _text(p)
        for i, ln in enumerate(t.splitlines(), 1):
            s = ln.strip()
            if s.startswith("echo") or s.startswith("rem") or not s:
                continue
            if s.startswith("python -m venv"):
                continue  # 唯一的例外：建 .venv 这一步本身只能拿全局 Python 来跑
            assert not re.match(r"^python(\s|$)", s), \
                p.name + " 第 " + str(i) + " 行还在裸用 python（会撞全局解释器）：" + s


def test_the_fallback_warns_instead_of_silently_using_global():
    """退回全局是应急，不是常态——不吭声的话，下次全局又被别的服务顶坏时，
    用户看到的是"审核台点不开"，而不是"你还没建 .venv"。1_安装依赖.bat 自己不指路
    （它就是来建环境的那个）。"""
    for p in BATS:
        t = _text(p)
        if PY_LINE not in t or p.name.startswith("1_"):
            continue
        assert PY_FALLBACK in t, p.name + " 有 .venv 缺失的分支却没把 PY 置回全局"
        assert "1_安装依赖.bat" in t, p.name + " 的兜底没说清去哪建 .venv"



def test_bats_that_call_python_resolve_py_first():
    for p in BATS:
        t = _text(p)
        if "%PY%" not in t:
            continue
        assert PY_LINE in t and PY_FALLBACK in t, \
            p.name + " 用了 %PY% 却没在顶部解析它（缺 .venv 的机器会直接报找不到命令）"


def test_dep_install_builds_the_local_venv_from_the_server_lock():
    """装依赖这一步是环境的唯一入口：建 .venv 并按锁文件装，才能和服务器同一套版本。"""
    t = _text(ROOT / "1_安装依赖.bat")
    assert "-m venv .venv" in t, "1_安装依赖.bat 不再建 .venv，装了等于装进全局"
    assert "requirements-server.lock.txt" in t, \
        "1_安装依赖.bat 没按服务器锁装，本机与线上的版本会漂"
    assert (ROOT / "requirements-server.lock.txt").is_file()
