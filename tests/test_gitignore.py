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
# 后两条钉的是 2026-10-10 抓到的那个真坑：规则写成 input/（没前导斜杠）会连任意层级同名
# 目录一起吞掉，L3 基线夹具的 input/ 就是这么被吞了几个月——夹具没进过 Git、没进过 GitHub、
# 不在部署包里，全新解包的目录跑回归当场"夹具只剩 0 份"。根目录那个投放口照旧要挡（上面 MUST_BLOCK）。
MUST_NOT_BLOCK = ("config.py", "server.py", "web/js/desk.js", "deploy/sync.sh",
                  "requirements-server.lock.txt", ".env.example",
                  # 探的是**未跟踪**的同目录路径：git 的忽略规则对已入库的文件不再生效，
                  # 拿 1_dsv.json 去问只会永远得到"没被忽略"，规则改回 input/ 也测不出来。
                  "tests/fixtures/l3/input/zzz_probe.json",
                  "tests/fixtures/l3/expected/zzz_probe.json")


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
    """退化判定：只看第一段有没有作为一行规则出现（'.env' 或 'output/' 这类）。
    前导斜杠在这里丢掉是对的：这条判定本来就只比首段，锚不锚根对它的结论没有影响
    （`/output/` 与 `output/` 对 `output/air/any.json` 都是命中，对
    `tests/fixtures/l3/input/x.json` 都不命中——后者首段是 tests）。"""
    head = rel.split("/", 1)[0]
    pats = [p[1:] if p.startswith("/") else p for p in _patterns()]
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


def test_l3_fixtures_are_actually_in_version_control():
    """规则吞掉一个目录时，git 只是"不主动收"，本机跑回归照样绿（文件就在盘上）——
    缺文件这件事要等到全新解包（部署包、别人 clone、CI）才现形，而那时已经晚了。
    所以这里直接问索引：盘上几份，索引里就得几份。
    服务器那份代码是 tar 推过去的、没有 .git，这条只在开发机与发版前的本地回归上跑（那正是它该管的地方）。"""
    if not _in_repo():
        return
    for sub in ("input", "expected"):
        on_disk = sorted(p.name for p in (ROOT / "tests" / "fixtures" / "l3" / sub).glob("*.json"))
        raw = subprocess.run(["git", "ls-files", "-z", "tests/fixtures/l3/" + sub],
                             cwd=ROOT, capture_output=True).stdout.decode("utf-8")
        tracked = sorted(x.rsplit("/", 1)[-1] for x in raw.split("\0") if x)
        assert len(on_disk) >= 31, f"L3 夹具盘上只剩 {len(on_disk)} 份，基线被误删了"
        assert tracked == on_disk, (
                sub + "/ 里有夹具没进版本控制（部署包与 clone 都会缺）："
                + str(sorted(set(on_disk) - set(tracked))[:5])
                + "——十有八九是 .gitignore 某条规则没锚定，把任意层级的 input/ 一起吞了")


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
