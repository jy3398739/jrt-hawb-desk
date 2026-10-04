# -*- coding: utf-8 -*-
"""审核台登录与账号：口令哈希、users.json 存储、HMAC 签名会话 Cookie。

设计口径（2026-09-22 用户定案）：
  - 登录是审核台全站唯一的门槛：除 / 、/health、/models 外，所有接口都要有效会话（任意角色），
    切模型与管账号要管理员会话。原先的 X-API-Key 接口密钥已随内部部署定案删除。
  - 管理员 admin 可切换模型、管理子账号；制单员（子账号）除这两项外全部可用。
  - 录入员（第三种角色，2026-09-23）不上传、不碰提取管道：按主单号从公司系统取回主单+分单 JSON，
    打开本机已归档的分单原件对票面核对。取数口子公司接口契约（待 IT），当前只有本地 mock。
    管理员与录入员都能用 /company/mawb；制单员不能（角色守卫 require_inputter）。
  - 子账号口令由管理员随时下发/重置，首登不强制改。种子账号里 8 位制单员与 3 位录入员口令为空。
  - 口令只存 pbkdf2_sha256 哈希，绝不明文；users.json 里另存一枚随机 secret 用来签会话，
    会话不落在服务端内存，重启不掉线、也不依赖额外配置。
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path

import atomic
import config

# === 可调项（都有合理默认，一般不用配） ===
PBKDF2_ITERS = int(os.getenv("AUTH_PBKDF2_ITERS", "240000"))
SESSION_HOURS = float(os.getenv("AUTH_SESSION_HOURS", "12"))
COOKIE = "hawb_session"
# 会话 Cookie 的 Path。反代剥前缀时应用看到的都是 /，默认 / 最稳；要收窄按部署改。
COOKIE_PATH = os.getenv("AUTH_COOKIE_PATH", "/")
# users.json 落盘位置：与 .env 同目录，已在 .gitignore 内，绝不入库。
USERS_FILE = Path(os.getenv("AUTH_USERS_FILE", str(config.BASE_DIR / "users.json")))

# 制单员种子账号（用户名即姓名）。管理员可增删、可随时重置口令。
REVIEWERS = ["马殿齐", "宛平", "陈新", "叶庭伸", "赵文宇", "杨皓荃", "隗一航", "董文志"]
# 录入员种子账号（主分单审核台，2026-09-23）。同样口令为空，待管理员下发。
INPUTTERS = ["刘明", "郭健康", "郭旭"]
SEED_ADMIN_NAME = "admin"
SEED_ADMIN_PASSWORD = "admin123"          # 生产首登后应尽快在「账号管理」里改掉

# 账号名允许的字符：中文、字母数字与空格 . _ -（挡掉路径分隔、竖线、控制符与换行——
# 竖线是会话 payload 的分隔符，混进用户名就能伪造别的身份段）。
_NAME_OK = re.compile(r"^[^|\\/:\x00-\x1f]{1,40}$")

# 三种角色：管理员管账号/切模型，制单员上传核对提交，录入员按主单号检索并核对主↔分单。
ROLES = ("admin", "reviewer", "inputter")


def valid_username(name: str) -> bool:
    return bool(_NAME_OK.match(str(name or ""))) and str(name).strip() == str(name) and bool(str(name).strip())


# === 口令哈希：pbkdf2_hmac(sha256)，无第三方依赖 ===
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, PBKDF2_ITERS)
    return f"pbkdf2_sha256${PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False                     # 未设口令：永远登录不了，等管理员下发
    parts = str(stored).split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        iters, salt, want = int(parts[1]), bytes.fromhex(parts[2]), parts[3]
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, iters)
    return hmac.compare_digest(calc.hex(), want)


# === users.json 存储 ===
def seed_data() -> dict:
    users = {SEED_ADMIN_NAME: {"name": SEED_ADMIN_NAME, "role": "admin",
                               "pw": hash_password(SEED_ADMIN_PASSWORD)}}
    for n in REVIEWERS:
        users[n] = {"name": n, "role": "reviewer", "pw": None}
    for n in INPUTTERS:
        users[n] = {"name": n, "role": "inputter", "pw": None}
    return {"secret": secrets.token_hex(32), "users": users}


def _write(data: dict) -> None:
    """原子写盘并保住权限：里面既有口令哈希又有签名 secret，新建时收进 600，
    已存在则沿用原权限（os.replace 会把临时文件的权限一起带过去，见 atomic.py）。"""
    atomic.write_json(USERS_FILE, data, mode=0o600)


def load() -> dict:
    """读账号表；文件不存在即按种子建一份（首次启动自动初始化 admin + 8 位制单员）。"""
    if not USERS_FILE.is_file():
        _write(seed_data())
    data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
    data.setdefault("users", {})
    data.setdefault("secret", secrets.token_hex(32))
    return data


def save(data: dict) -> None:
    _write(data)


def ensure_seed() -> bool:
    """True=本次新建了账号表。服务启动时调一次，让 users.json 早于任何请求就位。"""
    if USERS_FILE.is_file():
        return False
    save(seed_data())
    return True


def backfill_seed() -> list:
    """把"后来才加进种子的默认账号"（如 2026-09-23 新增的录入员）补进已存在的 users.json。
    老部署的 users.json 早于新角色生成，ensure_seed 见文件已存在就跳过，新角色永远进不去——
    这里只补当前缺失的种子账号（口令留空待管理员下发），绝不改动任何已有账号的口令/角色，
    也没缺就原样不动。返回本次补进去的名字列表。"""
    if not USERS_FILE.is_file():
        return []                         # 还没建表，交给 ensure_seed（会带全种子）处理
    data = load()
    defaults = [(n, "reviewer") for n in REVIEWERS] + [(n, "inputter") for n in INPUTTERS]
    added = []
    for name, role in defaults:
        if name not in data["users"]:
            data["users"][name] = {"name": name, "role": role, "pw": None}
            added.append(name)
    if added:
        save(data)
    return added


def list_users() -> list:
    """给「账号管理」用：只报名字/角色/有没有口令，绝不回哈希。按 admin→reviewer→inputter 排。"""
    users = load()["users"]
    rows = [{"name": k, "role": v.get("role"), "has_password": bool(v.get("pw"))}
            for k, v in users.items()]
    order = {r: i for i, r in enumerate(ROLES)}
    return sorted(rows, key=lambda r: (order.get(r["role"], len(ROLES)), r["name"]))


def set_password(name: str, password: str = "", role: str = "", create: bool = True):
    """设/重置某账号口令；账号不存在时按需新建。返回 (user, 错误)。"""
    name = str(name or "").strip()
    if not valid_username(name):
        return None, "账号名不合法"
    data = load()
    u = data["users"].get(name)
    if u is None:
        if not create:
            return None, f"账号不存在：{name}"
        u = {"name": name, "role": role if role in ROLES else "reviewer", "pw": None}
        data["users"][name] = u
    if role in ROLES:
        u["role"] = role
    if password:
        u["pw"] = hash_password(password)
    save(data)
    return u, None


def delete_user(name: str, by: str = ""):
    """删账号：不能删自己，也不能把最后一个管理员删掉——否则没人再管账号。返回 (成功, 错误)。"""
    name = str(name or "").strip()
    data = load()
    if name not in data["users"]:
        return False, f"账号不存在：{name}"
    if name == by:
        return False, "不能删除当前登录的自己"
    admins = [k for k, v in data["users"].items() if v.get("role") == "admin"]
    if name in admins and len(admins) <= 1:
        return False, "不能删除唯一的管理员账号"
    del data["users"][name]
    save(data)
    return True, None


# === 会话：HMAC 签名的自包含 Cookie，不落服务端内存 ===
def _sign(secret: str, msg: str) -> str:
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def make_token(name: str, role: str, secret: str) -> str:
    exp = int(time.time() + SESSION_HOURS * 3600)
    payload = f"{name}|{role}|{exp}"
    blob = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    return f"{blob}.{_sign(secret, blob)}"


def parse_token(token: str, secret: str):
    if not token or "." not in token:
        return None
    blob, _, sig = token.rpartition(".")           # base64url 不含点，签名恒为末段
    if not hmac.compare_digest(_sign(secret, blob), sig):
        return None
    try:
        payload = base64.urlsafe_b64decode(blob.encode("ascii")).decode("utf-8")
    except Exception:
        return None
    parts = payload.split("|")
    if len(parts) != 3:
        return None
    name, role, exp = parts
    try:
        if int(exp) < time.time():
            return None
    except ValueError:
        return None
    return {"name": name, "role": role}


def authenticate(name: str, password: str):
    """校验口令，成功返回 {name, role, token}（token 交给调用方写 Cookie）。失败 None。"""
    data = load()
    u = data["users"].get(str(name or "").strip())
    if not u or not verify_password(password, u.get("pw")):
        return None
    return {"name": u["name"], "role": u.get("role"),
            "token": make_token(u["name"], u.get("role"), data["secret"])}


def admin_still_uses_seed_password() -> bool:
    """管理员口令还是种子默认值吗？"上线先改 admin123"这件事不能只写在文档里——
    写在文档里就等于没写（清单挂了两周没人动）。只回答是/否，不回口令本身，也不回哈希。"""
    u = load()["users"].get(SEED_ADMIN_NAME)
    return bool(u) and verify_password(SEED_ADMIN_PASSWORD, u.get("pw"))


def session_user(request) -> dict:
    """从请求 Cookie 还原当前登录用户；无效或账号已删/已改角色一律 None。
    无 Cookie 时不读盘（省掉 /health 这类免登录路径的 users.json 打开）。"""
    token = request.cookies.get(COOKIE) if request is not None else None
    if not token:
        return None
    data = load()
    info = parse_token(token, data["secret"])
    if not info:
        return None
    u = data["users"].get(info["name"])
    if not u or u.get("role") != info["role"]:      # 删除或降权即刻失效，不必等 Cookie 过期
        return None
    return {"name": info["name"], "role": info["role"]}
