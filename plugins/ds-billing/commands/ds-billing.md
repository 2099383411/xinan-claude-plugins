---
description: 查看 DeepSeek 余额、今日消耗、高峰/空闲时段倒计时；注册/撤销常驻状态栏
argument-hint: [report|peak|setup-statusline|undo-statusline|--json|--refresh-interval N]
allowed-tools: Bash
---

# /ds-billing — DeepSeek 余额 / 今日消耗 / 峰谷时段

先找到本插件的引擎脚本，再用 `python3` 执行它并**原样向用户呈现输出**。
不要编造任何数字；脚本没有网络/日志可读时如实转述其提示。

## 第一步：定位引擎脚本

按以下顺序尝试，把第一个成功命中的路径存为 `DS_SCRIPT`：

```bash
if command -v ds-billing >/dev/null 2>&1; then
  # bin/ 目录随插件启用加入了 Bash PATH
  echo "BIN"
elif [ -n "$CLAUDE_PLUGIN_ROOT" ] && [ -f "$CLAUDE_PLUGIN_ROOT/scripts/ds_billing.py" ]; then
  echo "ROOT:$CLAUDE_PLUGIN_ROOT/scripts/ds_billing.py"
else
  # 市场安装的插件在 ~/.claude/plugins/cache/<marketplace>/ds-billing/<version>/
  find "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins" -type f -name "ds_billing.py" -path "*ds-billing*" 2>/dev/null | sort -V | tail -1
fi
```

- 若 `find` 返回空：本插件可能放在本地源码目录（未安装）。请用 Glob/Bash 在当前目录及上一级目录找
  `scripts/ds_billing.py`；仍找不到就询问用户插件目录在哪，不要臆测路径。
- 若环境变量不存在：直接跳到 find 分支即可。

## 第二步：按调用参数执行

先看**本次调用带不带参数**（用户在 `/ds-billing` 或 `/ds-billing:ds-billing` 后面输入的内容，
可能出现在对话里）并严格按参数分支执行：

| 用户输入 | 你要执行 |
|:---|:---|
| 无参数 / `report` | `<DS_SCRIPT> report`（完整报表） |
| `peak` | `<DS_SCRIPT> peak` |
| `balance` | `<DS_SCRIPT> balance` |
| `--json` | `<DS_SCRIPT> report --json` |
| `setup-statusline` | 直接执行 `<DS_SCRIPT> setup-statusline`（见下方"常驻状态栏"），不要跑 report |
| `setup-statusline --refresh-interval 60` | 注册并启用**定时刷新**（空闲时每 60 秒自动更新倒计时） |
| `undo-statusline` | 直接执行 `<DS_SCRIPT> undo-statusline`，并向用户说明还原结果 |
| `selftest` | `<DS_SCRIPT> selftest` |

把上一步得到的路径（无命中则用 `BIN` 分支的裸命令 `ds-billing`）替换 `<DS_SCRIPT>` 后执行；
如果 `<DS_SCRIPT>` 是裸命令 `ds-billing`，直接运行 `ds-billing <参数>` 即可。

## 常驻状态栏（一次性注册，可选）

用户希望"底部状态栏一直显示 余额/今日/峰值倒计时"（或本次调用带 `setup-statusline` 参数）时，运行：

```bash
<DS_SCRIPT> setup-statusline
# 想让空闲时倒计时也自动刷新：加 --refresh-interval 60（0 表示移除定时，恢复事件驱动）
<DS_SCRIPT> setup-statusline --refresh-interval 60
```

- 该命令会把 ds-billing 段并入 `~/.claude/settings.json` 的 `statusLine`；
- **保留用户原有状态栏**（如其他 HUD 插件的行，会先输出，再输出 DS 行）；
- 写入前先备份 settings.json（`settings.json.ds-billing.bak`，仅兜底）；
- 撤销是**外科手术式**：只把 statusLine 还原为注册前状态，
  期间安装的其他插件/对 settings.json 的改动不受影响；
  可撤销：`<DS_SCRIPT> undo-statusline`；
- 执行后**重启 Claude Code 或新开会话**生效。
- 执行前先向用户确认是否要改动其 settings.json（此操作会写用户配置）。

## 第三步：向用户说明

- 把报表完整贴给用户，分块解释：余额（官方接口）、今日消耗（本地日志估算 +
  官方 2026-08-17 峰谷分时价格）、当前时段与"距下次切换"倒计时。
- 若余额区显示"未配置 API Key"：
  - 提示在 `~/.claude/settings.json` 的 `env` 里设置 `DEEPSEEK_API_KEY`
    （若本机走 api.deepseek.com 的 Anthropic 兼容端点，已存在的
    `ANTHROPIC_AUTH_TOKEN` 即 DeepSeek key，无需新增）。
  - 或写入 `~/.config/ds-billing/config.json`：`{"api_key": "sk-..."}`（文件权限建议 600）。
- 若某模型显示"未计价"：告诉用户可在 `~/.config/ds-billing/config.json` 的 `prices`
  字段补充价目（格式见 scripts/ds_billing.py 顶部注释）。
- **任何时候都不要打印、回显或猜测 API Key 的值。**
