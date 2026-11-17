# AGENTS.md — `kaggle-ext` 开发与无侵扰扩展工程规范

本项目为 [`Kaggle/kaggle-cli`](https://github.com/Kaggle/kaggle-cli)（`kaggle` CLI 及其底层 `kagglesdk`）提供**零文件修改、可热恢复、故障静默降级（Fail-Open）**的外部扩展层。

---

## 1. 核心设计铁律（Non-Negotiable Rules）

1. **零侵扰上游环境（Non-Intrusive Isolation）**
   - 严禁修改 `kaggle` 或 `kagglesdk` 安装目录下的任何现有文件。
   - 仅向目标 `kaggle` 工具虚拟环境的 `site-packages/` 写入唯一单行钩子文件 **`kaggle_ext.pth`**；扩展源码全部驻留在工具环境之外的 **`~/.kaggle-ext/`**（可通过 `KAGGLE_EXT_HOME` / `KAGGLE_EXT_SOURCE` 覆盖）。
   - 当用户运行 `uv tool upgrade kaggle` / `pip install -U kaggle` 重建虚拟环境时，不会产生任何文件冲突；最坏情况是新环境的 `.pth` 丢失，执行 `python3 ~/.kaggle-ext/install.py`（或 `kaggle ext install`）即可单命令恢复。
2. **严禁在解释器启动时 Eager Import `kaggle`**
   - `kaggle/__init__.py` 在模块导入顶层即实例化 `KaggleApi()` 并立即执行 `api.authenticate()`（涉及加载 ~500 KB 模块、创建配置目录、读取凭据及可能的 OAuth/Token 网络自省请求）。
   - **禁止**在 `kaggle_ext.pth` 或 `kaggle_ext_loader.py` 中直接 `import kaggle` 或 `import kaggle.cli`。
   - 必须通过零开销 `sys.meta_path` 查找器（`importlib.abc.MetaPathFinder`）按需延迟拦截 `kaggle.api.kaggle_api_extended` 与 `kaggle.cli`，并在 `exec_module` 入口第一时间还原 `spec.loader` 与 `module.__loader__`。
3. **Fail-Open 故障静默降级**
   - 任何扩展代码异常（缺文件、半更新状态、上游 API 签名变更）**绝不允许阻断原生 `kaggle` 命令执行**。
   - `.pth` 入口与 `kaggle_ext_loader.py` / `kaggle_ext.register()` 必须捕获扩展自身异常；默认保持静默或单行告警，仅在 `KAGGLE_EXT_DEBUG=1` 时向 `stderr` 输出完整 traceback 诊断信息。
4. **底座纯 Python 标准库（Stdlib-Only Core）**
   - `install.py`、`kaggle_ext_loader.py`、`kaggle_ext/inject.py`、`kaggle_ext/diagnostics.py`、`kaggle_ext/accounts.py`、`kaggle_ext/sessions.py`、`kaggle_ext/session_ws.py`、`kaggle_ext/skill_doc.py` 与 `kaggle_ext/skill_commands.py` 必须严格仅依赖 Python 3.11+ 标准库，确保任意系统 `python3` 均可完成安装、自检、账户解析、会话存储、RFC 6455 WebSocket、离线诊断与 Skill 文档检索。
5. **严禁泄露真实凭据**
   - 日志、报错、`kaggle ext doctor` / `kaggle session list/status` / `--json` 输出及测试断言中，严禁打印或返回真实 `KGAT_*` token、`KGRT_*` refresh token、`kaggle.json` 中的 `key` 字段、OAuth `access_token`，以及 Kaggle Jupyter Proxy URL 中内嵌的路径密钥（`/k/<id>/<seg>`）或查询参数（`?token=...`）。

---

## 2. 上游 `Kaggle/kaggle-cli` 内部机制与实测陷阱清单

已基于本地实装 `Kaggle CLI 2.2.5`（`kaggle` + `kagglesdk`）源码核实以下关键行为，新增任何 Hook 必须严格遵守：

1. **导入即鉴权（Eager Auth in `kaggle/__init__.py`）与双阶段 Hook 顺序**
   - `kaggle/__init__.py` 依次执行：
     1. `from kaggle.api.kaggle_api_extended import KaggleApi`
     2. `api = KaggleApi()`
     3. `try: api.authenticate() except (Exception, SystemExit): pass`
   - 若等到 `kaggle.cli` 加载完成后才补丁 `KaggleApi`，`kaggle/__init__.py` 已经执行过第一次 `api.authenticate()`（在未登录或离线环境下会触发 auth help 输出甚至网络超时）。
   - **解法**：`kaggle_ext_loader.py` 必须分两阶段拦截：
     - Stage 1（`kaggle.api.kaggle_api_extended`）：支持 `pre-api` hook（在类属性 `config_dir` 求值前介入）与 `post-api` hook（`patch_api_module`，在 `api = KaggleApi()` 执行前修补 `KaggleApi.authenticate` 与 `_command_allows_logged_out`）。
     - Stage 2（`kaggle.cli`）：在 `kaggle.cli` 模块体执行完定义 `main` 与 `Help` 后调用 `kaggle_ext.register(cli_mod)`。
2. **未登录双重报错与离线扩展命令短路**
   - `kaggle/__init__.py`（吞掉 `SystemExit`）和 `kaggle.cli:main()`（`if not api._authenticated: api.authenticate()`）会先后两次调用 `api.authenticate()`，未登录时输出两遍提示并 `exit(1)`。
   - **细节坑**：通过 `python -m kaggle ...` 启动时，`runpy._run_module_as_main` 会先导入 `kaggle/__init__.py` 后再把 `sys.argv[0]` 改写为 `.../kaggle/__main__.py`，导致首次鉴权期间 `sys.argv[0] == "-m"`、上游 `KaggleApi._invoked_as_cli()` 返回 `False`。
   - **解法**：`cli_hooks.py` 维护 `OFFLINE_EXTENSION_COMMANDS`（首版含 `"ext"`，现含 `"account"`/`"acct"`/`"skill"`/`"SKILL"`）与 `_is_kaggle_cli_process()`（同时识别 `KaggleApi._invoked_as_cli()` 与 `sys.argv[0] == "-m"` 且调用栈含 `runpy._run_module_as_main(mod_name in {"kaggle", "kaggle.__main__", "kaggle.cli"})`）。命中离线扩展命令且**未**进入显式 `accounts.account_context()`（`accounts.ACTIVE_ACCOUNT.get() is None`）时，`authenticate()` 直接标记 `self._authenticated = True` 并返回；从而既避免顶层 CLI 鉴权报错，又不影响 `kaggle account status` 内部显式对各账户执行真实 `api.authenticate()` 探测。
3. **`kaggle.cli:main()` 局部 `ArgumentParser`、`Help.kaggle_choices` 与 `**command_args` 转发**
   - `kaggle.cli:main()` 在函数体内部创建局部 `parser = argparse.ArgumentParser(...)` 与 `subparsers = parser.add_subparsers(..., dest="command")`，并将 `subparsers.choices = Help.kaggle_choices`（显式 `list`）。
   - 解析后，`main()` **仅删除** `vars(args)` 中的 `"func"`、`"command"`、`"disable_version_warning"`、`"debug"`，随后直接执行 **`args.func(**command_args)`**。
   - **强制约束**：
     - 扩展命令下所有嵌套 `add_subparsers(...)` **必须使用 `dest="command"`**（与上游 `parse_auth` / `parse_config` 一致），否则子命令字段会泄露给 `args.func(**command_args)` 导致 `TypeError`。
     - 任何新增顶层命令或别名必须同时加入 `Help.kaggle_choices`、`subparsers.choices`（若为 `list`）及 `Help.kaggle` 摘要文本。
     - 任何未来新增的全局 CLI 参数（如 `-a / --account`）必须在 `on_parsed_args(args)` 钩子中消费并通过 `delattr(args, ...)` 剥离，避免透传进上游 `api.*_cli(...)` 方法。
4. **`kaggle` 与 `kagglesdk` 的四处分散凭据路径与默认参数陷阱**
   - `KaggleApi.config_dir` / `kaggle.json`：在 `class KaggleApi` **类定义期**即按 `KAGGLE_CONFIG_DIR` -> `~/.kaggle` -> Linux 下若 `~/.kaggle` 不存在则回落 `$XDG_CONFIG_HOME/kaggle` 求值，并立即调用 `os.makedirs(config_dir)`；`cli_hooks.py` 通过描述符将 `config_dir`/`config` 动态路由至当前激活账户目录，并在 `__init__` 为每个实例隔离 `config_values`。
   - `kagglesdk.kaggle_env.get_access_token_from_env()`：优先读 `KAGGLE_API_TOKEN` 环境变量，否则硬编码读取 `~/.kaggle/access_token` 与 `~/.kaggle/access_token.txt`（**完全忽略 `KAGGLE_CONFIG_DIR`**），且被 `kagglesdk`、`kagglesdk.kaggle_http_client`、`kaggle.api.kaggle_api_extended` 三处 `from ... import` 绑定，须同步替换这四处引用。
   - `kagglesdk.kaggle_creds.KaggleCredentials`：`DEFAULT_CREDENTIALS_FILE = "~/.kaggle/credentials.json"` 不仅硬编码，且 `save(self, file_path=DEFAULT_CREDENTIALS_FILE)` 与 `delete(self, file_path=DEFAULT_CREDENTIALS_FILE)` 在函数定义期绑定了默认参数（须包装 `load`/`save`/`delete` 动态重写默认路径）；此外其 `access_token_has_expired()` 存在 `- timedelta(minutes=30)` 符号缺陷（把已过期的 OAuth access token 误判为在过期后 30 分钟内仍有效），`cli_hooks.py` 已修补为提前 5 分钟刷新并统一按 UTC 比较。
   - `kagglesdk.kaggle_http_client._get_apikey_creds()`：同样硬编码读取 `~/.kaggle/kaggle.json`，未修补时会在新账号未登录执行 OAuth 换取 Token 时泄露旧账号请求头。
5. **Linux Wayland 浏览器检测缺陷**
   - `KaggleOAuth._can_open_browser()` 在 Linux 上仅检查 `os.environ.get("DISPLAY")`，在纯 Wayland（仅设 `WAYLAND_DISPLAY`、未设 `DISPLAY`）桌面会误判为无浏览器环境。`cli_hooks.py` 已在 Linux + `WAYLAND_DISPLAY` + `xdg-open` 可用时自动修补为返回 `True`。
6. **交互式会话（`CreateKernelSession` / Jupyter Proxy）实测契约陷阱（Gate 0 核实）**
   - **Proto-RPC 路由与 OAuth 鉴权顺序**：`KaggleHttpClient` 发送 `POST /v1/{service}/{method}` 而非 `request.endpoint()`，且 `_try_fill_auth()` 不读 `credentials.json`；OAuth 账号必须先调用 `api.authenticate()` 填入短期 `token` 后再调用 `api.build_kaggle_client()`。
   - **`SaveKernel.ref` `/code/` 前缀**：`ApiSaveKernelResponse.ref` 返回 `"/code/<owner>/<slug>"`，空 `.ipynb` 单元格 `source` 须为 `str`（`""`）而非 `list`；传给其他 SDK 接口前必须剥离 `/code/`。
   - **早期锁存 `kernelSessionId`**：初始 `CreateKernelSession` 返回的 `Operation.metadata` 含 `{"kernelSessionId": <int>}`，但在 `GetOperation` 轮询与终态时 `metadata` 会变为 `null`；须在初始响应即刻提取并落盘 `"starting"` 状态，且最终 `response` 中的 `jupyterUrl`（`/k/<id>/<seg>/proxy`）与 `tokenizedJupyterUrl`（`?token=...`）均直接内嵌密钥，须严格脱敏。
   - **`GetKernelSessionStatus` 非交互式真值**：交互式会话运行期间 `GetKernelSessionStatus` 仍返回 `COMPLETE`（反映保存版本状态）；存活探活必须以代理 `GET {jupyter_url}/api/kernels`（`HTTP 200`）为准。
   - **全局 `-a` 与独立监控 `session_id`**：`extract_account_from_argv()` 在 `parse_args()` 前剥离所有 `-a`，故扩展子命令禁止绑定 `-a` 短参数（如 `--all-accounts`/`--all`）；只读监控（`watch`/`logs -f`）必须生成全新 `session_id` UUID，避免踢断并发的 `exec` 连接。

---

## 3. 仓库结构与 Hook 扩展契约

```text
kaggle-ext/
├── AGENTS.md                 # AI 协作规范、上游机理与路线图（本文件）
├── CLAUDE.md                 # @AGENTS.md 入口指针
├── README.md                 # 英文说明文档
├── README.zh-CN.md           # 中文说明文档
├── install.py                # 独立 stdlib 安装/卸载/状态/symlink 脚本
├── install.sh                # POSIX 一键引导安装脚本
├── kaggle_ext_loader.py      # .pth 入口：零开销 MetaPathFinder 延迟拦截器
├── kaggle_ext/
│   ├── __init__.py           # register(cli_mod) 入口与版本元数据
│   ├── inject.py             # .pth 生成、env/site-packages 探测、symlink 管理
│   ├── accounts.py           # 多账户注册表、非破坏性迁移、目录与 contextvars 隔离
│   ├── cli_hooks.py          # 双阶段 Hook 注册中心与 KaggleApi / kagglesdk 隔离拦截器
│   ├── diagnostics.py        # 离线、只读、脱敏的 Kaggle 凭据与环境体检
│   ├── account_commands.py   # `kaggle account` (`acct`) 多账户管理与多种登录方式
│   ├── sessions.py           # 多账户隔离 sessions.json 存储、锁、硬件映射与 URL 脱敏（纯 stdlib）
│   ├── session_ws.py         # 纯 stdlib Jupyter Proxy HTTP & RFC 6455 asyncio WebSocket 客户端
│   ├── session_client.py     # 私有空壳占位 Kernel、CreateKernelSession 轮询与取消编排
│   ├── session_commands.py   # `kaggle session` (`sess`) CLI 命令组
│   ├── skill_doc.py          # 离线 AI Agent Skill 文档合并、主题解析与导出引擎（纯 stdlib）
│   ├── skill_commands.py     # `kaggle skill` (`SKILL`) 命令实现与上游前瞻兼容包装
│   ├── commands.py           # `kaggle ext {doctor,status,install,uninstall,version}`
│   └── skill_data/           # 随扩展分发的上游 `Kaggle/kaggle-cli/skills/` Markdown 文档
└── tests/
    └── test_kaggle_ext.py    # 单元测试 + 真实 `kaggle` 子进程隔离集成测试
```

### Hook 扩展点（后续功能统一在此注册，不散落 monkey-patch）
- `cli_hooks.register_pre_api_hook(fn)`：在 `kaggle.api.kaggle_api_extended` 执行前运行（用于提前解析 `-a` / `KAGGLE_ACCOUNT` 并在 `KaggleApi.config_dir` 求值前切换隔离目录）。
- `cli_hooks.register_root_parser_hook(fn)`：在根 `ArgumentParser` 构建完成后、`parse_args()` 前运行（用于注册 `-a / --account` 等全局 flag）。
- `cli_hooks.register_subparsers_hook(fn, *, choices=..., help_line=...)`：向根 `subparsers` 挂载新命令组（如 `ext`、`account`、`session`、`skill`）。
- `cli_hooks.register_parsed_args_hook(fn)`：在 `parse_args()` 返回后、`args.func(**command_args)` 调用前运行（消费并 `delattr` 扩展全局参数）。
- `cli_hooks.OFFLINE_EXTENSION_COMMANDS`：无需 Kaggle 登录即可运行的扩展顶层命令集合（含 `"ext"`, `"account"`, `"acct"`, `"session"`, `"sess"`, `"skill"`, `"SKILL"`；其中 `session` 在子命令内部按需鉴权以支持跨账户路由）。

---

## 4. 演进路线图（Roadmap）

- **Phase 1（已完成）**：零侵扰 `.pth` + `sys.meta_path` 注入底座、`install.py` / `install.sh`、`kaggle ext {doctor, status, install, uninstall, version}` 与全套隔离测试。
- **Phase 2（已完成）**：多账户隔离与切换（`kaggle account` / `acct`、全局 `-a / --account`、`KAGGLE_ACCOUNT`、两步式无头 `--url-only` / `--code` OAuth、Token/kaggle.json 导入及 Wayland 浏览器检测修补），统一隔离 `kaggle.json`、`access_token` 与 `credentials.json`。
- **Phase 3（已完成）**：`kaggle skill`（`SKILL`）命令，内置并动态合并上游 `Kaggle/kaggle-cli/skills/`（`SKILL.md` + 13 篇 `references/*.md`）与 `kaggle-ext` 扩展/多账户文档，支持 `--list`、按主题检索（含 `account` / `ext`）、`--all`、`--raw`、`--export` 及 `--json`。
- **Phase 4（已完成）**：`kaggle session`（`sess`）交互式会话与纯标准库 RFC 6455 Jupyter WebSocket 执行/监控层（私有 `QUICK_SAVE` 空壳占位、`CreateKernelSession` + `GetOperation` 轮询、`0600` 分账户 `sessions.json`、隐式内存注密、`.py`/`.ipynb` 逐格执行、独立 `session_id` 只读监控及本地回环 RPC/WS 全隔离回归测试）。

---

## 5. 开发与严密隔离测试规范（Hermetic Testing）

1. **严禁在测试或开发验证中污染宿主环境**：
   - 绝不可读写用户真实的 `~/.kaggle`、`~/.config/kaggle`、`~/.config/kaggle-ext`、`~/.kaggle-ext` 或真实 `uv` `site-packages`。
   - 所有集成测试必须隔离 `HOME`、`XDG_CONFIG_HOME`、`KAGGLE_CONFIG_DIR`、`KAGGLE_EXT_CONFIG_DIR`、`KAGGLE_EXT_HOME`、`KAGGLE_EXT_SOURCE` 与临时 `site-packages`（由于 `uv` 工具 venv 的 `site.ENABLE_USER_SITE = False`，子进程测试通过临时 `PYTHONPATH` 下的单行 `sitecustomize.py` 调用 `site.addsitedir()` 来真实触发 `.pth`）。
2. **本地回归验证命令**：
   ```bash
   python3 -m unittest discover -s tests -v
   ~/.local/share/uv/tools/kaggle/bin/python -m unittest discover -s tests -v
   sh -n install.sh
   ```
3. **开发模式挂载（仅在需要联调本地 CLI 时由用户或明确任务执行）**：
   ```bash
   python3 install.py --link
   ```
