# -*- coding: utf-8 -*-
"""部署面回归（设计文档 §5）：服务器锁文件、Ubuntu 部署说明、unit/nginx 片段三者都不能说谎。

为什么拿测试盯文档：这套东西跑在共享的生产机上，接手的第二个人只会照 README 与 unit 里的
注释做。写"配合 SSH 隧道访问、不暴露到网络"这种已经过时的话，比不写更危险。
"""
import ast
import configparser
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


def test_unit_has_a_start_limit_that_can_actually_trip():
    """`Restart=always` + `RestartSec=3` 而没有启动熔断：配置写错（工作目录挪了、venv 坏了、
    端口被占）时 systemd 每 3 秒拉一次、永远拉不起来，journal 被同一条错误刷满，真故障淹没在里
    头（2026-10-16 效率审查 D1）。这台 VM 是 systemd 255，默认窗口只有 10 秒/5 次，
    3 秒一轮恰好次次落在窗口外＝等于没设——所以熔断窗口必须明显大于重启间隔。

    另外这两个选项自 systemd 229 归 [Unit] 段，写进 [Service] 会被忽略并告警。"""
    u = UNIT.read_text(encoding="utf-8")
    cp = configparser.ConfigParser(strict=False)
    cp.optionxform = str            # systemd 的选项名区分大小写，别让它转成小写
    cp.read_string(u)
    unit_opts = dict(cp["Unit"]) if cp.has_section("Unit") else {}
    assert {"StartLimitIntervalSec", "StartLimitBurst"} <= set(unit_opts), \
        "启动熔断没在 [Unit] 段里（写在 [Service] 会被 systemd 忽略并告警，等于没设）"
    burst = int(unit_opts["StartLimitBurst"])
    window = int(re.match(r"(\d+)", unit_opts["StartLimitIntervalSec"]).group(1))
    interval = int(re.match(r"(\d+)", cp["Service"]["RestartSec"]).group(1))
    assert 1 <= burst <= 10, f"熔断次数 {burst}：1 是一崩就停，太多等于不拦"
    assert window >= interval * (burst + 1), \
        f"窗口 {window}s 装不下 {burst} 次 {interval}s 一轮的重启，崩环照样跑满"


def test_sync_reminds_about_the_unit_file_without_touching_systemd():
    """systemd 只认 /etc/systemd/system/ 里那一份，而 sync.sh 推的是程序目录 ⇒ 改了
    `hawb-desk.service`（比如 v1.29.0 那条启动熔断）不装上去就永远不生效，可 `/health` 看着一切正常
    ——这是"改了等于没改"那一类，最坑。但脚本自己不装：这台是共用机（ccsp / license /
    jrt-api / dsv-mail 都在上面），/etc 是总电闸房，脚本判断错的后果不是"我们网站慢一点"，
    而是服务起不来。所以这条**只许比对 + 打印提示**，装的那两条命令交给人手工执行。

    它跑在成功路径的最末尾：失败/干跑那几条分支早就 exit 了，别在没推东西的时候吓人。"""
    s = (ROOT / "deploy" / "sync.sh").read_text(encoding="utf-8")
    assert "/etc/systemd/system/" in s, "没跟服务器上那份 unit 比过，改了也不会提醒你装"
    assert re.search(r"md5sum [^\n]*(\$UNIT|hawb-desk\.service)", s), \
        "比对要读两边哈希，光 echo 文件名比不出差异"
    assert re.search(r"live=\"\$\(ssh [^\n]*md5sum /etc/systemd/system/\$UNIT\.service", s), \
        "「装没装过」必须从服务器上读回来：把它写死成空值（或读错路径）就永远报同一条提示，" \
        "而永远对的提示等于没有提示——第一轮变异 U3 就是这么活的"
    cp_lines = [l for l in s.splitlines() if "sudo cp" in l]
    assert cp_lines, "提示里得把要手工执行的那条 cp 命令原样写出来，否则等于没提示"
    assert all(("say " in l) or l.strip().startswith("#") for l in cp_lines), \
        "sync.sh 不许自己把 unit 装进 /etc：装那一步必须留在人手里（脚本本来就有 sudo systemctl " \
        "restart——重启我们自己的服务是既有行为，危险的只有覆盖别人的电闸房）"
    assert "unit_drift_hint" in s[s.index("sudo systemctl restart $UNIT &&"):], \
        "提示要挂在重启之后的成功路径上（函数只定义不调用等于没有）：没推上去就报「unit 不一样」是噪音"


def test_sync_prunes_its_own_deploy_backups():
    """每次发版在 ~ 打一份 `.bak-<时刻>-deploy.tar`，从前只增不减：2026-10-16 数过这台共享机上
    已攒 80 份、114MB（效率审查 D2）。回滚只看最近几版，留 80 版没用，迟早把盘写满。

    两条都要钉住：① 真的有裁剪；② 只按自己的命名删——这台机器上还有别的备份
    （run_batch/recheck 前手动打的 ~/backups/*.tar），通配写宽了就是删人家的东西。"""
    s = (ROOT / "deploy" / "sync.sh").read_text(encoding="utf-8")
    kept = re.search(r"KEEP=(\d+)", s)
    assert kept, "sync.sh 里没有 KEEP：备份根本没有裁剪这一步"
    assert 1 <= int(kept.group(1)) <= 10, f"备份留 {kept.group(1)} 份：这不叫清理"
    prune = re.search(r"ls -t \.bak-\*-deploy\.tar[^\n]*", s)
    assert prune, f"没找到「按时间留最近几份」那一步：{s[-400:]}"
    assert "tail -n +$((KEEP+1))" in prune.group(0), "留几份必须由 KEEP 这一个数说了算"
    assert "xargs -r rm -f" in prune.group(0), "空列表时不能报错（xargs -r），也别用宽通配"
    assert s.index(".bak-$STAMP-deploy.tar") < s.index(prune.group(0)), \
        "必须先打上本次备份再裁剪：顺序倒了会把上一份当最旧的删掉"
