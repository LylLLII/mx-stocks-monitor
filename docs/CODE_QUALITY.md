# 代码质量红线（mx-stocks-monitor）

> 本文档基于本项目**真实发生过的生产事故**提炼，是每次提交/PR 的必读红线。
> 配套自动守卫见 `tools/quality_gate.py`（CI + pre-commit 双重执行）。

---

## 一、团队当前 5 大技术短板（及对应红线）

### 红线 1：数据写入必须防 corruption
**事故**：观察池 CSV 被一夜清空（13 行归零）。根因是手动用 Excel 另存为给文件加了 UTF-8 BOM，
`load_master` 用 `utf-8` 打开导致首列变成 `\ufeff代码`，每行被跳过 → 池子空 → 写盘只剩表头。

- 读 CSV **一律 `utf-8-sig`**（自动吞 BOM）；写 CSV 用 `utf-8`（不带 BOM）。
- 写盘前**必须校验**：内存池为空但磁盘已有数据 → 拒绝覆盖（见 `screener_monitor._write_pool` 守卫）。
- **禁止用 Excel 直接编辑仓库内 CSV**。要改数据走脚本或 VS Code / Notepad++（存为 UTF-8 无 BOM）。

### 红线 2：外部 API 返回值必须校验有效性，并与业务时间窗耦合
**事故**：盘前 9:00–9:30 行情接口返回 `open=0、现价=昨收、无成交`，被误判成"停牌"，
且开盘涨跌幅被锁成 `-100%` 类错误值且永久无法修正。

- 凡依赖外部行情/接口返回值，**先校验值是否有效**（`>0`、非 `None`、非盘前占位），无效则跳过，不写入。
- 时间敏感逻辑必须和"交易时间窗"耦合：盘前/停牌态不抓取、不标记、不锁值，等开盘后（`>=9:30`）再处理。
- 任何"快照类"字段（如收盘涨跌幅、形态）**只在收盘后固化**，盘中只刷实时值，避免把盘中价误存成收盘。

### 红线 3：禁止用 `sys.exit(0)` 伪造成功
**事故**：云端缺 key 时 `ensure_api_key` 直接 `sys.exit(0)`，GitHub Actions 显示绿勾，
实际零产出（CSV 没写、没 push），且 `except Exception` 抓不住 `SystemExit`。

- 无头/云端/定时任务中**失败必须显式暴露**：`raise` 具体异常 / `sys.exit(1)` / 开 Issue 告警。
- **绝不允许**在无头路径用 `sys.exit(0)` 提前退出制造"成功"假象。本地交互分支（有人扫码）的 `sys.exit(0)` 允许，但附近须有 `_print_auth` / `input()` 等人工交互标记。
- `except Exception` 抓不到 `SystemExit` 和 `BaseException` —— 需要终止整个流程时用 `raise`，不要用 `sys.exit` 当 `return`。

### 红线 4：明确"单一写入方"，并发写同一文件须有策略
**事故**：云端与本地同时 `commit+push` 同一份 CSV，后 push 方被拒（non-fast-forward），
`_git` 只打印"跳过"不重试，改动丢失，甚至合并出冲突标记。

- 同一份共享数据文件**必须明确唯一写入方**（当前：本地任务为主力，云端为备份）。
- 若多端都可能写，**用 `git pull --rebase --autostash`** 或加写入锁，禁止 best-effort 静默跳过。
- 云端若只做镜像，用 `--no-push`（只 pull、不写）。

### 红线 5：无头/定时任务必须可观测，失败不能静默
**事故**：本地用 `pythonw.exe`（无窗口）跑，key 过期后只往 `monitor.log` 刷二维码链接，
屏幕上无任何报错，观察池悄悄停止增长，几天后才发现。

- 定时任务的所有失败路径都要落到**日志 + 显式告警**（Issue / 通知文件 / 退出码非零）。
- `monitor.log` 必须轮转，避免无限膨胀。
- 关键失败（如 key 过期、抓取全失败）要在任务下次运行时能被"看见"，不能只在深埋的日志里。

---

## 二、Code Review Checklist（PR 前逐项勾）

- [ ] 所有 CSV 读写：读 `utf-8-sig`、写 `utf-8`，且无 Excel 手改引入 BOM
- [ ] 写盘前是否有"池非空 / 行数校验"守卫（防清空）
- [ ] 外部 API 返回值是否校验有效性（`>0` / 非 `None` / 非盘前占位）
- [ ] 时间窗逻辑：盘前/停牌/非交易时段是否正确跳过，快照字段只在收盘后固化
- [ ] 无 `sys.exit(0)` 出现在无头/云端路径；失败路径显式 `raise` / 非零退出 / 告警
- [ ] `except` 是否误把 `SystemExit`/`BaseException` 当 `Exception` 吞掉
- [ ] 共享文件写入边界是否清晰（单一写入方 or rebase/锁）
- [ ] 定时/无头任务的失败是否可观测（日志 + 告警）
- [ ] 脚本通过 `python tools/quality_gate.py` 本地守卫

---

## 三、自动守卫（quality_gate.py）

`tools/quality_gate.py` 用纯标准库实现，检查 4 项，任一失败退出码 1：

1. **CSV BOM 检测**：`mx_stocks_screener/*.csv` 不得带 UTF-8 BOM（防观察池清空）。
2. **Python 语法**：仓库内所有 `.py`（跳过 `.venv`/`__pycache__`/`.git`/`.workbuddy`）`py_compile` 通过。
3. **主 CSV 可解析**：`观察池_累计.csv` 首列须为 `代码`，`utf-8-sig` 能正常读。
4. **无头环境 `sys.exit(0)` 反模式**：扫描 `screener_monitor.py`，若 `sys.exit(0)` 附近无
   `GITHUB_ACTIONS` 守卫 / `# 人工交互` / `_print_auth` / `input()` 标记则报错。

### 本地运行
```bash
python tools/quality_gate.py
```

### 接入 pre-commit（本地提交前自动拦）
```bash
cp scripts/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

### CI
`.github/workflows/quality-gate.yml` 在每次 `push` / `pull_request` 到 `main` 时自动执行。

---

## 四、长期机制（待建）
- **C. 定期 Code Review 节奏**：每次改动提 PR，由资深开发逐条审（结合本红线）。
- **D. mini 培训/结对**：围绕"文件编码防御""外部 API 边界校验""定时任务错误处理"做 1–2 次分享。
