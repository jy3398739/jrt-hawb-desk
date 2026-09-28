# -*- coding: utf-8 -*-
"""HTTP 接口服务（形态③）。业务系统上传分单文件，实时返回两遍 39 字段 JSON。
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
  POST /extract            需登录会话；multipart 字段 file=分单文件；?save=true 同时按原名落盘
  POST /submit             需登录会话；body={"tickets":[...] }；制单员提交=回传公司(现 mock)+进索引的唯一出口，缺主/分单号拒绝
  GET  /company/mawb       需登录会话(任意角色，2026-09-26 并入制单台)；?mawb=主单号 → 检索主单+名下分单(带本机原件 stem)，现走 mock
  GET  /webplan/{stem}     需登录会话(任意角色)；CCSP 填表计划 dry-run（只读归档，不碰平台）
  GET  /results            已落盘条数（需登录会话）
  GET  /source/{stem}      回看票面原件，供审核台预览（需登录会话）；?raw=true 发原件本身供下载
  GET  /mawb/source/{mawb} 回看主单原件（需登录会话，任意角色）；原件在 output/mawb_source/<归一化主单号>/
  GET  /layout/{stem}      L1 逐字转录（含每行 bbox），审核台「点字段定位票面行」用（需登录会话）
  GET  /render/{stem}      把归档票面按页转成 PNG（?page=&scale=），定位模式的底图（需登录会话）
"""
import argparse, json, re, shutil, tempfile, time
from pathlib import Path

import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

import auth
import company_api
import config
import master_pipeline
import store
import web_bridge
import xlsx2pdf
from jobs import handle_file

app = FastAPI(title="HAWB 分单识别服务", version=config.APP_VERSION)
WEB_DIR = Path(__file__).resolve().parent / "web"
CHUNK = 1 << 20
# 进程启动时刻：用来发现「源码改了但服务没重启」。审核台曾因此一直拿不到票面原件
_BOOT = time.time()
_PKG_DIR = Path(__file__).resolve().parent
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


class LoginBody(BaseModel):
    name: str = ""
    password: str = ""


class UserBody(BaseModel):
    name: str = ""
    password: str = ""
    role: str = ""


@app.post("/login")
def login(body: LoginBody, response: Response):
    """登录审核台：校验口令 → 写签名会话 Cookie。口令只在本机 users.json 里存哈希。"""
    name = str(body.name or "").strip()
    if not name or not body.password:
        raise HTTPException(400, "请输入账号和密码")
    result = auth.authenticate(name, body.password)
    if not result:
        raise HTTPException(401, "账号或密码不正确（子账号需先由管理员设置密码）")
    response.set_cookie(auth.COOKIE, result["token"], max_age=int(auth.SESSION_HOURS * 3600),
                        httponly=True, samesite="lax", path=auth.COOKIE_PATH)
    return {"ok": True, "user": {"name": result["name"], "role": result["role"]}}


@app.post("/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE, path=auth.COOKIE_PATH)
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    """前端据此决定显登录页还是进审核台，并按角色隐藏模型下拉/账号管理。"""
    user = auth.session_user(request)
    return {"authenticated": bool(user), "user": user}


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

@app.get("/inputter")
def inputter_desk():
    """录入员检索台已并入制单台（2026-09-26 用户定案：主单检索/主单原件/填表计划都在 / 一页）。
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
    return {"ok": True, "version": config.APP_VERSION,
            "model": config.VLM_MODEL, "model_choice": config.VLM_MODEL_CHOICE,
            "vision": config.MODEL_VISION, "key_configured": config.vlm_api_key_configured(),
            "stale_files": _stale_files()}


# 模型 id 允许的字符：字母数字与 . _ - / :（org/Model、版本号）。挡住换行/等号/引号——
# 选择会写进 .env，值是拼进去的，混进换行就能伪造出别的配置行。
_MODEL_ID_OK = re.compile(r"^[A-Za-z0-9._:/-]{1,120}$")


class ModelChoice(BaseModel):
    model: str = ""


def _company_submit(payload: dict) -> dict:
    """把一张已核对的分单 JSON 回传公司系统，回执写进提交台账。

    真实现收敛在 company_api.submit_order：契约待 IT，现在一律 mock、不外发任何请求，
    只回带 mode='mock' 的回执，把"提交态 + 索引"这条本地链路先跑通。真接口到位后改
    company_api 一处即可，这里的调用与上面 /submit 的门禁都不动。"""
    return company_api.submit_order(payload)


@app.get("/company/mawb")
def company_mawb(background: BackgroundTasks,
                 mawb: str = Query(..., description="主单号（可带连字符/空格，检索前归一化）"),
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
        res["master"] = master_pipeline.read_master(mawb) or {"state": "parsing", "mawb": mawb}
        background.add_task(master_pipeline.ensure, mawb, order)
    else:
        # 没资料就没得解析：就地记下 failed，让页面能说清"为什么没有结果"而不是空着
        res["master"] = master_pipeline.ensure(mawb, order)
    return res


@app.get("/master/{mawb}")
def master_result(mawb: str, _: None = Depends(require_web)):
    """主单解析记录（state/ams 36 列/qc 红旗与保真/L1 原文）。没解析过就 404。"""
    rec = master_pipeline.read_master(mawb)
    if rec is None:
        raise HTTPException(404, "这条主单还没有解析结果（先在审核台检索一次主单号）")
    return rec


class MasterSubmitBody(BaseModel):
    mawb: str = ""
    reviewer: str = ""
    ams: dict = {}
    acked_flags: list = []


@app.post("/master/submit")
def master_submit(body: MasterSubmitBody, _: None = Depends(require_web)):
    """把人工核对过的主单回传公司（j9 mawb2）。这是主单侧唯一对外写出口，只有人工点才发。

    门与错误码与分单同构：红旗未清/未署名 → 400（红旗在服务端按当前值重算，前端藏旗无效）；
    公司侧 SEND_STATUS 非 0/2（已发送锁定）→ 409 且不发写请求；接口不通/没配 → 502，绝不写台账。"""
    try:
        return master_pipeline.submit_master(body.model_dump())
    except master_pipeline.MasterFlagged as e:
        raise HTTPException(400, str(e))
    except company_api.CompanyLocked as e:
        raise HTTPException(409, str(e))
    except (company_api.CompanyApiError, company_api.CompanyNotConfigured) as e:
        raise HTTPException(502, str(e))


@app.get("/webplan/{stem}")
def webplan(stem: str, _: None = Depends(require_web)):
    """V2 dry-run：CCSP 填表计划。读归档的 L3 航空口径（+L2 原文补城市名），按 sites/ccsp.json
    的映射算出"将往平台 editHAWBForm 哪些控件填什么值 + 需人工核对清单"。
    纯只读：不起浏览器、不向平台发任何请求；真填表/提交是后续里程碑且永远人工点。"""
    name = _safe_stem(stem)
    air_p = config.OUTPUT_AIR_DIR / f"{name}.json" if name else None
    if not air_p or not air_p.exists():
        raise HTTPException(404, f"本机没有 {stem} 的航空口径结果（output/air/ 缺文件）")
    air = json.loads(air_p.read_text(encoding="utf-8"))
    raw = {}
    raw_p = config.OUTPUT_RAW_DIR / f"{name}.json"
    if raw_p.exists():
        raw = json.loads(raw_p.read_text(encoding="utf-8"))
    return {"stem": name, "hawb": str(air.get("HAWB_NO", "") or ""),
            "plan": web_bridge.build_fill_plan(air, raw=raw)}


class SubmitBody(BaseModel):
    tickets: list = []
    submitted_at: str = ""
    client: str = ""


@app.post("/submit")
def submit(body: SubmitBody, _: None = Depends(require_web)):
    """制单员提交：这是"回传公司 + 进索引"的唯一出口。

    门禁以服务端为准、不信前端（前端那道门只是体验）：一张分单必须有可归一化的主单号与分单号，
    否则拒绝——因为 (主单号|分单号) 复合键是"录入员日后能否按号打开本机原件"的唯一连接键，
    缺号的票进了台账就等于往索引里塞坏数据。缺号≠处理失败：这里只挡提交，票的 L2/L3 早已落盘，
    制单员补号后重新提交即可。机批/监控/站点不经过这里，所以它们落的脏 output 永远进不了索引。"""
    tickets = body.tickets if isinstance(body.tickets, list) else []
    if not tickets:
        raise HTTPException(400, "没有待提交的票据")
    results = []
    for tk in tickets:
        if not isinstance(tk, dict):
            raise HTTPException(400, "票据格式不正确：tickets 里每一项都应是对象")
        rec = tk.get("air_reviewed") if isinstance(tk.get("air_reviewed"), dict) else {}
        mawb = str(rec.get("MAWB_NO", "") or "").strip()
        hawb = str(rec.get("HAWB_NO", "") or "").strip()
        stem = str(tk.get("stem") or tk.get("filename") or "").strip()
        if not store.norm_no(mawb) or not store.norm_no(hawb):
            raise HTTPException(400, f"主单号/分单号不能为空（补齐后才能回传公司并进索引）：{stem or tk.get('filename', '')}")
        try:
            receipt = _company_submit({"mawb": mawb, "hawb": hawb, "stem": stem, "air": rec})
        except company_api.CompanyLocked as e:
            raise HTTPException(409, str(e))
        except (company_api.CompanyApiError, company_api.CompanyNotConfigured) as e:
            raise HTTPException(502, f"回传公司失败：{e}")
        acked = tk.get("acked_flags") if isinstance(tk.get("acked_flags"), list) else []
        entry = store.mark_submitted(stem, mawb, hawb,
                                     str(tk.get("reviewer", "") or ""), receipt, acked_flags=acked)
        results.append({"stem": stem, "submitted": True, "key": entry["key"], "mode": receipt.get("mode")})
    return {"ok": True, "results": results}


@app.get("/models")
def models():
    """可选模型清单。免登录：审核台要先填出下拉（可能还没登录）。"""
    return {"current": config.VLM_MODEL_CHOICE, "effective": config.VLM_MODEL,
            "vision": config.MODEL_VISION,
            "presets": [{"key": k, "model": v["model"], "vision": bool(v["vision"]), "label": v["label"]}
                        for k, v in config.MODEL_PRESETS.items()]}


@app.post("/model")
def switch_model(body: ModelChoice, _: None = Depends(require_admin)):
    """切换提取用的模型：本进程立即生效，并写回 .env 供重启后与 CLI/批处理沿用。
    换模型直接改变提取结果——管理员专属（管理员登录会话）。"""
    choice = body.model.strip()
    if not choice:
        raise HTTPException(400, "model 不能为空：填预设键（如 intern-s2-official）或 org/模型 id")
    if not _MODEL_ID_OK.match(choice):
        raise HTTPException(400, f"模型名不合法：{choice!r}（只允许字母数字与 . _ - / :）")
    info = config.set_model(choice)
    try:
        config.persist_model_choice(info["choice"])
    except OSError as e:
        raise HTTPException(500, f"模型已在本进程切换，但写 .env 失败（重启后仍是旧模型）：{e}")
    return {"ok": True, **info}


@app.post("/extract")
async def extract(file: UploadFile = File(...), save: bool = Query(False, description="是否按原文件名落盘"),
                  _: None = Depends(require_web)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in config.ALL_EXTS:
        raise HTTPException(400, f"不支持的文件类型 {suffix}，支持 {sorted(config.ALL_EXTS)}")

    # PDF/XLSX 需要文件路径，且落盘名要用票面原名（用 NamedTemporaryFile 的随机名会丢出处）
    tmp_dir = Path(tempfile.mkdtemp(prefix="hawb_upload_"))
    path = tmp_dir / safe_name(file.filename or "", suffix)
    try:
        await _save_upload(file, path)
        # 提取是同步网络调用（7-20s）：不丢进线程池就会占死事件循环，期间 /health 都无人响应
        r = await run_in_threadpool(handle_file, path, save)
        if save and not r["error"]:
            await run_in_threadpool(store.rebuild_summary)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if r["error"]:
        return JSONResponse(status_code=500,
                            content={"ok": False, "error": r["error"], "elapsed": r["elapsed"],
                                     "filename": file.filename, "stem": r["stem"], "qc": r["qc"]})
    return {
        "ok": True,
        "filename": file.filename,
        "stem": r["stem"],
        "channel": r["channel"],
        "elapsed": r["elapsed"],
        "qc": r["qc"],          # needs_review/flags/fidelity：业务方按 needs_review 决定是否人工核票
        "raw": r["raw"],
        "air": r["air"],
        "transcript": r["transcript"],   # L1 逐行转录+bbox：审核台点字段定位票面靠它
    }


@app.get("/results")
def results(_: None = Depends(require_web)):
    raw_dir, air_dir = config.OUTPUT_RAW_DIR, config.OUTPUT_AIR_DIR
    return {
        "raw_count": len(list(raw_dir.glob("*.json"))) - (1 if (raw_dir / "all_hawbs.json").exists() else 0),
        "air_count": len(list(air_dir.glob("*.json"))) - (1 if (air_dir / "all_hawbs_air.json").exists() else 0),
        "raw_dir": str(raw_dir),
        "air_dir": str(air_dir),
    }


def _archive_dir(stem: str):
    """URL 里的 stem 只当单段目录名用：'..' / 带分隔符 / 空名一律不认，
    否则 ../../ 就能越过归档根去读服务器上任何目录。"""
    name = _safe_stem(stem)
    if name is None:
        return None
    d = config.ARCHIVE_DIR / name
    return d if d.is_dir() else None


def _safe_stem(stem: str):
    """单段文件名的那半道闸门：/layout 读的是转录目录不是归档目录，没法靠
    「目录存在」兜底，必须先剥掉路径与 '..' 再拼文件名。"""
    name = str(stem or "").replace("\\", "/").split("/")[-1]
    if not name or name in (".", "..") or name != stem:
        return None
    return name


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


@app.get("/source/{stem}")
def source(stem: str, raw: bool = Query(False, description="true=发原件本身（Excel/TIFF 浏览器渲染不了时下载用）"),
           _: None = Depends(require_web)):
    """审核台回看票面用的原件。电子单的原件是 xlsx，浏览器渲染不了，
    改发 LibreOffice 转出的 PDF——也就是模型实际看过的那张，对账口径一致。"""
    d = _archive_dir(stem)
    if d is None:
        raise HTTPException(404, "归档里没有这张票的原件：上传时没勾「落盘留档」，或归档已被清理")
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
        raise HTTPException(404, "没有这张票的 L1 转录：上传时未勾「落盘留档」，或转录早于本功能")
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"转录文件读坏了（{type(e).__name__}: {e}），可删掉 output/transcript/{name}.json 后重跑该票")
    pages = max([int(l.get("page") or 1) for l in (data.get("lines") or []) if isinstance(l, dict)] or [1])
    return {**data, "pages": pages}


# 定位模式底图的页图缓存：同一张票反复点字段不该每次都重渲染；
# 键含 scale，FIFO 挡 8 张（A4 @2x 一张 PNG 约 0.5-2MB，最坏十几 MB，够用也够小）。
_RENDER_CACHE: dict = {}
_RENDER_SCALE = (1, 3)


@app.get("/render/{stem}")
def render(stem: str, page: int = Query(1, ge=1, description="1 起的页码"),
           scale: float = Query(2, description="渲染倍率（≈72dpi 的倍数），服务端钳到 1-3"),
           _: None = Depends(require_web)):
    """把归档票面按页转成 PNG：浏览器内嵌 PDF 阅读器没法按区域高亮/放大，
    定位模式只能拿页图当底图再叠 bbox。TIFF 浏览器渲染不了，pymupdf 却读得动，
    归档了就能定位。"""
    d = _archive_dir(stem)
    if d is None:
        raise HTTPException(404, "归档里没有这张票的原件：上传时没勾「落盘留档」，无法按页定位")
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
    hit = _RENDER_CACHE.get(key)
    if hit is not None:
        png, total = hit
    else:
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
        if len(_RENDER_CACHE) >= 8:
            _RENDER_CACHE.pop(next(iter(_RENDER_CACHE)))
        _RENDER_CACHE[key] = (png, total)
    return Response(png, media_type="image/png", headers={"X-Ticket-Pages": str(total or 1)})


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
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
