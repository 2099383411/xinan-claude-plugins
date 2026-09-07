# ds-billing — Claude Code 插件：DeepSeek 余额 / 今日消耗 / 峰谷时段

> 出品：**信安云科（北京）科技有限责任公司**

一个 Claude Code **插件包**（`/ds-billing` 斜杠命令 + 可选状态栏），用来：

1. **余额**：调用 DeepSeek 官方 `GET https://api.deepseek.com/user/balance` 显示账户总额/赠送/充值；
2. **今日消耗**：扫描本地会话日志（`~/.claude/projects/*/*.jsonl`）统计今日各模型 token，
   并按**请求时刻**所在的高峰/空闲时段用官方分时价格估算人民币花费；
3. **峰时**：按官方规则（高峰 = 北京时间周一~周五 9:00-12:00 / 14:00-18:00，其余为空闲且价格减半）
   显示当前处于哪个时段，以及**距下一次切换的倒计时**，例如：
   `峰时：1小时23分后进入空闲（半价）` / `峰时：2小时10分后进入高峰`。

> 分时价格内置为 DeepSeek 官方峰谷定价（deepseek-v4-pro / deepseek-v4-flash /
> deepseek-v4-flash-vision-exp，人民币/百万 tokens），可用配置文件覆盖。
> "今日消耗"为**本地日志估算**，不是官方账单；DeepSeek 官方仅公开余额接口。

---

## 目录结构

```
ds-billing/
├── .claude-plugin/plugin.json   # 插件清单
├── commands/ds-billing.md       # 斜杠命令 /ds-billing
├── bin/ds-billing               # 可执行入口（插件启用后自动加入 Bash PATH）
├── scripts/ds_billing.py        # 核心引擎（纯标准库，Python 3.9+）
├── config.example.json          # 可选配置文件样例
├── README.md
└── LICENSE
```

参考来源：
- Claude Code 官方文档 [Plugins reference](https://code.claude.com/docs/en/plugins-reference)
- Claude Code 官方文档 [Customize your status line](https://code.claude.com/docs/en/statusline)
- DeepSeek 官方 [Get User Balance](https://api-docs.deepseek.com/api/get-user-balance/)

---

## 安装

**方式 A（推荐）：从市场安装**

```text
/plugin marketplace add 2099383411/xinan-claude-plugins
/plugin install ds-billing@xinan-claude-plugins
```

版本与升级：插件版本由 `.claude-plugin/plugin.json` 的 `version` 控制；更新执行
`/plugin update ds-billing@xinan-claude-plugins`。

**方式 B：本地目录**

将本插件目录放入 `~/.claude/skills/<name>/`（下次会话自动加载），或临时体验：
`claude --plugin-dir <本插件目录绝对路径>`。

安装后输入 `/ds-billing` 即可查看完整报表；也可直接运行裸命令 `ds-billing report`
（插件启用时 `bin/` 已加入 Bash PATH）。

> 纯本地试用（不装插件）也可以直接运行引擎：
> `python3 scripts/ds_billing.py report`

---

## API Key 配置

脚本按以下顺序取密钥（**只读使用，绝不回显**）：

1. 环境变量 `DEEPSEEK_API_KEY`
2. 环境变量 `ANTHROPIC_AUTH_TOKEN`（走 `api.deepseek.com` Anthropic 兼容端点时，
   `~/.claude/settings.json` 的 `env.ANTHROPIC_AUTH_TOKEN` 即 DeepSeek key，无需新增配置）
3. `~/.config/ds-billing/config.json` 的 `api_key` 字段

仅查询余额需要 key；时段倒计时与今日 token 统计离线可用。

---

## 使用

### 斜杠命令

```
/ds-billing              # 完整报表：余额 + 今日消耗 + 时段与倒计时
/ds-billing --json       # JSON 输出
```

### 命令行

```bash
ds-billing report             # 同 /ds-billing
ds-billing peak               # 只要当前时段 + 切换倒计时（离线）
ds-billing balance            # 只要余额
ds-billing usage              # 今日 token/花费明细（JSON）
ds-billing statusline         # 状态栏单行文本（余额/今日/峰值倒计时）
ds-billing setup-statusline   # 一次性注册常驻状态栏（保留原状态栏、自动备份、可撤销）
ds-billing undo-statusline    # 撤销注册（仅还原 statusLine 一处，其余配置不动）
ds-billing selftest           # 内置自检（时段边界/计费/别名等）
```

### 状态栏（statusline）— 一次性注册，以后常驻

Claude Code 原生状态栏由 `~/.claude/settings.json` 的 `statusLine` 字段配置（子进程 stdin 收事件、
stdout 输出文本，见[官方文档](https://code.claude.com/docs/en/statusline)）。

**一键注册（自动合并、可撤销）**：

```bash
ds-billing setup-statusline     # 或 /ds-billing setup-statusline
ds-billing setup-statusline --refresh-interval 60   # 注册并启用定时刷新（可选）
```

- 把 ds-billing 段写入 settings.json 的 `statusLine`，并**保留原有状态栏**（如其他 HUD 插件的行
  会先输出，随后输出 ds-billing 行：余额 / 今日消耗 / 峰值倒计时）；
- 撤销是**外科手术式**：只把 `statusLine` 还原为注册前状态（含完整原对象快照），
  期间对 settings.json 的其他改动一律保留；`settings.json.ds-billing.bak` 仅为兜底，撤销后自动清理；
- **默认"事件驱动刷新"**：状态栏只在会话有活动时更新，空闲时倒计时会定格；
  想空闲也自动刷新，加 `--refresh-interval 60`（0 表示移除定时恢复事件驱动）；
- **重启 Claude Code（或新开会话）后生效**。

状态栏单行文本形如：

```
余额：¥294.75  今日：¥3.32  峰值：1小时16分后进入高峰     ← 空闲(半价)时
余额：¥294.75  今日：¥3.32  峰值：1小时18分后进入平价     ← 高峰时
```

（余额缓存 60 秒、今日消耗缓存 300 秒，可在配置文件中调 `balance_cache_seconds` /
`today_cache_seconds`。）

### 配置文件（可选）

复制 `config.example.json` 到 `~/.config/ds-billing/config.json` 即可覆盖价格/密钥等：

```jsonc
{
  "api_key": "sk-...",              // 可选；也可只用环境变量
  "base_url": "https://api.deepseek.com",
  "tz_offset_minutes": 480,         // 目标时区（默认北京 UTC+8）
  "balance_cache_seconds": 60,      // 状态栏余额缓存秒数，0 = 关闭
  "prices": {                        // 覆盖/补充价格（人民币/百万 tokens）
    "deepseek-v4-pro": { "hit": [0.15, 0.30], "miss": [4.5, 9.0], "out": [13.5, 27.0] }
  }
}
```

> 数组两项依次为 空闲价 / 高峰价；官方规则固定"空闲=高峰的一半"，官方调价只需改这里。
> 价目来源：[DeepSeek 定价页](https://api-docs.deepseek.com/quick_start/pricing)。

---

## 说明与免责

- 余额：官方接口实时返回。
- 今日消耗：从 `~/.claude/projects` 会话日志统计 token（按消息 id 去重），再按官方分时价估算；
  模型不在价格表时如实标注"未计价"，不猜测金额。
- 高峰/空闲判定：高峰 = 北京时间周一~周五 9:00-12:00、14:00-18:00；其余（含周末、午休、夜间）
  为空闲（半价），不含节假日特例，与官方公告一致。
- 价格与规则可能随 DeepSeek 官方调整，请以官网为准并及时更新 `prices` 配置。
