"""外发前第一道闸：证明 .gitignore 真的挡住了密钥与本地产物。

为什么单独测：sync.sh 的部署清单和 GitHub 的提交清单都取自 git，挡不住就是直接把模型密钥 /
口令哈希推出去；而误挡一个源码文件则是"部署成功但线上缺文件"。两种失败都不会在写代码的人
眼前现形，所以每次回归都要问一遍。

两条判定路径：有 .git 时问 git 自己（Negation、**、目录尾斜杠这些语义别靠人想）；
服务器那份代码是 tar 推过去的、没有 .git，那时只能退回去查规则文本——所以这条退化路径
在本机也直接测一遍，别等它当了真闸门才第一次跑。
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 绝不该进部署包/仓库的东西：密钥、口令库、每次跑批重建的输出，和本机/服务器各自的 venv
# 最后两条是"改配置前顺手 cp 一份"的备份：里面全是真密钥，而这类文件天生比 .env 更容易被
# git add -A 一把带走（2026-10-08 就差点发生一次）。
MUST_BLOCK = (".env", "users.json", "output/air/any.json", "input/any.pdf",
              ".venv/Scripts/python.exe", "__pycache__/config.pyc",
              ".env.bak-20261008-131937", "users.json.bak-deadkeys",
              # 暂存档：一张票一份，里面是整张票面的人工终值与人名（和 submitted.json 同级敏感）
              "output/staged/any.json")
# 必须能被 sync.sh 的 git ls-files 取到，挡了就等于"部署成功但线上缺文件"
# .env.example 是模板（要入库），规则写成 .env.* 时必须靠 ! 把它放回来——它也在这一条里核。
MUST_NOT_BLOCK = ("config.py", "server.py", "web/js/desk.js", "deploy/sync.sh",
                  "requirements-server.lock.txt", ".env.example")


def _git_text():
    return (ROOT / ".gitignore").read_text(encoding="utf-8")


def _patterns():
    return [ln.strip() for ln in _git_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _ignored_by_git(rel: str) -> bool:
    """问 git：退出码 0 = 命中忽略规则，1 = 没命中。128 = 不在仓库里（调用方负责不走这条）。"""
    return subprocess.run(["git", "check-ignore", "-q", rel], cwd=ROOT,
                          capture_output=True).returncode == 0


def _in_repo() -> bool:
    return subprocess.run(["git", "rev-parse", "--git-dir"], cwd=ROOT,
                          capture_output=True).returncode == 0


def _mentioned_in_gitignore(rel: str) -> bool:
    """退化判定：只看第一段有没有作为一行规则出现（'.env' 或 'output/' 这类）。"""
    head = rel.split("/", 1)[0]
    pats = _patterns()
    if head in pats or (head + "/") in pats:
        return True
    # 密钥文件的备份/临时副本（.env.bak-xxx、users.json.bak）：规则写的是 .env.* 这种通配，
    # 这里只认这一个用得到的小形状——.env.example 是模板，跟 git 一样把它放回来。
    # 其余通配语义一律交给 git 判（有 .git 时走的就是那条路）。
    for fam in (".env.", "users.json."):
        if head.startswith(fam) and head != ".env.example":
            return fam + "*" in pats
    return False


def _blocked(rel: str) -> bool:
    return _ignored_by_git(rel) if _in_repo() else _mentioned_in_gitignore(rel)


def test_secrets_and_local_env_are_blocked():
    for rel in MUST_BLOCK:
        assert _blocked(rel), rel + " 没被 .gitignore 挡住，会随部署包和仓库外发"


def test_source_files_are_not_blocked():
    for rel in MUST_NOT_BLOCK:
        assert not _blocked(rel), rel + " 被误挡：部署清单取自 git ls-files，挡住就等于不部署"


def test_fallback_rule_text_matcher_agrees_with_git():
    """服务器上没有 .git，走的就是 _mentioned_in_gitignore —— 本机把两条路都跑一遍，
    不一致就是那条退化闸门在骗人。"""
    if not _in_repo():
        return
    for rel in MUST_BLOCK + MUST_NOT_BLOCK:
        assert _mentioned_in_gitignore(rel) == _ignored_by_git(rel), \
            "规则文本判定与 git 判定对 " + rel + " 结论不一致"


def test_it_api_doc_is_still_blocked_by_name():
    """IT 那份说明含两把真 key，文件名是中文——git check-ignore 在 Windows 上对非 ASCII 参数
    不可靠，所以这一条只查规则文本（'AMS录入接口调用说明 (1).md' 这类后缀变体一并挡掉）。"""
    assert "AMS录入接口调用说明*.md" in _git_text(), "挡 IT 接口说明的那行没了——它含两把真 key"
