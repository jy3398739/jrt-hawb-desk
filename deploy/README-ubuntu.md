# 在 Ubuntu 服务器上部署「主分单审核台」

现状：Ubuntu 24.04.4 / Python 3.12.3，主机 `efreight-vm`，服务名 `hawb-desk`，
只监听 `127.0.0.1:8020`，公网经 nginx 反代到 `https://<服务器IP>/hawb/`。
本文最后核对：2026-10-01。

## 0. 这套系统在做什么

票面（PDF / 图片 / Excel 电子单）→ L0 原件归档 → L1 逐字转录 → L2 模型提取 40 列
→ L3 航空口径归一（国家两位码 / 城市三字码 / 电话归一）→ 质检红旗 → 审核台人工核对
→ 回传公司 j9 AMS。

主单（MAWB）是并行的另一条链：公司 AMS 文本 → 36 列 → 人工确认 → `mawb2` 回传。
两条链**共用引擎、不共用字段表**，也不共用默认模型。

## 1. 系统依赖

```bash
sudo apt-get install -y python3.12-venv libreoffice fonts-noto-cjk
soffice --version          # 现状 24.2.7.2
```

- **LibreOffice 必需**：Excel 电子单先由它无头转成 PDF，再走和扫描件一样的链路。
  没装会明确报"服务器上没装 LibreOffice"，不会静默出空结果。
- **中文字体必装**（`fonts-noto-cjk`）：转 PDF 时中文缺字体会变方框，模型就读不到字了。

## 2. 代码与虚拟环境

```bash
mkdir -p ~/jrt-hawb/release            # 代码放在 ~/jrt-hawb/release/hawb_extractor
cd ~/jrt-hawb/release/hawb_extractor
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-server.lock.txt
```

服务器用**锁文件**（精确复现现在跑着的这套）。

开发机（Windows）从 2026-10-02 起也建一份同名 `.venv`、装同一把锁：
`python -m venv .venv` 后 `.venv\Scripts\python.exe -m pip install -r requirements-server.lock.txt`。
`./deploy/release.sh` 见到 `.venv` 就用它跑回归，所以本地跑的版本组合 = 线上跑的版本组合；
以前用全局 Python 时两边会漂（漂出过 pydantic 与 pydantic-core 对不上、本地一条回归都跑不起来）。
`.venv` 已在 `.gitignore` 里，不会进部署清单。`requirements.txt` 的 `>=` 范围留着给"只想快速装个能跑的"用。
双击启动的 `.bat` 仍走全局 Python——本机那份只是给人手动看结果，线上以服务为准。

## 3. 配置 `.env`（不进仓库、不进同步包）

```bash
cp .env.example .env && chmod 600 .env
```

- 必填：`QWEN_API_KEY`（公司百炼专属实例 token，`sk-ws-` 开头，默认渠道 `qwen38-flash-bailian`）。
  兜底渠道 `intern-s2-official` 要能切过去，才需要 `INTERNLM_API_KEY`。
- 主单链默认走 `qwen38-flash-bailian` → 需要 `QWEN_API_KEY`（公司百炼专属实例 token，`sk-ws-` 开头；向管理员索取，不是公共百炼控制台那把）
  （不想配就把 `MASTER_VLM_MODEL=` 留空，让它跟分单同渠道）。
- 真连公司系统才需要：`COMPANY_API_MODE=live`、`COMPANY_API_URL`、
  `COMPANY_MAWB_KEY`、`COMPANY_HAWB_KEY`（两把 key 不通用，找 IT 要）。

密钥只放 `.env`：绝不写进代码、前端或聊天记录。

## 4. 账号（上线第一件事）

首次启动自动在工作目录生成 `users.json`（权限 600）：管理员 `admin` / 口令 `admin123`，
外加 8 位制单员与 3 位录入员（这两类口令为空，等管理员下发）。

**这个入口是公网可达、并且能写公司系统的**，所以部署完立刻：登录 → 「账号管理」
改掉 admin 口令 → 给在岗制单员下发各自口令。

`users.json` 与 `.env` 永不同步、永不入库：`deploy/sync.sh` 的文件清单取 `git ls-files`，
天然不含它们（两者也都在 `.gitignore` 里）。

## 5. systemd

```bash
sudo cp deploy/hawb-desk.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now hawb-desk
curl -s http://127.0.0.1:8020/health        # 期望 "ok":true 且 "stale_files":[]
```

- 只绑 `127.0.0.1`，对外靠 nginx；别改成 `0.0.0.0`。
- `MemoryHigh=900M / MemoryMax=1300M`：这台机器还有别的服务（ccsp 峰值约 1.9G），
  内存闸撞满是整个服务被杀，不是只杀一张票——别为了跑得快把它调到整机水位。
- 端口选 8020：8000/8010 已被同机其他服务占用。

## 6. nginx（对外入口）

把 `deploy/nginx-hawb-location.conf` 里的 `location` 块贴进 443 的 `server{}` 内、
放在 `location / {` **之前**，然后：

```bash
sudo nginx -t && sudo systemctl reload nginx
```

要点：`proxy_pass http://127.0.0.1:8020/;` 结尾的斜杠会剥掉 `/hawb/` 前缀（页面按
`location.pathname` 自适应，前缀和根路径都能跑）；`proxy_read_timeout 900s` 是因为
解析是同步长调用，按默认 60s 会被掐。证书是自签的，浏览器告警一次点继续即可。

## 7. 验证部署（每次装完/升级后都跑）

```bash
.venv/bin/python -X utf8 tests/run_tests.py            # 全量回归，期望 0 失败（当前 287 项）
curl -s http://127.0.0.1:8020/health                   # 版本戳 + 队列水位
```

浏览器再走一遍真实流程：登录 → 拖一张真分单 → 逐字段核对 → **提交**
（提交是唯一对外写出口，一定要人工点，不要脚本代点）。

## 8. 日常运维

- **更新代码**：在开发机 `./deploy/sync.sh`（备份 → 推 → 逐文件哈希核对 → 服务器跑回归
  → 重启 → `/health` 自证）。只改 `css/js` 时加 `--no-restart`：静态资源按请求现读，
  重启反而会把正在核票的会话踢下线。
- **回滚**：每次部署前服务器会打 `~/.bak-<时间>-deploy.tar`。
  `tar xf ~/<包> -C ~/jrt-hawb/release/hawb_extractor && sudo systemctl restart hawb-desk`
- **看日志**：`journalctl -u hawb-desk -f`（logger 名 `hawb.desk`；提交失败会带单号落日志）。
- **看版本**：`/health` 的 `built_at` / `started_at` / `queue`；`stale_files` 非空
  = 磁盘上的 `.py` 比进程新，也就是推了代码没重启。
- **换模型**：管理员登录 → 顶栏下拉热切（写回 `.env`，重启与批处理都沿用）。
  分单与主单各一条链，两个下拉各切各的。
- **数据在哪**：`output/archive`（原件 + manifest）、`output/{raw,air,qc,transcript,preview}`、
  `output/master`（主单解析缓存）、`output/submitted.json` 与 `output/master_submitted.json`
  （提交台账）。**台账是本机一份，两台机器各记各账**，审核台「今日台账」页读的就是它。

## 9. 接手前要知道的边界

- **j9 是整表写回**：请求里没带的列会被写成 NULL。所以提交前一定先读回公司当前值
  （读不到就直接拒发，不硬写），并且只有 `SEND_STATUS` 为 0/2 的行允许更新（1 = 已发送锁定）。
- **限流**：IT 侧每把 key 10 次/秒，本机默认按 8 走并留两成余量。
- **并发**：同时解析 4 张、排队上限 200（`DESK_CONCURRENCY` / `DESK_QUEUE_MAX`；2026-10-08 实测后从 2 提到 4，
  2 槽吞吐 15 张/分钟、内存高水位才 142MB，闸 900M/1.3G），
  满了直接回 429 而不是把服务压死；单票最长等 30 分钟。
- **慢在模型**：单次调用超时 300s，超时类失败最多再试 1 次，其余错误按 2/8/20s 退避重试。
- 每次提交都按"接受即写 0"处理，所以界面上的「待公司发送」是**公司还没往航司发**，
  不代表本台没提交；本台提交记录在「今日台账」与提示里分开显示。
