# kaggle-ext —— `Kaggle/kaggle-cli` 的无侵扰扩展底座

> [English](README.md) | 中文

`kaggle-ext` 用于在不修改官方 [`Kaggle/kaggle-cli`](https://github.com/Kaggle/kaggle-cli)（`kaggle` CLI 及其底层 `kagglesdk`）任何文件的前提下，以**外部挂载 + 按需延迟拦截 + 故障静默降级（Fail-Open）**的方式扩展 `kaggle` 命令行工具。

升级 `kaggle`（如 `uv tool upgrade kaggle` / `pip install -U kaggle`）不会产生任何合并冲突；扩展源码独立存放于 `~/.kaggle-ext/`，仅向目标虚拟环境的 `site-packages/` 写入唯一的单行 `kaggle_ext.pth`。

## 环境要求

- Python 3.11+（安装器、加载器与离线诊断模块均为纯标准库）
- 已安装并可用 [`kaggle`](https://pypi.org/project/kaggle/)（如 `uv tool install kaggle`）

## 安装与管理

### 一键引导安装

```sh
curl -fsSL https://raw.githubusercontent.com/deluxebear/kaggle-ext/main/install.sh | sh
```

### 从本地仓库安装

```bash
git clone https://github.com/deluxebear/kaggle-ext
cd kaggle-ext
python3 install.py              # 复制源码到 ~/.kaggle-ext 并写入 kaggle_ext.pth
python3 install.py --link       # 开发模式：符号链接本仓库，改代码即生效
python3 install.py --status     # 查看探针定位到的 site-packages 与挂载状态
python3 install.py --uninstall  # 卸载 kaggle_ext.pth（保留 ~/.kaggle-ext）
```

## 当前命令

### 多账户隔离与登录（`kaggle account` / `acct`，全局 `-a / --account`）

每个账户都在 `~/.config/kaggle-ext/accounts/<name>/`（`0700` 目录、`0600` 凭据）下独立存放 `credentials.json`、`access_token` 与 `kaggle.json`。首次使用会**无损**把原有 `~/.kaggle/` 凭据纳管为 `default` 账户。

```bash
# 多种登录方式（浏览器 OAuth、手动粘贴链接、两步无头 PKCE、API Token、kaggle.json）
kaggle account login work
kaggle account login alt --no-launch-browser        # 打印链接，贴到另一个浏览器 Profile / 无痕窗口授权
kaggle account login alt --url-only                 # 两步无头登录第 1 步（输出授权 URL 并退出）
kaggle account login alt --code <验证码或回调URL>     # 两步无头登录第 2 步
kaggle account login work --token KGAT_...          # 也支持 --token-file / --token-stdin
kaggle account login work --kaggle-json ~/Downloads/kaggle.json

# 单条命令按账号切换，或修改全局默认账户
kaggle -a work competitions list
kaggle quota -a alt
KAGGLE_ACCOUNT=work kaggle datasets list
kaggle account use work

# 账户管理与体检
kaggle account list [--json]                        # 离线列出所有账户及鉴权状态
kaggle account current [--json]                     # 查看当前解析出的账户
kaggle account status [--json]                      # 并发核验所有账户有效性及 GPU/TPU 每周剩余配额
kaggle account add personal --use
kaggle account logout alt [--revoke]
kaggle account rename old new
kaggle account remove alt [--force]
```

### 扩展管理（`kaggle ext`）

```bash
kaggle ext doctor [--json]      # 自检 .pth 注入状态、当前多账户状态与离线凭据/环境诊断
kaggle ext status [--json]      # doctor 的别名
kaggle ext install [--link]     # 升级 kaggle 后一键重新注入 kaggle_ext.pth
kaggle ext uninstall            # 移除 kaggle_ext.pth
kaggle ext version              # 输出扩展版本与环境路径 JSON
```

`kaggle ext doctor` 与 `kaggle account list` 全程**离线、免登录、只读且脱敏**，绝不输出任何明文密钥/Token。

## 无侵扰工作原理

1. `kaggle_ext.pth` 将 `~/.kaggle-ext` 加入 `sys.path` 并导入 `kaggle_ext_loader`。
2. `kaggle_ext_loader` 注册零开销的 `sys.meta_path` 查找器，**不在启动时 Eager Import `kaggle`**（避免普通 `python -c ...` 触发 `kaggle/__init__.py` 的导入期鉴权）。
3. 当进程真正导入 `kaggle.api.kaggle_api_extended` 时，双阶段 Hook 拦截 `KaggleApi.authenticate`，使 `kaggle ext ...` 等离线扩展命令免登录即可执行，同时原生 `kaggle` 命令保持 100% 上游鉴权行为。
4. 当进程导入 `kaggle.cli` 时，`kaggle_ext.register()` 包装 `kaggle.cli:main()` 内的局部 `ArgumentParser` 与 `Help.kaggle_choices`，挂载扩展子命令。
5. **Fail-Open 容错**：扩展内部发生任何异常都自动降级，绝不阻断官方 `kaggle` 命令（设置 `KAGGLE_EXT_DEBUG=1` 可打印完整堆栈）。

## 演进路线图

- **Phase 1（已完成）**：无侵扰 `.pth` + `sys.meta_path` 底座、`install.py` / `install.sh`、`kaggle ext {doctor, status, install, uninstall, version}`。
- **Phase 2（已完成）**：多账户隔离与切换（`kaggle account` / `-a` / `KAGGLE_ACCOUNT`）、无头两步式 PKCE OAuth（`--url-only` / `--code`）、Token/kaggle.json 导入及 Linux Wayland 浏览器唤起兼容。
- **Phase 3（下一步）**：Agent Skill 指引命令（`kaggle skill`），动态融合上游 `Kaggle/kaggle-cli` 技能文档与扩展指令。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
