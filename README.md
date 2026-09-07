# xinan-claude-plugins

信安云科（北京）科技有限责任公司出品的 **Claude Code 插件市场**（开源，Apache-2.0）。

通过 Claude Code 的插件市场机制分发插件：`marketplace add` 一次后即可安装、更新；升级检测以各插件 `plugin.json` 的 `version` 为准。

## 插件列表

| 插件 | 说明 |
|:---|:---|
| [ds-billing](plugins/ds-billing/README.md) | DeepSeek 账户余额 / 今日消耗 / 高峰·空闲时段倒计时；支持一次性注册底部常驻状态栏（余额 · 今日 · 峰值倒计时） |

## 仓库结构

```
.
├── .claude-plugin/marketplace.json   # 市场清单
├── LICENSE
├── README.md
└── plugins/
    └── ds-billing/                   # 插件（清单/命令/引擎/配置示例）
```

## 安装使用

```text
/plugin marketplace add 2099383411/xinan-claude-plugins
/plugin install ds-billing@xinan-claude-plugins
```

更新插件：

```text
/plugin update ds-billing@xinan-claude-plugins
```

各插件的能力、配置与使用方式见其目录下的 `README.md`。

## 许可证

[Apache-2.0](LICENSE) © 信安云科（北京）科技有限责任公司
