# -*- coding: utf-8 -*-
"""HTTP 接口服务（形态③）。业务系统上传分单文件，实时返回两遍 全字段 JSON。
启动:
  python server.py                 # 默认只监听 127.0.0.1:8000
  python server.py --host 0.0.0.0 --port 9000   # 局域网开放：所有接口都要登录，先改掉默认口令
门禁: 全站唯一的门槛是登录（users.json 里的账号）。/ 与 /health 与 /models 免登录，
      其余数据接口要任意角色会话，切模型/管账号要管理员会话。
接口:
  GET  /health             健康检查（免登录，供存活探测）
  GET  /                   制单员审核台页面（登录→上传→看票面原件→核对航空口径字段→提交）
  GET  /models             模型清单（免登录，审核台顶部下拉用）
  POST /login              登录：body={"name","password"}；成功写会话 Cookie（HttpOnly）
  POST /logout             退出：清掉会话 Cookie
  GET  /api/me             当前登录者（有会话→{authenticated,user:{name,role}}，否则 authenticated=false）
  GET  /admin/users        账号清单（管理员会话）；不回口令
  POST /admin/users        设/重置口令或新建账号（管理员）；body={"name","password"?,"role"?}
  DELETE /admin/users/{name}  删除账号（管理员；不能删自己或唯一管理员）
  POST /model              切换模型并写回 .env（管理员会话）；body={"model":"intern-s2-official"}
  POST /model/thinking     切换思考档并写回 .env（管理员会话）；body={"thinking":""|"0"|"1"}
  POST /extract            需登录会话；multipart 字段 file=分单文件；一律按原名落盘（L0 归档+四层结果）
  POST /submit             需登录会话；body={"tickets":[...] }；制单员提交=回传公司(现 mock)+进索引的唯一出口，缺主/分单号拒绝
  POST /stage              需登录会话；body={"tickets":[...] }；核对结果存服务器 + 公司占位(hawb2，状态 0)，缺号只存本机
  GET  /company/mawb       需登录会话(任意角色)；?mawb=主单号 → 主单+名下分单(带本机原件 stem)+pending(本机暂存、公司还不认识的票)，并自动起主单解析；?force=1 重解析
  GET  /master/{mawb}      需登录会话(任意角色)；主单解析记录（状态/36 列/红旗/L1/暂存的人工值与事件）
  POST /master/stage       需登录会话；body={"mawb","ams","acked_flags"}；36 列核对结果存服务器 + 公司占位(mawb2，状态 0)
  POST /master/submit      需登录会话；主单**提交发送**公司(mawb3)的唯一出口；没录入员复核过照样发、台账留痕
  GET  /results            已落盘条数（需登录会话）
  GET  /tickets            需登录会话；本机经手过的票与状态 parsed/staged/submitted/failed，可带 ?date=&state=&mawb=
  GET  /day                需登录会话；今日台账（分单+主单合一张表，按天），可带 ?date=
  POST /void               需登录会话；台账行作废/恢复 body={"kind":"hawb|mawb","key":..,"void":true|false}
                           只写 output/voided.json 一个戳，提交台账、暂存、归档原件与索引一律不动
  GET  /staged/{stem}      需登录会话；一份暂存（模型原样 + 人工最新值 + 每次谁改了哪几列）
  GET  /stats              仅管理员；?from=&to=&group_by=model|role|user|field → 解析正确率（模型那版 vs 人定下的那版）
  GET  /stats/export       仅管理员；同一份数导成 Excel（口径 / 每张票 / 逐列 三张表）
  GET  /source/{stem}      回看票面原件，供审核台预览（需登录会话）；?raw=true 发原件本身供下载
  GET  /mawb/source/{mawb} 回看主单原件（需登录会话，任意角色）；原件在 output/mawb_source/<归一化主单号>/
  GET  /layout/{stem}      L1 逐字转录（含每行 bbox），审核台「点字段定位票面行」用（需登录会话）
  GET  /render/{stem}      把归档票面按页转成 PNG（?page=&scale=），定位模式的底图（需登录会话）
"""
import argparse, hashlib, json, logging, re, shutil, tempfile, threading, time
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.middleware.gzip import GZipMiddleware

import auth
import company_api
import config
import desk_queue
import fieldspec
import jobs
import master_pipeline
import metrics
import purge
import stats
import store
import xlsx2pdf
from jobs import handle_file

app = FastAPI(title="HAWB 分单识别服务", version=config.APP_VERSION)
# 静态壳是 no-store（有意：留半份旧脚本出现过"点了没反应"），所以每台机器每次刷新都要重下
# 一整份 desk.js（已 230KB）。压缩层只管文本：PNG/JPG 这些二进制 Starlette 默认跳过，
# /render 的 ETag/304 走的是空体那条分支，也不受影响。
app.add_middleware(GZipMiddleware, minimum_size=1024)
# 回传失败的原因只在这次 HTTP 响应里，页面关掉就查无实据（2026-09-29 用户连吃两个 502 无从下手），
# 所以带号落到服务日志里，journalctl -u hawb-desk 能直接对着票查。
LOG = logging.getLogger("hawb.desk")
WEB_DIR = Path(__file__).resolve().parent / "web"
CHUNK = 1 << 20
# 进程启动时刻：用来发现「源码改了但服务没重启」。审核台曾因此一直拿不到票面原件
_BOOT = time.time()
_PKG_DIR = Path(__file__).resolve().parent


def _source_mtime() -> float:
    """源码（含审核台页面）里最新那份的修改时刻。和 `_BOOT` 一比就知道推上去的代码有没有真的重新加载
    ——部署是手工 tar 推文件，漏重启就会新旧混跑（stale_files 说得出哪些文件新，说不出这是哪一版）。"""
    files = list(_PKG_DIR.glob("*.py"))
    page = WEB_DIR / "index.html"
    if page.exists():
        files.append(page)
    stamps = [f.stat().st_mtime for f in files]
    return max(stamps) if stamps else 0.0


_BUILT_AT = _source_mtime()


def _stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)) if ts else ""
_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# 浏览器能内联渲染的票面格式。TIFF 有意不在列（Chrome/Firefox 都不认），走「下载原件」
_PREVIEW_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".bmp": "image/bmp", ".pdf": "application/pdf"}
_EXCEL_TYPES = {".xlsx", ".xlsm", ".xls"}


def require_web(request: Request) -> None:
    """审核台数据接口：只认登录会话（任意角色）。无有效会话 → 401，请先登录。"""
    if not auth.session_user(request):
        raise HTTPException(401, "请先登录")


def require_admin(request: Request) -> None:
    """管理员专属（切模型 / 管账号）：只认管理员会话。
    已登录但非管理员 → 403（权限不足）；无会话 → 401。"""
    user = auth.session_user(request)
    if user and user.get("role") == "admin":
        return
    if user:
        raise HTTPException(403, "此操作需要管理员权限")
    raise HTTPException(401, "请先以管理员登录")

def current_user(request: Request) -> dict:
    """需要"是谁"的接口用这个。提交留痕里的复核人只认登录会话——前端传什么都不认，
    否则台账可以随便署名（2026-09-29 审计）。"""
    u = auth.session_user(request)
    if not u:
        raise HTTPException(401, "请先登录")
    return u



class LoginBody(BaseModel):
    name: str = ""
    password: str = ""


class UserBody(BaseModel):
    name: str = ""
    password: str = ""
    role: str = ""


@app.post("/login")
def login(body: LoginBody, request: Request, response: Response):
    """登录审核台：校验口令 → 写签名会话 Cookie。口令只在本机 users.json 里存哈希。

    公网可达的门口要留得下"谁在试"（含扫描器）；口令本身绝不进日志。"""
    name = str(body.name or "").strip()
    ip = request.client.host if request.client else "?"
    if not name or not body.password:
        raise HTTPException(400, "请输入账号和密码")
    result = auth.authenticate(name, body.password)
    if not result:
        LOG.warning("登录失败 name=%s ip=%s", name, ip)
        raise HTTPException(401, "账号或密码不正确（子账号需先由管理员设置密码）")
    LOG.info("登录成功 name=%s role=%s ip=%s", result["name"], result["role"], ip)
    response.set_cookie(auth.COOKIE, result["token"], max_age=int(auth.SESSION_HOURS * 3600),
                        httponly=True, samesite="lax", path=auth.COOKIE_PATH)
    return {"ok": True, "user": {"name": result["name"], "role": result["role"]}}


@app.post("/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE, path=auth.COOKIE_PATH)
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    """前端据此决定显登录页还是进审核台，并按角色隐藏模型下拉/账号管理。
    默认口令没改掉这件事只告诉管理员：免登录的 /health 上说等于送人一个答案，
    告诉制单员则是推一个改不了的人。"""
    user = auth.session_user(request)
    out = {"authenticated": bool(user), "user": user}
    if user and user.get("role") == "admin":
        out["admin_pw_default"] = auth.admin_still_uses_seed_password()
    return out


@app.get("/admin/users")
def admin_users(_: None = Depends(require_admin)):
    return {"users": auth.list_users()}


@app.post("/admin/users")
def admin_set_user(body: UserBody, _: None = Depends(require_admin)):
    """管理员下发/重置子账号口令，或新建账号。password 留空=只改角色/新建不带口令。"""
    name = str(body.name or "").strip()
    if not auth.valid_username(name):
        raise HTTPException(400, "账号名不合法（不能含 | / \\ : 或控制字符）")
    if body.password and len(body.password) < 6:
        raise HTTPException(400, "口令至少 6 位")
    user, err = auth.set_password(name, password=body.password, role=str(body.role or "").strip())
    if err:
        raise HTTPException(400, err)
    return {"ok": True, "user": {"name": name, "role": user["role"], "has_password": bool(user["pw"])}}


@app.delete("/admin/users/{name}")
def admin_delete_user(name: str, request: Request, _: None = Depends(require_admin)):
    actor = auth.session_user(request)
    ok, err = auth.delete_user(name, by=(actor or {}).get("name", ""))
    if not ok:
        raise HTTPException(400, err)
    return {"ok": True}


def safe_name(filename: str, suffix: str) -> str:
    """只留 basename 并清掉路径分隔/保留字符：上传方给 '..\\..\\windows\\x.pdf' 也不能逃出临时目录。
    反斜杠先归一成 / 再取 basename——Linux 上 '\\' 不是分隔符，不归一会整串留下（曾靠 _ 兜底但名字已脏）。
    分隔符统一换 _，但 NBSP 这类 Windows 合法字符保留——落盘名要跟票面原件名一致，好对账。"""
    raw = str(filename or "").replace("\x00", "").replace("\\", "/")
    base = Path(raw).name
    out = _BAD_NAME.sub("_", base).strip(" .")[:120] or "upload"
    if suffix and not out.lower().endswith(suffix.lower()):
        out += suffix                    # 少数上传方不带扩展名，补上才能进管道
    return out


async def _save_upload(upload: UploadFile, dst: Path) -> None:
    """分块写盘并计字节：整体 read() 会把任意大的文件一次性吃进内存。"""
    limit = config.UPLOAD_MAX_MB * CHUNK
    written = 0
    with dst.open("wb") as f:
        while chunk := await upload.read(CHUNK):
            written += len(chunk)
            if written > limit:
                break            # 先出 with 关掉句柄，否则 Windows 上删不掉
            f.write(chunk)
    if written > limit:
        dst.unlink(missing_ok=True)
        raise HTTPException(413, f"文件超过 {config.UPLOAD_MAX_MB}MB 上限")


@app.get("/")
def desk():
    """制单员审核台（上传→并排看票面→核对航空口径→提交）。页面是静态壳，取数据仍要登录会话。"""
    page = WEB_DIR / "index.html"
    if not page.is_file():
        raise HTTPException(404, f"审核台页面缺失: {page}")
    return FileResponse(page, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})   # 别拿旧壳：页面改了刷新即生效


_WEB_DIRS = ("css", "js")
_WEB_TYPES = {".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8"}


@app.get("/web/{asset:path}")
def web_asset(asset: str):
    """审核台的样式与脚本：免登录（登录遮罩本身要样式才好看），但只发 css/ js/ 两个目录里的
    白名单类型，路径必须是一段目录 + 一个文件名，不给 `..` 留缝。

    和页面一样 no-store：这台改完重启就该立刻是新样子，缓存里留半份旧脚本就会出现
    "按钮点了没反应"那种查半天的事（2026-09-28 真撞到过一次）。"""
    parts = [p for p in asset.split("/") if p]
    if len(parts) != 2 or parts[0] not in _WEB_DIRS or parts[1] != Path(parts[1]).name:
        raise HTTPException(400, "前端资源路径不合法")
    media = _WEB_TYPES.get(Path(parts[1]).suffix.lower())
    if media is None:
        raise HTTPException(400, "前端资源只发 .css / .js")
    file = WEB_DIR / parts[0] / parts[1]
    if not file.is_file():
        raise HTTPException(404, "没有这个前端资源")
    return FileResponse(file, media_type=media, headers={"Cache-Control": "no-store"})

@app.get("/inputter")
def inputter_desk():
    """录入员检索台已并入制单台（2026-09-26 用户定案：主单检索与主单原件都在 / 一页）。
    老书签与外链一律 302 回主页，避免留下会各自漂移的第二份检索界面。"""
    return RedirectResponse(url="./", status_code=302)


def _stale_files() -> list:
    """比进程还新的源码文件。uvicorn 不带 --reload 时改动只落在磁盘上，跑的还是启动时那份——
    审核台看到 /health 里的这个字段就该提示重启，否则新功能（比如票面浏览）看着像坏了，其实只是进程旧。"""
    try:
        return sorted(p.name for p in _PKG_DIR.glob("*.py") if p.stat().st_mtime > _BOOT)
    except OSError:
        return []


@app.get("/health")
def health():
    mb = config.master_model_bundle()
    return {"ok": True, "version": config.APP_VERSION, "form": config.FORM,
            "model": config.VLM_MODEL, "model_choice": config.VLM_MODEL_CHOICE,
            "vision": config.MODEL_VISION, "key_configured": config.vlm_api_key_configured(),
            # 思考档如实报出来：它和模型名一样决定这一票怎么解析，健康检查里看不见就等于说谎
            "thinking": config.thinking(),
            # 主单链默认走自己的渠道（MASTER_VLM_MODEL）：两条链各报各的，免得看着像配错了
            "master_model": mb["model"], "master_model_choice": mb["choice"],
            "master_thinking": mb["thinking"],
            "master_key_configured": config.master_api_key_configured(),
            # 跑的是哪一版：commit 在服务器上是空串（只收 tar 推的文件、没有仓库），
            # 那就看 built_at（源码最新 mtime）与 started_at（进程启动）——前者晚于后者就是忘了重启。
            "commit": config.git_commit(), "built_at": _stamp(_BUILT_AT), "started_at": _stamp(_BOOT),
            # 队列实况：排队/在解析几张、几个槽位——"卡住了吗"第一眼就能判断
            "queue": DESK_QUEUE.stats(),
            "stale_files": _stale_files()}


# 模型 id 允许的字符：字母数字与 . _ - / :（org/Model、版本号）。挡住换行/等号/引号——
# 选择会写进 .env，值是拼进去的，混进换行就能伪造出别的配置行。
_MODEL_ID_OK = re.compile(r"^[A-Za-z0-9._:/-]{1,120}$")


class ModelChoice(BaseModel):
    model: str = ""
    chain: str = "hawb"      # hawb = 分单链（默认）；master = 主单链


class ThinkingChoice(BaseModel):
    # None 而不是 ""："" 是合法档位（跟随预设），漏发字段不能被读成"帮你改回跟随预设"
    thinking: str | None = None


def _company_submit(payload: dict) -> dict:
    """把一张已核对的分单 JSON 回传公司系统，回执写进提交台账。

    真实现收敛在 company_api.submit_order：契约待 IT，现在一律 mock、不外发任何请求，
    只回带 mode='mock' 的回执，把"提交态 + 索引"这条本地链路先跑通。真接口到位后改
    company_api 一处即可，这里的调用与上面 /submit 的门禁都不动。"""
    return company_api.submit_order(payload)


@app.get("/company/mawb")
def company_mawb(background: BackgroundTasks,
                 mawb: str = Query(..., description="主单号（可带连字符/空格，检索前归一化）"),
                 force: int = Query(0, description="1=越过缓存重跑解析（换了模型或手动重试）"),
                 _: None = Depends(require_web)):
    """按主单号检索：返回该主单原生资料 + 名下分单（带上本机原件 stem 供 /source 对票面核对），
    并**就地起主单解析**（用户定案：检索到就解析；同一主单并发只有一个真跑）。

    响应里的 `master` 是解析记录：命中缓存就是 done（不再花钱），刚起任务是 parsing，
    前端拿 /master/{mawb} 轮询到 done/failed。2026-09-26 起对任意登录角色开放——录入员台已并入。"""
    if not store.norm_no(mawb):
        raise HTTPException(400, "主单号不能为空")
    res = company_api.search_mawb(mawb)
    order = res.get("mawb_order") or {}
    if order:
        fresh = master_pipeline.fresh_record(mawb, order, bool(force))
        res["master"] = master_pipeline.public(fresh) if fresh else {"state": "parsing", "mawb": mawb}
        background.add_task(master_pipeline.ensure, mawb, order, bool(force))
    else:
        # 没资料就没得解析：就地记下 failed，让页面能说清"为什么没有结果"而不是空着
        res["master"] = master_pipeline.public(master_pipeline.ensure(mawb, order))
    return res


@app.get("/master/{mawb}")
def master_result(mawb: str, _: None = Depends(require_web)):
    """主单解析记录（state / 36 列可编辑面 / 红旗与保真 / L1 原文 / 列名中文分组表）。没解析过就 404。"""
    rec = master_pipeline.public(master_pipeline.read_master(mawb))
    if rec is None:
        raise HTTPException(404, "这条主单还没有解析结果（先在审核台检索一次主单号）")
    return rec


class MasterSubmitBody(BaseModel):
    mawb: str = ""
    reviewer: str = ""
    reviewer_role: str = ""      # 一律由服务端按会话覆写：请求里写什么都不认
    ams: dict = {}
    acked_flags: list = []


@app.post("/master/stage")
def master_stage(body: MasterSubmitBody, user: dict = Depends(current_user)):
    """主单暂存：36 列的人工核对结果存进这条主单缓存（留痕、给同事复核），并在公司那边占个位
    （`mawb2`，落状态 0，可反复改）。

    分单有了暂存档，主单也必须有——需求二说的是"主单和分单的暂存内容"。从前 `MV.edit`
    只活在浏览器里（换台机器、刷新一次就没了），录入员根本交不出主单的核对结果。
    公司那一发失败不回滚本机这份，原因随响应带回去（见 _company_stage）。"""
    try:
        rec = master_pipeline.stage_master(body.mawb, body.ams, str(user.get("name") or ""),
                                           str(user.get("role") or ""), body.acked_flags)
    except ValueError as e:
        raise HTTPException(400, str(e))
    company = {"ok": False, "skipped": "没有主单号"}
    if rec.get("mawb"):
        try:
            res = company_api.stage_master({"mawb": rec["mawb"], "ams": rec.get("ams_final") or {}})
            company = {"ok": True, "mode": res.get("mode"), "action": res.get("action")}
        except company_api.CompanyLocked as e:
            LOG.info("主单公司锁行，本机这份仍已存 %s who=%s：%s", rec.get("mawb"),
                     user.get("name"), e)
            company = {"ok": False, "locked": True, "status": e.status, "error": str(e)[:200]}
        except Exception as e:
            LOG.warning("主单公司暂存失败 %s who=%s：%s", rec.get("mawb"), user.get("name"), e)
            company = {"ok": False, "error": str(e)[:200]}
    LOG.info("主单暂存 who=%s(%s) %s 改动=%d 列", user.get("name"), user.get("role"),
             body.mawb, len((rec.get("events") or [{}])[-1].get("edits") or {}))
    return {"ok": True, "mawb": rec.get("mawb"), "edited": len(rec.get("events")[-1]["edits"]),
            "stager": rec.get("stager"), "company": company,
            "reviewed_by_inputter": store.reviewed_by_inputter(rec)}


@app.post("/master/submit")
def master_submit(body: MasterSubmitBody, user: dict = Depends(current_user)):
    """把人工核对过的主单**提交发送**给公司（j9 mawb3，落库状态 1）。主单侧唯一对外写出口，只有人工点才发。

    门与错误码与分单同构：红旗未清/未署名 → 400（红旗在服务端按当前值重算，前端藏旗无效）；
    公司侧 SEND_STATUS 非 0（已提交发送或外围已发送成功，公司拒改）→ 409 且不发写请求；
    接口不通/没配 → 502，绝不写台账。
    署名同样取登录会话，不认前端传的名字。"""
    body.reviewer = str(user.get("name") or "")
    body.reviewer_role = str(user.get("role") or "")
    try:
        out = master_pipeline.submit_master(body.model_dump())
        LOG.info("主单提交 who=%s %s 模式=%s 公司=%s 确认旗=%d 条",
                 user.get("name"), body.mawb, out.get("mode"), out.get("action") or "-",
                 len(body.acked_flags or []))
        return out
    except master_pipeline.MasterFlagged as e:
        # detail 带结构化红旗：前端逐条出「确认无误」按钮，不用解析提示文案
        raise HTTPException(400, {"message": str(e), "flags": e.flags})
    except company_api.CompanyLocked as e:
        LOG.warning("主单回传被公司锁行：%s → %s", body.mawb, e)
        raise HTTPException(409, str(e))
    except (company_api.CompanyApiError, company_api.CompanyNotConfigured) as e:
        LOG.warning("主单回传失败：%s → %s", body.mawb, e)
        raise HTTPException(502, str(e))


class SubmitBody(BaseModel):
    tickets: list = []
    submitted_at: str = ""
    client: str = ""


def _read_json(path: Path):
    """读一份本机 JSON，坏文件/没文件都当"没有"，不让提交门自己被 IO 异常绊倒。"""
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:
        return None


def _l2_l1(stem: str, tk: dict):
    """提交门要看的两份本机依据：L2 原文口径与 L1 转录。
    优先读服务器上落盘的那份（前端带来的值改了也不算数），落盘没有才退回包里 `raw_original`；
    两样都没有就只算 L3 那一层——别的机器解析的票也得能提交，不能凭空报旗。"""
    name = _safe_stem(stem)
    raw = transcript = None
    if name:
        raw = _read_json(config.OUTPUT_RAW_DIR / f"{name}.json")
        transcript = _read_json(config.TRANSCRIPT_DIR / f"{name}.json")
    if raw is None and isinstance(tk.get("raw_original"), dict):
        raw = tk["raw_original"]
    return raw, transcript


def _empty_cols(raw: dict | None, rec: dict) -> list:
    """提交这一版里「票面本来就没印」的列：L2 与 L3 都空——与审核台 fieldStatus 同一条判据。
    由服务端按落盘那份算，不数前端报的几个：空栏留痕以后要按列聚合，口径只能有一处
    （红旗那条「以服务端重算为准」是同一个教训，见 test_server_recomputes_house_flags_and_blocks_unacked）。"""
    out = []
    for k in fieldspec.desk_keys():
        if str((raw or {}).get(k) or "").strip() or str(rec.get(k) or "").strip():
            continue
        out.append(k)
    return out


def _sent_fingerprint(mawb: str, hawb: str, rec: dict) -> str:
    """这次到底把什么发给了公司。整表写回出事时（哪列被抹成 NULL）靠它对账重算。"""
    return hashlib.sha256(json.dumps({"mawb": mawb, "hawb": hawb, "air": rec}, sort_keys=True,
                                     ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


@app.post("/submit")
def submit(body: SubmitBody, user: dict = Depends(current_user)):
    """制单员提交：这是"回传公司 + 进索引"的唯一出口。

    前三道门都以服务端为准，不信前端（前端那道只是体验）：
    ① 主/分单号必须可归一化——复合键是"日后能否按号打开原件"的唯一连接键，缺号进索引等于塞坏数据；
    ② 红旗按**提交时刻的当前值**重算（`jobs.recompute_flags`，与解析时同一批判据同一套文案），
       未清的必须逐条 `acked_flags` 确认；改了值旧旗文案会变，自动要重新确认；
    ③ 没有"录入员复核过的暂存记录"的，**照样直发**（2026-10-14 删掉了那次确认框），但台账留痕
       `no_inputter_review`——这一档不能悄悄发生却也不拦人，否则需求三答不出
       "没复核就发的"和"复核过才发的"分别错多少。
    任一不过 → 400 且**一个公司请求都不发**（先整批门检，再逐张发，别提交一半）。
    复核人取登录会话身份。缺号≠处理失败：这里只挡提交，票的 L2/L3 早已落盘，补号后重新提交即可。
    机批/监控/站点不经过这里，所以它们落的脏 output 永远进不了索引。

    第四道门是**顺序**（2026-10-14 用户定案："提交的时候要先提交主单，再提交分单"）：这条分单
    名下的主单还没提交发送就先别发。公司那边收一条指向不存在主单的分单，等于交付半套数据，
    比两张都没发出去更难收拾。挡下时不去替人点主单那颗提交——那一发只能是人工那一下。"""
    tickets = body.tickets if isinstance(body.tickets, list) else []
    if not tickets:
        raise HTTPException(400, "没有待提交的票据")
    masters = store.master_ledger()      # 整批只读一次：循环里反复开台账是白费
    asked = {}                           # 同一次提交里同一条主单只问公司一遍（A1）：
    passed, blocked, notes = [], {}, []  # 30 张同主单的票，从前是 30 次串行往返堵住唯一出口
    for tk in tickets:
        if not isinstance(tk, dict):
            raise HTTPException(400, "票据格式不正确：tickets 里每一项都应是对象")
        rec = tk.get("air_reviewed") if isinstance(tk.get("air_reviewed"), dict) else {}
        mawb = str(rec.get("MAWB_NO", "") or "").strip()
        hawb = str(rec.get("HAWB_NO", "") or "").strip()
        stem = str(tk.get("stem") or tk.get("filename") or "").strip()
        if not store.norm_no(mawb) or not store.norm_no(hawb):
            raise HTTPException(400, f"主单号/分单号不能为空（补齐后才能回传公司并进索引）：{stem or tk.get('filename', '')}")
        raw, transcript = _l2_l1(stem, tk)
        acked = [str(a) for a in (tk.get("acked_flags") if isinstance(tk.get("acked_flags"), list) else [])]
        left = [f for f in jobs.recompute_flags(raw, rec, transcript) if f not in acked]
        if left:
            raise HTTPException(400, {"message": f"{hawb} 还有未清的红旗：逐条确认后才能回传公司",
                                      "flags": left})
        # 空栏两档留痕（v1.16.0）：哪些列在提交这一版真的没值由服务端算，人逐列确认「本就为空」的
        # 记 acked_empty，没确认就点第二次硬发出去的记 skipped_empty。有值的列即使前端报了也不收。
        blanks = _empty_cols(raw, rec)
        claim = {str(x) for x in (tk.get("acked_empty") if isinstance(tk.get("acked_empty"), list) else [])}
        acked_empty = [k for k in blanks if k in claim]
        skipped_empty = [k for k in blanks if k not in claim]
        # 第三道门（2026-10-08 定案，2026-10-14 用户改口径："把这个提示删掉"）：
        # 没有录入员复核过的暂存记录**照样直发，不再多点一次确认**，但台账仍记
        # no_inputter_review。留痕才是这条的目的——确认框本身不产生任何数据，
        # 砍掉它不丢信息，砍掉记号才会丢"没复核就发的那批错得多不多"这个对照。
        # 判据只认服务器上的暂存事件，不认前端写的任何名字。
        st = store.load_staged(stem) or {}
        reviewed = store.reviewed_by_inputter(st)
        # 提交人自己就是录入员：这次提交即他的核对，台账记 inputter_self_submit、
        # 不记"未经录入员复核"——那一档是"制单员跳过复核直发"的对照组，
        # 混进录入员自己的提交，对照就废了（2026-10-12 郭健康现场）。
        self_submit = (not reviewed) and str(user.get("role") or "") == "inputter"
        # 顺序这一道：本机主单台账没记的，去问公司一次（可能别处已经发过了），再决定挡不挡。
        # 放在最后是因为前面几道都是"这张票自己还没好"，先把内容说清，才轮到"先去做另一件事"。
        mk = store.norm_no(mawb)
        if mk not in masters:
            # 第一次问的时刻与从前一字不差，后到的票复用这一答案——比"循环前预取"的窗口更短。
            if mk not in asked:
                asked[mk] = company_api.master_sent(mawb)
            verdict, why = asked[mk]
            if verdict == "not_sent":
                blocked[mk] = mawb
            elif verdict == "unknown":
                notes.append(f"没读到主单 {mawb} 在公司侧的状态（{why}）："
                             "本机台账说它没提交过，这次按你说的走")
        passed.append((tk, stem, mawb, hawb, rec, acked, st, reviewed, acked_empty,
                       skipped_empty, self_submit))
    if blocked:
        names = "、".join(list(blocked.values())[:2])
        more = "" if len(blocked) <= 2 else f" 等 {len(blocked)} 条"
        raise HTTPException(400, {
            "message": f"主单 {names}{more} 还没提交发送：先把主单交了，再回来提交它名下的分单",
            "ack": "master_first", "mawb": next(iter(blocked.values()))})
    results, codes = [], []
    specs, done = [], []      # 台账一批一次写：先收齐回执，循环结束后统一落账
    for tk, stem, mawb, hawb, rec, acked, st, reviewed, acked_empty, skipped_empty, self_submit in passed:
        # 逐张收集成败，不在第一张错上 raise：/submit 收的是列表，前面几张已经写进公司库了，
        # 让它们随异常一起消失，调用方就分不清"没发出去"和"发出去了但没回执"，
        # 再点一次等于第二次整表写回。
        try:
            receipt = _company_submit({"mawb": mawb, "hawb": hawb, "stem": stem, "air": rec})
        except company_api.CompanyLocked as e:
            LOG.warning("分单回传被公司锁行：%s|%s（stem=%s）→ %s", mawb, hawb, stem, e)
            results.append({"stem": stem, "ok": False, "error": f"公司锁行：{e}"})
            codes.append(409)
            continue
        except (company_api.CompanyApiError, company_api.CompanyNotConfigured) as e:
            LOG.warning("分单回传失败：%s|%s（stem=%s）→ %s", mawb, hawb, stem, e)
            results.append({"stem": stem, "ok": False, "error": str(e)})
            codes.append(502)
            continue
        # 回执到手才进批次——顺序说死在用例里：先写台账后发公司，会把「没发出去」记成「发出去了」
        specs.append(dict(stem=stem, mawb=mawb, hawb=hawb,
                          reviewer=str(user.get("name") or ""), receipt=receipt,
                          acked_flags=acked,
                          edited_fields=(tk.get("edited_fields")
                                         if isinstance(tk.get("edited_fields"), list) else []),
                          sent_fingerprint=_sent_fingerprint(mawb, hawb, rec),
                          company_action=str(receipt.get("action") or ""),
                          air_sent=rec, stager=st.get("stager") or "",
                          staged_at=st.get("staged_at") or "",
                          no_inputter_review=not reviewed and not self_submit,
                          inputter_self_submit=self_submit,
                          skipped_empty=skipped_empty, acked_empty=acked_empty))
        done.append((stem, mawb, hawb, receipt, acked, st, acked_empty, skipped_empty, reviewed))
    # 一批回执拿齐，台账一次写完：M 张票 = 1 次整份读写（从前是 M 次，2026-10-17 效率审查 A2）
    for (stem, mawb, hawb, receipt, acked, st, acked_empty, skipped_empty, reviewed), entry in \
            zip(done, store.mark_submitted_batch(specs)):
        if st:
            store.mark_staged_submitted(stem, str(user.get("name") or ""), entry["sent_fingerprint"])
        results.append({"stem": stem, "ok": True, "submitted": True,
                        "key": entry["key"], "mode": receipt.get("mode")})
        LOG.info("分单提交 who=%s %s|%s 模式=%s 公司=%s 幂等重放=%s 改动=%d 列 确认旗=%d 条 暂存人=%s"
                 " 空栏=%d（确认 %d / 跳过 %d）%s",
                 user.get("name"), mawb, hawb, receipt.get("mode"), receipt.get("action") or "-",
                 bool(receipt.get("idempotent")), len(entry.get("edited_fields") or []), len(acked),
                 st.get("stager") or "-", len(acked_empty) + len(skipped_empty),
                 len(acked_empty), len(skipped_empty),
                 "" if reviewed else "（未经录入员复核，已确认）")
    if codes:
        bad = [r for r in results if not r["ok"]]
        n_ok = len(results) - len(bad)
        raise HTTPException(502 if 502 in codes else 409, {
            "message": f"{len(bad)} 张没发出去（{bad[0]['error']}）"
                       + (f"；另外 {n_ok} 张已成功并发出，台账已记，别再重发这批" if n_ok else ""),
            "results": results})
    return {"ok": True, "results": results, "notes": notes}


class StageBody(BaseModel):
    tickets: list = []


def _company_stage(saved: dict, user: dict) -> dict:
    """暂存顺手在公司那边占个位（`hawb2`，落 `SEND_STATUS=0`，可反复改、外围不取）。

    两个前提缺一不可：主/分单号都在（公司按号定位那一行，缺号连行都找不到），
    以及模式是 live（mock 下不发——回归与端到端验证都靠这条）。
    发不成功**不回滚本机那份**：同事照样能接着复核，只是公司那边还没占上位，
    所以把原因原样带回去让人看得见，而不是让整次暂存报错。"""
    if not (saved.get("mawb") and saved.get("hawb")):
        return {"ok": False, "skipped": "没解析出主单号或分单号，公司那边占不了位"}
    try:
        res = company_api.stage_order({"mawb": saved["mawb"], "hawb": saved["hawb"],
                                       "stem": saved["stem"], "air": saved["air_final"]})
    except company_api.CompanyLocked as e:
        # 锁行不是失败：那一行已经离开状态 0（已提交发送 / 外围已发送），公司本来就不收改动。
        # 说成"失败"会让人以为整次白点，于是再点一次（2026-10-14 公司那台现场的截图）。
        LOG.info("公司锁行，本机这份仍已存 stem=%s who=%s：%s", saved["stem"], user.get("name"), e)
        return {"ok": False, "locked": True, "status": e.status, "error": str(e)[:200]}
    except Exception as e:
        LOG.warning("公司暂存失败 stem=%s who=%s：%s", saved["stem"], user.get("name"), e)
        return {"ok": False, "error": str(e)[:200]}
    return {"ok": True, "mode": res.get("mode"), "action": res.get("action")}


@app.post("/stage")
def stage(body: StageBody, user: dict = Depends(current_user)):
    """暂存：核对结果存到服务器（留痕、给同事复核），并在公司那边**占个位**（状态 0，可反复改）。

    和 /submit 的分工是刻意的，别混：那边是交付——号必须齐全、红旗必须逐条确认、发出去就进台账
    进索引，而且**离开状态 0 就再也改不动**；这边只是把工作进度交出去——缺号也允许存本机
    （那张票还是同一张，别人能接着补），缺号时干脆不发公司，也绝不写台账、绝不碰提交发送口。
    需求二"录入员核对之后再发公司"与需求三"改动按人分账"都靠本机这一层落地：
    人工值从前只活在浏览器 localStorage 里，换台机器、换个人就什么都没留下。
    逐字段差异由服务器拿 output/air 那份模型原样来算，不信前端的 edited_fields。"""
    tickets = body.tickets if isinstance(body.tickets, list) else []
    if not tickets:
        raise HTTPException(400, "没有要暂存的票据")
    out = []
    for tk in tickets:
        if not isinstance(tk, dict):
            raise HTTPException(400, "票据格式不正确：tickets 里每一项都应是对象")
        stem = str(tk.get("stem") or "").strip()      # 不退回 filename：那是带扩展名的原名，对不上落盘的键
        if not stem:
            raise HTTPException(400, "这张票没有 stem，暂存不知道要存哪一张（解析过的票都带 stem）")
        rec = tk.get("air_reviewed") if isinstance(tk.get("air_reviewed"), dict) else {}
        if not rec:
            raise HTTPException(400, f"这张票没有可存的核对内容：{stem}")
        try:
            saved = store.stage_ticket(stem, rec, str(user.get("name") or ""),
                                       str(user.get("role") or ""), tk.get("acked_flags"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        out.append({"stem": saved["stem"], "hawb": saved["hawb"], "edited": len(saved["edits"]),
                    "company": _company_stage(saved, user)})
        LOG.info("暂存 who=%s(%s) stem=%s %s|%s 改动=%d 列 本次改=%d 列",
                 user.get("name"), user.get("role"), saved["stem"], saved["mawb"], saved["hawb"],
                 len(saved["edits"]), len(saved["events"][-1]["edits"]))
    return {"ok": True, "staged": out}


@app.get("/models")
def models():
    """可选模型清单 + 两条链各自在用什么。免登录：审核台要先填出下拉（可能还没登录）。
    `thinking` 一并报回（顶层=当前生效档，预设里=这家能不能开关）：思考档的下拉必须由这份
    数据填，浏览器自己记一份的话，重启后 .env 说了算、界面却停在旧档。"""
    mb = config.master_model_bundle()
    return {"current": config.VLM_MODEL_CHOICE, "effective": config.VLM_MODEL,
            "vision": config.MODEL_VISION, "thinking": config.thinking(),
            "master": {"choice": mb["choice"], "model": mb["model"], "vision": mb["vision"],
                       "label": mb.get("label") or "", "key_configured": config.master_api_key_configured(),
                       "thinking": mb["thinking"]},
            "presets": [{"key": k, "model": v["model"], "vision": bool(v["vision"]), "label": v["label"],
                         "thinking": config.thinking(v)}
                        for k, v in config.MODEL_PRESETS.items()]}


@app.post("/model")
def switch_model(body: ModelChoice, _: None = Depends(require_admin)):
    """切换提取用的模型：本进程立即生效，并写回 .env 供重启后与 CLI/批处理沿用。
    换模型直接改变提取结果——管理员专属（管理员登录会话）。
    `chain=master` 切的是主单链（写 MASTER_VLM_MODEL 那一行），默认切分单链；两条链互不带对方。"""
    choice = body.model.strip()
    chain = (body.chain or "hawb").strip().lower()
    if chain not in ("hawb", "master"):
        raise HTTPException(400, f"chain 只能是 hawb 或 master，收到 {chain!r}")
    if not choice:
        raise HTTPException(400, "model 不能为空：填预设键（如 intern-s2-official）或 org/模型 id")
    if not _MODEL_ID_OK.match(choice):
        raise HTTPException(400, f"模型名不合法：{choice!r}（只允许字母数字与 . _ - / :）")
    if chain == "master":
        info = config.set_master_model(choice)
        try:
            config.persist_model_choice(info["choice"], "MASTER_VLM_MODEL")
        except OSError as e:
            raise HTTPException(500, f"主单链模型已在本进程切换，但写 .env 失败（重启后仍是旧模型）：{e}")
        return {"ok": True, "chain": "master", "choice": info["choice"],
                "model": info["model"], "vision": info["vision"]}
    info = config.set_model(choice)
    try:
        config.persist_model_choice(info["choice"])
    except OSError as e:
        raise HTTPException(500, f"模型已在本进程切换，但写 .env 失败（重启后仍是旧模型）：{e}")
    return {"ok": True, "chain": "hawb", **info}


@app.post("/model/thinking")
def switch_thinking(body: ThinkingChoice, request: Request, _: None = Depends(require_admin)):
    """思考档三档："" 跟随预设 / "0" 关 / "1" 开。本进程立即生效并写回 .env（重启后、批处理沿用）。
    和换模型同一道门——它同样改变提取结果，而且开思考要多烧几千 reasoning token、单票更慢。
    只对预设里声明了该参数的渠道生效：官方书生不接受 extra_body，硬塞直接 400，所以在不支持的
    渠道上拨到"开"只是一次无害的偏好记录，切到 Qwen 才真的生效（响应里的 supported 会说清）。"""
    if body.thinking is None:
        raise HTTPException(400, '要带 thinking 字段："" 跟随预设 / "0" 关 / "1" 开')
    try:
        th = config.set_thinking(body.thinking)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(500, f"思考档已在本进程切换，但写 .env 失败（重启后仍是旧档）：{e}")
    u = auth.session_user(request) or {}
    LOG.info("思考档 who=%s mode=%s 生效=%s 渠道=%s 支持=%s",
             u.get("name"), th["mode"], th["on"], config.VLM_MODEL_CHOICE, th["supported"])
    return {"ok": True, "thinking": th, "model": config.VLM_MODEL}


DESK_QUEUE = desk_queue.DeskQueue(slots=config.DESK_CONCURRENCY, max_pending=config.DESK_QUEUE_MAX)


def _parse_ticket(tmp_dir: Path, path: Path, filename: str, who: str, role: str) -> dict:
    """队列 worker 里跑的那一段：解析 → 重算汇总 → 清临时目录。返回的就是从前 /extract 的响应体，
    前端因此只多了"轮询 /job"这一步。

    一律 save=True：这张票只有落了盘，别人才检索得到、才打得开原件（需求一的根）。
    who/role 取登录会话，随质检记录落盘——不然"这张票谁传的"只活在日志和内存里。"""
    meta = {"uploader": who, "uploader_role": role}
    try:
        try:
            # fresh=True：上传这条路是人有意重跑这一票（重传也走这里），不复用上次的 L1 转录。
            # 复用只留给自动路径（批处理/监控/站点），口径见 jobs.handle_file。
            r = handle_file(path, True, meta, fresh=True)
            if not r["error"]:
                store.schedule_rebuild()   # 合并重建：安静 3 秒后一次，别 4 个 worker 抢着全量扫
        except (Exception, SystemExit) as e:
            # handle_file 自己会兜住绝大部分失败；真漏出来的一律变成任务结果，别把 worker 带走
            r = {"stem": path.stem, "channel": "", "elapsed": 0.0, "raw": None, "air": None,
                 "transcript": None, "qc": None, "error": f"{type(e).__name__}: {e}"}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    if r["error"]:
        LOG.warning("解析失败 who=%s file=%s stem=%s 耗时=%ss → %s",
                    who, filename, r["stem"], r["elapsed"], r["error"])
        return {"ok": False, "error": r["error"], "elapsed": r["elapsed"],
                "filename": filename, "stem": r["stem"], "qc": r["qc"]}
    LOG.info("解析完成 who=%s file=%s stem=%s 通道=%s 耗时=%ss 红旗=%d",
             who, filename, r["stem"], r["channel"], r["elapsed"],
             len((r["qc"] or {}).get("flags") or []))
    return {"ok": True, "filename": filename, "stem": r["stem"], "channel": r["channel"],
            "elapsed": r["elapsed"], "qc": r["qc"], "raw": r["raw"], "air": r["air"],
            "transcript": r["transcript"]}


@app.post("/extract")
async def extract(file: UploadFile = File(...), user: dict = Depends(current_user)):
    """上传一张分单：入队即 202，结果靠 `GET /job/{job}` 取。

    从前这个接口是同步等的：一次解析最坏十几分钟，nginx 900 秒先给人一个 504，
    后端却还在继续跑继续花钱。现在解析在队列里跑（同时最多 DESK_CONCURRENCY 张），
    排队位次回给前端显示"排队中·前面 N 张"。塞满则直接 429 拒收，不再让每个人都超时。

    以前这里还有一个 `?save=` 参数（默认 False），勾不勾由浏览器里那个复选框决定；不勾时
    四层结果一个都不写、上传的原件当场删掉，结果就是这张票在服务器上根本不存在——
    录入员检索主单查不到同事刚传的票（需求一）。现在一律落盘，参数不再接受，带了也忽略。"""
    user_name = str(user.get("name") or "?")
    user_role = str(user.get("role") or "")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in config.ALL_EXTS:
        raise HTTPException(400, f"不支持的文件类型 {suffix}，支持 {sorted(config.ALL_EXTS)}")

    # PDF/XLSX 需要文件路径，且落盘名要用票面原名（用 NamedTemporaryFile 的随机名会丢出处）
    tmp_dir = Path(tempfile.mkdtemp(prefix="hawb_upload_"))
    path = tmp_dir / safe_name(file.filename or "", suffix)
    handed_off = False
    try:
        await _save_upload(file, path)
        jid = DESK_QUEUE.submit(lambda: _parse_ticket(tmp_dir, path, file.filename,
                                                      user_name, user_role),
                                who=user_name, name=file.filename or "",
                                cleanup=lambda: shutil.rmtree(tmp_dir, ignore_errors=True))
        handed_off = True            # 临时目录从这一刻归 worker 清
    except desk_queue.QueueFull as e:
        raise HTTPException(429, str(e))
    finally:
        if not handed_off:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    st = DESK_QUEUE.get(jid) or {}
    return JSONResponse(status_code=202, content={
        "ok": True, "queued": True, "job": jid, "filename": file.filename,
        "position": st.get("position", 0), "waiting": st.get("waiting", 0)})


@app.get("/job/{job_id}")
def job_result(job_id: str, user: dict = Depends(current_user)):
    """解析作业的状态与结果（queued 时带 position）。

    作业表只在内存里：服务重启后旧作业 404——票本身和已完成的解析都在 output/，
    前端拿 404 会自己重传一次，不用人再点。"""
    j = DESK_QUEUE.get(re.sub(r"[^0-9a-zA-Z]", "", job_id or ""))
    if not j:
        raise HTTPException(404, "没有这个解析作业（服务可能重启过），请重新上传这张票")
    out = {"state": j["state"], "position": j.get("position", 0),
           "waiting": j.get("waiting", 0), "running": j.get("running", 0)}
    if j["state"] == desk_queue.DONE:
        out["result"] = j["result"]
    elif j["state"] == desk_queue.FAILED:
        out["error"] = j["error"]
    return out


@app.post("/job/{job_id}/cancel")
def job_cancel(job_id: str, user: dict = Depends(current_user)):
    """摘掉一张还没开始的解析（列队里那张不想等了）。

    三种结局原样回给前端，别替它圆场：`cancelled` 真的不跑了；`running` 掐不掉——模型调用
    和 LibreOffice 子进程都在 worker 手里，硬杀会把解析到一半的票留在盘上；`gone` 没这一张
    （多半是服务重启过）。作业号本身就是凭证：`GET /job/{id}` 连票面内容都回，也没设身份门，
    只在取消这一半单独装一道，只会让人以为"别人的票我删不掉"而实际防的是另一回事。"""
    jid = re.sub(r"[^0-9a-zA-Z]", "", job_id or "")
    state = DESK_QUEUE.cancel(jid)
    LOG.info("取消解析作业 who=%s job=%s → %s", user.get("name"), jid, state)
    return {"ok": state == "cancelled", "state": state}


class ReviewBody(BaseModel):
    kind: str = "hawb"        # hawb = 分单（key 给 stem）；mawb = 主单（key 给主单号）
    key: str = ""


@app.post("/review")
def review(body: ReviewBody, user: dict = Depends(current_user)):
    """录入员"这张我看过了，先不发"——只留一条痕：不写核对结果、不碰公司、不改任何值。

    为什么不复用暂存：`/stage` 会把 `air_final` 写进去并顺手在 j9 占位（状态 0）。
    而"先放着"这张票很可能还要回去问制单员——那时候把值交给公司就成了一次假交付，
    也正是用户说的"录入员正常情况下不该暂存"那一层。
    留痕与暂存共用同一份记录、同一处判据（`store.staged_edits` 把 review 那条挑出去，
    所以一次复核既不会把 parsed 顶成 staged，也不会让「谁交的这列」答错）。
    形状与 `/void` 一致：`kind` + `key`。"""
    k = str(body.kind or "hawb").strip().lower()
    who, role = str(user.get("name") or ""), str(user.get("role") or "")
    if k not in ("hawb", "mawb"):
        raise HTTPException(400, "票种只认 hawb / mawb")
    try:
        rec = (master_pipeline.review_master(body.key, who, role) if k == "mawb"
               else store.review_ticket(body.key, who, role))
    except ValueError as e:
        raise HTTPException(400, str(e))
    last = (rec.get("events") or [{}])[-1]
    LOG.info("复核留痕 who=%s(%s) %s=%s at=%s", who, role, k, body.key, last.get("at"))
    return {"ok": True, "kind": k, "key": rec.get("stem") or rec.get("mawb") or body.key,
            "at": str(last.get("at") or "")}


@app.get("/results")
def results(_: None = Depends(require_web)):
    raw_dir, air_dir = config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR
    return {
        "raw_count": len(list(raw_dir.glob("*.json"))) - (1 if (raw_dir / "all_hawbs.json").exists() else 0),
        "air_count": len(list(air_dir.glob("*.json"))) - (1 if (air_dir / "all_hawbs_air.json").exists() else 0),
        "raw_dir": str(raw_dir),
        "air_dir": str(air_dir),
    }


@app.get("/day")
def day(date: str = "", all: int = Query(0, ge=0, le=1),
        scope: str = Query("", description="mine / all / toreview（待我复核）/ reviewed（我核过·没提交）"),
        q: str = Query("", description="分单号或主单号：归一后跨所有日子找"),
        user: dict = Depends(current_user)):
    """某一天经手过的**分单与主单**一张表看完（解析/红旗/提交/原件），行上带 kind。
    台账是本机的、两台机器各记各账，这一页就是把"今天谁做了什么"摊开在同一张桌子上；
    公司侧的发送状态要按主单查（限流），所以不混进这一页，去「主单检索」看。
    作废的行照样返回（带 voided），显示与否由前端那一个筛决定——数据层不藏东西。

    **默认只给登录者自己动过的那些**（上传解析 / 暂存核对 / 提交发送任一，2026-10-12 需求：
    "每个人记录每个人的，不能混在一起"）。`all=1` 只对管理员放开，非管理员带了它仍然只拿到
    自己那一份。过滤发生在服务端而不是前端藏行：后者等于把别人的经手记录发到他的浏览器里，
    F12 就能翻出来，而 `scope` 这一格报的仍是"只看你的"。"""
    d = (date or time.strftime("%Y-%m-%d")).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        raise HTTPException(400, "date 要写成 2026-09-30 这种四位日期")
    q = str(q or "").strip()
    if q and (len(q) > 40 or re.search(r"[/\\]", q)):
        # 长度与分隔符都要挡：这一格最后会去比 norm_no 过的字符串，不挡的话在 Linux 上
        # 是一次 `stat()` 抛 ENAMETOOLONG 而不是干净地"查无此号"（同族的坑踩过一次）
        raise HTTPException(400, "这个号没法查（太长或带路径分隔符）")
    qn = store.norm_no(q)
    who = str(user.get("name") or "")
    role = str(user.get("role") or "")
    # 谁能拿哪一档，只有这一处判据：录入员的职责就是复核别人交上来的票，所以"这台机器所有人的"
    # 与两档复核清单对他放开；制单员仍只看自己（他没提这个需求，放开是另一件事）。
    # 拿不到的人**退回他自己那一份并把 scope 如实报成 mine**，不是报错——前端认的就是响应里这一格。
    may = role in ("admin", "inputter")
    want = (scope or "").strip().lower() or ("all" if may and (qn or all) else "mine")
    if want not in ("mine", "all", "toreview", "reviewed"):
        want = "mine"
    if want != "mine" and not may:
        want = "mine"
    # 复核那一档与号检索都不按天：昨天暂存的今天还得核，手里只有一个号的时候更不该先猜哪天。
    flat = want in ("toreview", "reviewed") or bool(qn)
    day, every = store.day_and_all("" if flat else d)   # 一次全量扫（B1）：两个口径内存筛
    if want in ("toreview", "reviewed"):
        rows = [r for r in every if not r.get("submitted") and not r.get("voided")
                and bool(r.get("reviewed_by_inputter")) == (want == "reviewed")]
    else:
        rows = day if want == "all" else [r for r in day if store.owns(r, who, None if flat else d)]
    if qn:
        # 整号相等，或（归一后至少 5 位时）截段命中：只允许整号的话，人敲号尾那几个字符
        # 会看到"查无此号"，而那正是这一页最容易被误读成"票丢了"的一种空。
        loose = len(qn) >= 5

        def _hit(v):
            v = store.norm_no(v)
            return bool(v) and (v == qn or (loose and qn in v))

        rows = [r for r in rows if _hit(r.get("mawb")) or _hit(r.get("hawb"))
                or _hit(r.get("stem")) or _hit(r.get("source_name"))]
    all_days = every
    others = 0 if want in ("toreview", "reviewed") else len(day) - len(rows)
    who_r = {str((r["submitted"] or {}).get("reviewer") or "") for r in rows} - {""}
    return {"ok": True, "date": "" if flat else d, "rows": rows, "reviewers": sorted(who_r),
            "scope": want, "me": who, "others_today": others, "q": q, "count": len(rows),
            "last_date": store.day_latest(all_days, None if want in ("all", "toreview", "reviewed") or qn else who),
            "machine_last_date": store.day_latest(all_days)}


class VoidBody(BaseModel):
    kind: str = "hawb"          # hawb = 分单行（key 给 stem）；mawb = 主单行（key 给主单号）
    key: str = ""
    void: bool = True           # False = 撤销作废


@app.post("/void")
def void_row(body: VoidBody, user: dict = Depends(current_user),
             _: None = Depends(require_admin)):
    """台账那一行的「作废 / 恢复」：只往 voided.json 写一个戳（谁、何时），
    **提交台账、暂存记录、归档原件、复合键索引一律不动**——"这张票回传过公司"的凭据
    不能从表里点一下就没了，否则以后出了争议连查都没处查。

    2026-10-14 用户改口径：这一颗只给管理员（v1.11.0 起是"登录就能点"，为的是让制单员
    手边能清自己的测试数据）。软闸门那两样一样没丢——二次确认照旧、戳上照旧记是谁作废的，
    变的只是点它的人；恢复与作废同一条门，不然留一个"谁都能翻回来"的后门。"""
    if not str(body.key or "").strip():
        raise HTTPException(400, "要带单号：分单给 stem，主单给主单号")
    try:
        if body.void:
            entry = store.void_row(body.kind, body.key, str(user.get("name") or ""))
        else:
            store.unvoid_row(body.kind, body.key)      # 没作废过也回 200：恢复要幂等
            entry = None
    except ValueError as e:
        raise HTTPException(400, str(e))
    LOG.info("台账作废 who=%s(%s) %s=%s -> %s", user.get("name"), user.get("role"),
             body.kind, body.key, "作废" if body.void else "恢复")
    return {"ok": True, "voided": entry}


@app.get("/tickets")
def tickets(date: str = "", state: str = "", mawb: str = "", _: None = Depends(require_web)):
    """这台服务器上经手过的票与它们的状态：parsed / staged / submitted / failed。

    `/day` 就是它按天的视图，参数都可省（不填 date=全表）。这张表的读数全部来自磁盘
    （质检记录 + 暂存文件 + 提交台账），不看任何浏览器状态——需求一"录入员要看得见制单员
    传上来的票"、需求二"谁暂存过、改了哪几列"读的都是它。计数不在这里算（口径归 logic.js 一处）。"""
    if state and state not in ("parsed", "staged", "submitted", "failed"):
        raise HTTPException(400, "state 只能是 parsed / staged / submitted / failed")
    if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HTTPException(400, "date 要写成 2026-09-30 这种四位日期")
    rows = store.ticket_rows(date=date or None, state=state or None, mawb=mawb or None)
    return {"ok": True, "rows": rows}


@app.get("/stats")
def stats_report(date_from: str = Query("", alias="from", description="含端点的起始日 yyyy-mm-dd"),
                 date_to: str = Query("", alias="to", description="含端点的截止日 yyyy-mm-dd"),
                 group_by: str = "", _: dict = Depends(require_admin)):
    """解析正确率（需求三）：模型那一版与人最后定下的那一版逐列比。

    算术只有 `stats.py` 一处——页面、导出、以后任何读数都从这里出，同一套算术写两遍迟早给出
    两个百分比。`acc_field` 是"模型本身准不准"，`acc_after_review` 才是"人还剩多少活"
    （判断能不能直接对接平台看后者）；历史没留值的台账进 `legacy`，不进分母。"""
    if group_by and group_by not in stats.GROUPS:
        raise HTTPException(400, "group_by 只能是 " + "、".join(stats.GROUPS))
    for d in (date_from, date_to):
        if d and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            raise HTTPException(400, f"日期要写成 2026-09-30 这种四位日期：{d!r}")
    rows = stats.collect(date_from, date_to)
    return {"ok": True, "window": {"from": date_from, "to": date_to},
            "summary": stats.summary(rows, group_by or None), "rows": rows}


@app.get("/metrics")
def metrics_report(date_from: str = Query("", alias="from", description="含端点的起始日 yyyy-mm-dd"),
                   date_to: str = Query("", alias="to", description="含端点的截止日 yyyy-mm-dd"),
                   _: dict = Depends(require_admin)):
    """额度与耗时花在哪：按天 / 按模型 / 按阶段聚合每张票 qc 里的 token 账与分段耗时，
    外加失败按类分档。

    `usage` 与 `timings` 从 v1.20.0 起就落在 qc 里了，缺的只是一个出口——没有它，
    "换模型省了多少、哪类失败最多、卡在哪一段"只能一张张翻 qc。算术只有 `metrics.py` 一处。
    这是运维视角的数：制单员/录入员用不到，也不该看到别人的账单，所以挂管理员门禁。"""
    for d in (date_from, date_to):
        if d and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            raise HTTPException(400, f"日期要写成 2026-09-30 这种四位日期：{d!r}")
    return {"ok": True, **metrics.collect(store.load_qc(), date_from, date_to)}


_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

@app.get("/stats/export")
def stats_export(date_from: str = Query("", alias="from"), date_to: str = Query("", alias="to"),
                 _: dict = Depends(require_admin)):
    """把同一份统计导成 Excel（口径 / 每张票 / 逐列 三张表），与页面严格同源。

    与 `/stats` 同一道门（仅管理员）：导出是同一份数的另一种拿法，只把入口藏起来不算门禁。"""
    rows = stats.collect(date_from, date_to)
    data = stats.export_bytes(rows, stats.summary(rows))
    name = "解析正确率_" + (date_from or "全部") + ("_" + date_to if date_to else "") + ".xlsx"
    return Response(content=data, media_type=_XLSX_MIME,
                    headers={"Content-Disposition": 'attachment; filename="stats.xlsx"; '
                             'filename*=UTF-8\'\'' + quote(name)})


def _archive_dir(stem: str):
    """URL 里的 stem 只当单段目录名用：'..' / 带分隔符 / 空名一律不认，
    否则 ../../ 就能越过归档根去读服务器上任何目录。"""
    name = _safe_stem(stem)
    if name is None:
        return None
    d = config.ARCHIVE_DIR / name
    return d if d.is_dir() else None


_safe_stem = store.safe_stem
# 单段文件名的判定只留一份（真身在 store.safe_stem）：读转录/归档与写暂存文件用的是同一条规则。
# 写成模块级赋值而不是转调 store.safe_stem，是因为测试会把 server.store 整个换成桩——
# 输入校验不该跟着被换掉。


def _make_excel_preview(stem: str, xls: Path) -> Path:
    try:
        return store.save_preview(stem, xlsx2pdf.convert_excel_to_pdf(xls))
    except FileNotFoundError as e:
        raise HTTPException(415, f"服务器上没装 LibreOffice，电子单转不出可预览的 PDF：{e}")
    except Exception as e:
        raise HTTPException(415, f"电子单转 PDF 失败（{type(e).__name__}: {e}），请点「下载原件」在本地打开")


def _archive_files(d: Path) -> list:
    files = sorted(p for p in d.iterdir() if p.is_file() and p.name != "manifest.json")
    if not files:
        raise HTTPException(404, "归档目录里没有原件文件")
    return files


def _preview_file(stem: str, d: Path) -> Path:
    """/source 与 /render 共用的票面定位顺序：归档里浏览器能直接渲染的文件 →
    电子单转出的 PDF（缓存）→ 现转现缓存。转不出来抛 HTTPException，说清原因。"""
    files = _archive_files(d)
    for p in files:
        if p.suffix.lower() in _PREVIEW_TYPES:
            return p
    conv = config.PREVIEW_DIR / f"{stem}.pdf"
    if not conv.exists():
        xls = next((p for p in files if p.suffix.lower() in _EXCEL_TYPES), None)
        if xls is not None:
            # 老票（本功能上线前传的）归档里只有 xlsx、没有转出的 PDF：现转现看，转完缓存下来
            conv = _make_excel_preview(stem, xls)
    if conv.exists():
        return conv
    raise HTTPException(415, "这张票的原件格式浏览器预览不了（TIFF），请点「下载原件」在本地打开")


@app.get("/ticket/{stem}")
def ticket_result(stem: str, _: None = Depends(require_web)):
    """按 stem 重开一张已归档的分单：L2 原文口径 + L3 航空口径 + 质检记录 + 本机提交条目。
    落盘的东西原样交回（`qc` 一个字不改——那是"这次解析抄得对不对"的凭据，解析质量分读它）；
    唯一现算的是 `flags_now`：**已经发出去的票，红旗按台账那份公司终值重算**
    （2026-10-16 用户："已经提交的分单和主单为什么我第二次点进去还是提示我有红旗"）。
    判据不另起一处——就是提交门那一个 `jobs.recompute_flags`，只是 `transcript` 传 None：
    逐字保真查的是模型这次抄得对不对，人改过的值不该拿它回查。"""
    name = _safe_stem(stem)
    if name is None:
        raise HTTPException(400, "stem 不合法（不接受带路径的名字）")
    raw_p, air_p = config.OUTPUT_RAW_DIR / f"{name}.json", config.OUTPUT_AIR_DIR / f"{name}.json"
    if not (raw_p.exists() and air_p.exists()):
        raise HTTPException(404, f"本机没有 {name} 的解析结果（output/raw 或 output/air 缺文件）")
    qc_p = config.OUTPUT_QC_DIR / f"{name}.json"
    qc = json.loads(qc_p.read_text(encoding="utf-8")) if qc_p.exists() else {}
    raw = json.loads(raw_p.read_text(encoding="utf-8"))
    led = store.ledger().get(name)
    sent = (led or {}).get("air_sent")
    now = jobs.recompute_flags(raw, sent, None) if isinstance(sent, dict) and sent else None
    return {"stem": name, "filename": qc.get("source_name") or name,
            "channel": qc.get("channel"), "elapsed": qc.get("elapsed"),
            "raw": raw,
            "air": json.loads(air_p.read_text(encoding="utf-8")),
            "qc": qc, "submitted": led,
            "flags_now": now, "flags_now_at": (led or {}).get("submitted_at") or ""}


@app.get("/staged/{stem}")
def staged_record(stem: str, _: None = Depends(require_web)):
    """读一份暂存：模型原样 + 最新人工值 + 每次谁改了哪几列。

    `/ticket/{stem}` 给的是模型口径（air/raw/qc），看不到人工值；录入员打开别人暂存的票
    走这里（需求二"由录入员核对后再发公司"的读侧）。没暂存过要说清为什么没有，
    不能让人以为票丢了。"""
    name = _safe_stem(stem)
    if name is None:
        raise HTTPException(400, "stem 不合法（不接受带路径的名字）")
    rec = store.load_staged(name)
    if rec is None:
        raise HTTPException(404, f"{name} 还没有暂存记录：票主没点过「暂存」，或暂存文件被清理过")
    return {"ok": True, **rec, "reviewed_by_inputter": store.reviewed_by_inputter(rec)}


@app.get("/source/{stem}")
def source(stem: str, raw: bool = Query(False, description="true=发原件本身（Excel/TIFF 浏览器渲染不了时下载用）"),
           _: None = Depends(require_web)):
    """审核台回看票面用的原件。电子单的原件是 xlsx，浏览器渲染不了，
    改发 LibreOffice 转出的 PDF——也就是模型实际看过的那张，对账口径一致。"""
    d = _archive_dir(stem)
    if d is None:
        raise HTTPException(404, "归档里没有这张票的原件：它是在「上传一律落盘」上线之前传的，"
                                 "或归档目录被清理过")
    if raw:
        original = _archive_files(d)[0]
        return FileResponse(original, media_type="application/octet-stream", filename=original.name)
    p = _preview_file(stem, d)
    return FileResponse(p, media_type=_PREVIEW_TYPES[p.suffix.lower()])


@app.get("/mawb/source/{mawb}")
def mawb_source(mawb: str, raw: bool = Query(False, description="true=发原件本身（电子单下载用）"),
                _: None = Depends(require_web)):
    """主单(MAWB)原件预览：/source 的分单侧对应物，任何登录角色可开（检索已并入制单台）。

    原件从 output/mawb_source/<归一化主单号>/ 取，格式处理与分单侧同一套（浏览器能渲染的直接发、
    电子单现转 PDF 缓存）。主单原件接口待 IT——真接口到位后由 company_api 缓存进同一目录即可，
    这条路由与前端都不用动。预览缓存的 stem 加 mawb- 前缀，避免与分单文件名撞车。"""
    key = store.mawb_source_key(mawb)
    if not key:
        raise HTTPException(400, "主单号不合法：只认 6-20 位字母数字（连字符/空格会自动忽略）")
    d = store.mawb_source_dir(mawb)
    if d is None:
        raise HTTPException(404, f"本机还没有这张主单的原件（公司主单原件接口待 IT；可先放到 output/mawb_source/{key}/）")
    if raw:
        original = _archive_files(d)[0]
        return FileResponse(original, media_type="application/octet-stream", filename=original.name)
    p = _preview_file("mawb-" + key, d)
    return FileResponse(p, media_type=_PREVIEW_TYPES[p.suffix.lower()])


@app.get("/layout/{stem}")
def layout(stem: str, _: None = Depends(require_web)):
    """L1 逐字转录（每行 text+bbox）。审核台「点字段定位票面行」拿它把字段值对回票面。
    读的是转录目录而非归档：未勾落盘的票没有转录文件，一律 404 说清原因。"""
    name = _safe_stem(stem)
    if name is None:
        raise HTTPException(404, "stem 不是合法的单段名称")
    f = config.TRANSCRIPT_DIR / f"{name}.json"
    if not f.is_file():
        raise HTTPException(404, "没有这张票的 L1 转录：它是在转录留档上线之前解析的，"
                                 "或转录文件被清理过——重新上传一次这张票就有")
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"转录文件读坏了（{type(e).__name__}: {e}），可删掉 output/transcript/{name}.json 后重跑该票")
    pages = max([int(l.get("page") or 1) for l in (data.get("lines") or []) if isinstance(l, dict)] or [1])
    data.pop("_l1", None)          # 指纹是给复用判据用的，不进这个接口的数据面
    return {**data, "pages": pages}


# 定位模式底图的页图缓存：同一张票反复点字段不该每次都重渲染；
# 键含 scale，FIFO 挡 8 张（A4 @2x 一张 PNG 约 0.5-2MB，最坏十几 MB，够用也够小）。
# 这份 dict 会被线程池与队列 worker 同时碰，所以读写都在 _RENDER_LOCK 里——淘汰那一步
# `pop(next(iter(...)))` 在无锁下能撞上"字典大小在迭代中变了"。渲染本身留在锁外：
# 一张四页票每页 100-300ms，全局锁住渲染就等于把并发核对压成串行。
_RENDER_CACHE: dict = {}
_RENDER_LOCK = threading.Lock()
_RENDER_SCALE = (1, 3)


def _drop_render_cache(key: str) -> None:
    """彻底删除一张票时把它的页图从缓存里摘掉。不清这一步，删完还能从内存缓存里
    把已经"人间蒸发"的票面翻出来——那张 PNG 正是这整件事要抹掉的东西。"""
    with _RENDER_LOCK:
        for k in [k for k in _RENDER_CACHE if k[0] == key]:
            _RENDER_CACHE.pop(k, None)


purge.CACHE_HOOKS.append(_drop_render_cache)


class PurgeItems(BaseModel):
    items: list = []


class PurgeDays(BaseModel):
    days: "str | int" = ""          # 界面填的是文本，接口也可能收到数字：两种都接，非法值统一 400


@app.get("/purge/config")
def purge_config_get(_: None = Depends(require_admin)):
    return {"days": purge.retention(), "range": list(config.PURGE_DAYS_RANGE)}


@app.post("/purge/config")
def purge_config_set(body: PurgeDays, _: None = Depends(require_admin)):
    """安静期只存在这一份。执行删除时服务端重读它，**不看请求里带的天数**——
    不然带一个 days=0 就能删掉昨天刚被碰过的票，安静期成了摆设。"""
    got = purge.set_retention(body.days)
    if got is None:
        raise HTTPException(400, "安静期要填 %d-%d 之间的整数天" % config.PURGE_DAYS_RANGE)
    return {"days": got}


@app.get("/purge/candidates")
def purge_candidates(days: int = Query(0, ge=0, description="0=用服务端那份安静期；填数字只做假设预览"),
                     brief: int = Query(0, ge=0, le=1, description="1=只数条数，不去 stat 归档目录"),
                     all: int = Query(0, ge=0, le=1, description="1=连没到期的也给出（面板那份全量视图）"),
                     _: None = Depends(require_admin)):
    """面板与顶栏徽标同一份数据。这里的 days 只能用来"如果改成 N 天会捞出哪些"，
    它不授权任何删除——POST 那边会按服务端安静期重算一遍。
    `all=1` 只给面板看：徽标与 run_purge.py --auto 走的都是默认那一份（只含到期的），
    所以那份另给一格 aged_count，免得有人拿全量条数去当"有票到期了"。
    last_run 顺带一起给：定时器是另一个进程，不告诉界面"上次跑成什么样"就是静默失败。"""
    d = purge.retention() if not days else days
    items = purge.candidates(days=d, with_bytes=not brief, only_aged=not all)
    return {"ok": True, "days": purge.retention(), "preview_days": d,
            "items": [{k: v for k, v in i.items()
                       if k not in ("files", "dirs", "sources", "drop")} for i in items],
            "aged_count": len([i for i in items if i.get("aged")]),
            "total_bytes": sum(i["bytes"] or 0 for i in items),
            "last_run": purge.read_run_state()}


@app.post("/purge")
def purge_run(body: PurgeItems, request: Request, _: None = Depends(require_admin)):
    """人工那一路：逐张执行、逐张报成败，一批里有一张卡住不许其余的假报已删。
    不在名册里的项（没满安静期 / 票早就不在了 / 主单名下还有分单）由 purge 自己拒，
    不靠前端没勾。自动那一路走 run_purge.py，不经这个口。"""
    who = auth.session_user(request) or {}
    results = purge.purge(body.items or [], by=str(who.get("name") or ""))
    return {"results": results, "days": purge.retention()}


@app.get("/render/{stem}")
def render(stem: str, request: Request,
           page: int = Query(1, ge=1, description="1 起的页码"),
           scale: float = Query(2, description="渲染倍率（≈72dpi 的倍数），服务端钳到 1-3"),
           _: None = Depends(require_web)):
    """把归档票面按页转成 PNG：浏览器内嵌 PDF 阅读器没法按区域高亮/放大，
    定位模式只能拿页图当底图再叠 bbox。TIFF 浏览器渲染不了，pymupdf 却读得动，
    归档了就能定位。

    页图带 ETag 且 no-cache：定位模式里换页、点字段、来回切票都会反复取同一张 PNG，
    而归档原件是不可变的——字节没变就该 304，不该重传（2026-10-13 效率审查 P0-4）。
    不用长 max-age：删票与重解析都发生在服务端，得让浏览器每次来问一次才立刻看到新的。"""
    d = _archive_dir(stem)
    if d is None:
        raise HTTPException(404, "归档里没有这张票的原件，无法按页定位：它是在「上传一律落盘」"
                                 "上线之前传的，或归档目录被清理过")
    try:
        p = _preview_file(stem, d)
    except HTTPException as e:
        if e.status_code != 415:
            raise
        p = _archive_files(d)[0]      # TIFF 之类：走 pymupdf 直接解码，比报错有用
    if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp"}:
        return FileResponse(p, media_type=_PREVIEW_TYPES[p.suffix.lower()])   # 底图本来就用原图
    scale = min(max(float(scale), _RENDER_SCALE[0]), _RENDER_SCALE[1])
    key = (stem, page, scale)
    with _RENDER_LOCK:
        hit = _RENDER_CACHE.get(key)
    if hit is None:
        import pymupdf
        try:
            doc = pymupdf.open(str(p))
            try:
                total = doc.page_count
                if total < 1:
                    raise HTTPException(415, f"{p.name} 里没有可渲染的页，请点「下载原件」在本地打开")
                pg = doc[min(page, total) - 1]
                png = pg.get_pixmap(matrix=pymupdf.Matrix(scale, scale)).tobytes("png")
            finally:
                doc.close()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(415, f"票面按页渲染失败（{type(e).__name__}: {e}），请点「下载原件」在本地打开")
        hit = (png, total, '"' + hashlib.sha1(png).hexdigest()[:16] + '"')
        with _RENDER_LOCK:
            if len(_RENDER_CACHE) >= 32:
                _RENDER_CACHE.pop(next(iter(_RENDER_CACHE)))
            _RENDER_CACHE[key] = hit
    png, total, etag = hit
    head = {"X-Ticket-Pages": str(total or 1), "ETag": etag, "Cache-Control": "no-cache"}
    if etag in [x.strip() for x in request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=304, headers=head)
    return Response(png, media_type="image/png", headers=head)


def main():
    config.fix_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="默认只监听本机；对局域网开放用 --host 0.0.0.0（届时请先改掉管理员默认口令）")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    if auth.ensure_seed():
        print(f"[账号] 已初始化 users.json：管理员 {auth.SEED_ADMIN_NAME}（默认口令 {auth.SEED_ADMIN_PASSWORD}，"
              f"请登录后立即在「账号管理」改密）；制单员 {len(auth.REVIEWERS)} 位待管理员下发口令")
    added = auth.backfill_seed()
    if added:
        print(f"[账号] 已向现有 users.json 补录默认账号：{'、'.join(added)}"
              f"（口令为空，请在「账号管理」下发；录入员在 /inputter 检索台使用）")
    public = args.host not in ("127.0.0.1", "localhost", "::1")
    if public:
        print(f"[提示] 已开放到 {args.host}：登录是唯一门槛，请务必先改掉管理员默认口令 {auth.SEED_ADMIN_PASSWORD}。")
    print(f"HAWB 提取服务 http://{args.host}:{args.port}  （审核台 http://{args.host}:{args.port}/ 需登录；"
          f"/health 与 /models 免登录）")
    print(f"提取模型 {config.VLM_MODEL}（{config.VLM_MODEL_CHOICE}）"
          + ("" if config.MODEL_VISION else " · 纯文本：扫描件会失败，电子单/文字层 PDF 可用"))
    # nginx 在本机反代，不开 proxy_headers 的话会话日志里全是 127.0.0.1，出了事找不到是谁在试。
    # 只信本机转发来的 X-Forwarded-*（allow_ips 就写回环），别让外面的人能伪造身份 IP。
    uvicorn.run(app, host=args.host, port=args.port,
              proxy_headers=True, forwarded_allow_ips="127.0.0.1")


if __name__ == "__main__":
    main()
