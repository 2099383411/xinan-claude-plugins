#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ds_billing.py — DeepSeek 余额 / 今日消耗 / 高峰·空闲时段 查询引擎
================================================================
供 Claude Code 插件 "ds-billing" 使用。纯标准库，Python 3.9+。

功能
----
* balance   : 查询 DeepSeek 账户余额（官方 GET https://api.deepseek.com/user/balance）
* usage     : 按 ~/.claude/projects 下的会话日志统计“今日”各模型 token 消耗，
              并按请求时刻（北京时间）用高峰/空闲分时价格估算人民币花费
* peak      : 当前处于高峰时段还是空闲时段 + 距下一次切换的倒计时
* report    : 以上三者合并的可读报表（斜杠命令 /ds-billing 默认输出）
* statusline: 状态栏单行文本（供 settings.json 的 statusLine 使用）
* selftest  : 内置自检（时段判定与倒计时用例）

高峰/空闲规则（DeepSeek 官方，2026-08-17 峰谷定价方案）
-------------------------------------------------------
  空闲时段价格为高峰时段价格的一半。
  高峰时段 = 北京时间 周一至周五 9:00-12:00、14:00-18:00；其余（含周末全天）为空闲时段。

密钥获取优先级
--------------
  1) 环境变量 DEEPSEEK_API_KEY
  2) 环境变量 ANTHROPIC_AUTH_TOKEN（api.deepseek.com Anthropic 兼容端点场景即 DeepSeek key）
  3) 配置文件 ~/.config/ds-billing/config.json 的 "api_key" 字段

价格表默认值来自官方价目表（人民币 / 每百万 tokens，2026-08 生效），
可用 ~/.config/ds-billing/config.json 里的 "prices" 覆盖（格式见 PRICES 注释）。
"""

import argparse
import json
import math
import os
import re
import shutil
import stat
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# 常量与默认配置
# ---------------------------------------------------------------------------

BASE_URL = "https://api.deepseek.com"
BALANCE_PATH = "/user/balance"

# 官方价目表（2026-08-17 峰谷定价生效；单位：人民币/百万 tokens）
# 每项为 {"hit": (空闲价, 高峰价), "miss": (空闲价, 高峰价), "out": (空闲价, 高峰价)}
#   hit  = 输入(缓存命中)   miss = 输入(缓存未命中，含缓存写入)   out  = 输出
DEFAULT_PRICES = {
    "deepseek-v4-pro": {
        "hit": (0.15, 0.30), "miss": (4.5, 9.0), "out": (13.5, 27.0),
    },
    "deepseek-v4-flash": {
        "hit": (0.05, 0.10), "miss": (1.5, 3.0), "out": (4.5, 9.0),
    },
    "deepseek-v4-flash-vision-exp": {
        "hit": (0.05, 0.10), "miss": (1.5, 3.0), "out": (4.5, 9.0),
    },
}

# 模型别名归一化：日志里的模型串可能带厂商前缀 / 版本号后缀
MODEL_ALIASES = {
    "deepseek/deepseek-v4-pro": "deepseek-v4-pro",
    "deepseek/deepseek-v4-flash": "deepseek-v4-flash",
    "deepseek-v4-pro-0813": "deepseek-v4-pro",
    "deepseek-v4-flash-0731": "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp-0731": "deepseek-v4-flash-vision-exp",
    "deepseek-v4-flash-vision": "deepseek-v4-flash-vision-exp",
}

# 高峰时段（本地/北京时间，分钟粒度）。周一=0 … 周日=6
PEAK_WEEKDAYS = (0, 1, 2, 3, 4)  # 周一~周五
PEAK_SEGMENTS = ((9 * 60, 12 * 60), (14 * 60, 18 * 60))  # 9:00-12:00, 14:00-18:00

CONFIG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "ds-billing", "config.json",
)
CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "ds-billing",
)


# ---------------------------------------------------------------------------
# 时间 / 时段工具（默认北京 UTC+8，可用 config 的 tz_offset_minutes 调整）
# ---------------------------------------------------------------------------

def beijing_tz(offset_minutes=480):
    return timezone(timedelta(minutes=offset_minutes))


def to_local(ts_utc, offset_minutes):
    """ISO UTC 时间字符串 -> 带偏移的 datetime。解析失败返回 None。"""
    if isinstance(ts_utc, (int, float)):
        return datetime.fromtimestamp(ts_utc, tz=beijing_tz(offset_minutes))
    try:
        s = str(ts_utc).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(beijing_tz(offset_minutes))
    except (ValueError, TypeError):
        return None


def weekday_label(dt):
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    return names[dt.weekday()]


def is_peak_at(dt_local):
    """dt_local: 已换算到目标时区的 datetime -> 是否高峰。"""
    if dt_local.weekday() not in PEAK_WEEKDAYS:
        return False
    m = dt_local.hour * 60 + dt_local.minute
    return any(a <= m < b for a, b in PEAK_SEGMENTS)


def next_transition(dt_local, horizon_days=3):
    """
    从 dt_local 起向后找下一个时段切换点（返回 (切换时刻, 切换后是否高峰)）。
    找不到（理论上不会）返回 (None, None)。
    """
    cur = dt_local.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = dt_local + timedelta(days=horizon_days)
    while cur <= limit:
        peak = is_peak_at(cur)
        # 与前一分钟比较即可判断是否发生切换（向后扫描的第一分钟非当前段即切换）
        prev = cur - timedelta(minutes=1)
        if is_peak_at(prev) != peak:
            return cur, peak
        cur += timedelta(minutes=1)
    return None, None


def fmt_duration(total_minutes):
    """把分钟数格式化为 'X小时Y分' / 'Y分' / 'X小时'。"""
    total_minutes = max(0, int(round(total_minutes)))
    h, m = divmod(total_minutes, 60)
    if h and m:
        return "%d小时%d分" % (h, m)
    if h:
        return "%d小时" % h
    return "%d分钟" % m


def peak_summary(now_utc=None, offset_minutes=480):
    """返回 (当前是否高峰, 描述文本, 切换倒计时文本, 切换后是否高峰)。"""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    dt = now_utc.astimezone(beijing_tz(offset_minutes))
    peak_now = is_peak_at(dt)
    nxt, nxt_peak = next_transition(dt)
    if nxt is None:
        return peak_now, "无法计算时段切换", "", None
    mins = (nxt - dt).total_seconds() / 60.0
    countdown = fmt_duration(mins)
    if peak_now:
        desc = "高峰时段（按高峰价计费）"
        label = "峰时：%s后进入空闲（半价）" % countdown
    else:
        desc = "空闲时段（半价计费）"
        label = "峰时：%s后进入高峰" % countdown
    return peak_now, desc, label, nxt_peak


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def load_config():
    cfg = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            cfg = raw
    except (OSError, ValueError):
        pass
    return cfg


def api_key(cfg):
    for env in ("DEEPSEEK_API_KEY", "ANTHROPIC_AUTH_TOKEN", "DS_BILLING_API_KEY"):
        v = os.environ.get(env)
        if v:
            return v
    return cfg.get("api_key") or ""


def prices_table(cfg):
    prices = dict(DEFAULT_PRICES)
    user_p = cfg.get("prices")
    if isinstance(user_p, dict):
        for model, rows in user_p.items():
            if isinstance(rows, dict) and all(k in rows for k in ("hit", "miss", "out")):
                prices[model] = rows
    return prices


def normalize_model(raw):
    if not raw:
        return ""
    name = str(raw).strip().lower()
    name = name.split("/")[-1]
    return MODEL_ALIASES.get(name, name)


# ---------------------------------------------------------------------------
# 余额查询（官方接口示例：GET /user/balance，Bearer Token）
# ---------------------------------------------------------------------------

def fetch_balance(base_url, key, timeout=15):
    url = base_url.rstrip("/") + BALANCE_PATH
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "Authorization": "Bearer " + key,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# 会话日志解析（今日消耗）
# ---------------------------------------------------------------------------

def _iter_log_files(claude_dir):
    projects = os.path.join(claude_dir, "projects")
    if not os.path.isdir(projects):
        return
    for slug in sorted(os.listdir(projects)):
        d = os.path.join(projects, slug)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.endswith(".jsonl"):
                yield os.path.join(d, name)


def scan_usage(claude_dir, day_start_utc, model_filter="deepseek", prices=None):
    """
    扫描日志，返回结构化的消息级用量列表（已按 message.id 去重）。
    day_start_utc: 本日零点(目标时区)对应的 UTC datetime，早于此的消息不计。
    """
    rows = []  # {ts, model, hit, miss, out, peak(bool), file}
    seen = set()
    prices = prices or {}
    slack = timedelta(hours=2)
    for path in _iter_log_files(claude_dir):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
        except OSError:
            continue
        if mtime < day_start_utc - slack:
            continue  # 今天没有新增事件的旧文件直接跳过
        try:
            fh = open(path, "r", encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                model = msg.get("model")
                if not usage or not model or model_filter not in str(model).lower():
                    continue
                mid = msg.get("id") or obj.get("uuid") or ""
                key = (path, mid)
                if mid and key in seen:
                    continue
                if mid:
                    seen.add(key)
                ts = obj.get("timestamp")
                hit = usage.get("cache_read_input_tokens") or 0
                creation = usage.get("cache_creation_input_tokens") or 0
                miss = (usage.get("input_tokens") or 0) + (creation or 0)
                out = usage.get("output_tokens") or 0
                # OpenAI 风格字段兜底
                if not (hit or miss or out):
                    hit = usage.get("prompt_cache_hit_tokens") or 0
                    miss = usage.get("prompt_cache_miss_tokens") or 0
                    out = usage.get("completion_tokens") or 0
                if not (hit or miss or out):
                    continue
                rows.append({
                    "ts": ts, "model": model,
                    "hit": int(hit), "miss": int(miss), "out": int(out),
                    "file": path,
                })
    return rows


# ---------------------------------------------------------------------------
# 计费
# ---------------------------------------------------------------------------

def cost_for(model, hit, miss, out, peak, prices):
    """返回 (成本元, None) 或 (None, 原因)。"""
    name = normalize_model(model)
    row = prices.get(name)
    if not row:
        return None, "模型 %s 不在价格表，未计价（可在配置 prices 中补充）" % name
    idx = 1 if peak else 0  # 1=高峰价, 0=空闲价
    try:
        c = (hit * row["hit"][idx] + miss * row["miss"][idx] + out * row["out"][idx]) / 1e6
    except (KeyError, TypeError, IndexError):
        return None, "模型 %s 价格表格式不正确" % name
    return c, None


def aggregate_day(claude_dir, day_start_utc, prices, offset_minutes, model_filter="deepseek"):
    """
    汇总某一天的消耗：
    return {
      "msgs": int, "rows": [...],
      "by_model": {model: {hit,miss,out,cost}},
      "cost_peak": float, "cost_idle": float, "cost_total": float|None(有未计价时也返回已计价合计)
      "unpriced": [model...]
    }
    """
    out = {
        "msgs": 0, "rows": 0,
        "by_model": {}, "cost_peak": 0.0, "cost_idle": 0.0,
        "cost_total": 0.0, "unpriced": [], "unpriced_msgs": 0,
    }
    raw = scan_usage(claude_dir, day_start_utc, model_filter, prices)
    out["rows"] = len(raw)
    for r in raw:
        ts = to_local(r["ts"], offset_minutes)
        if ts is None or ts < day_start_utc.astimezone(beijing_tz(offset_minutes)):
            # ts 早于当日零点（含 ts 缺失）不计入今日
            if ts is None:
                out["rows"] -= 1  # 无法定位时间的记录不计
                continue
            continue
        peak = is_peak_at(ts)
        name = normalize_model(r["model"])
        m = out["by_model"].setdefault(name, {"model": r["model"], "hit": 0, "miss": 0, "out": 0, "cost": 0.0, "msgs": 0})
        m["hit"] += r["hit"]
        m["miss"] += r["miss"]
        m["out"] += r["out"]
        m["msgs"] += 1
        cost, why = cost_for(r["model"], r["hit"], r["miss"], r["out"], peak, prices)
        if cost is None:
            if name not in out["unpriced"]:
                out["unpriced"].append(name)
            out["unpriced_msgs"] += 1
            continue
        m["cost"] += cost
        out["cost_total"] += cost
        if peak:
            out["cost_peak"] += cost
        else:
            out["cost_idle"] += cost
        out["msgs"] += 1
    return out


# ---------------------------------------------------------------------------
# 展示
# ---------------------------------------------------------------------------

def _fmt_yuan(v):
    if v is None:
        return "-"
    if abs(v) < 0.005:
        return "¥0.00"
    return "¥%.2f" % v


def _fmt_tokens(n):
    return "{:,}".format(int(n))


def _balance_text(data):
    lines = []
    ok = data.get("is_available")
    infos = data.get("balance_infos") or []
    if not isinstance(infos, list):
        infos = []
    if not infos:
        lines.append("账户状态: %s（无余额明细）" % ("可用" if ok else "不可用"))
    for b in infos:
        cur = b.get("currency", "?")
        total = b.get("total_balance", "-")
        grant = b.get("granted_balance") or "0"
        topup = b.get("topped_up_balance") or "0"
        flag = "可用" if ok else "不可用"
        lines.append("%-4s 总额 %s  |  赠送 %s  +  充值 %s   [账户%s]" % (cur, total, grant, topup, flag))
    return "\n".join(lines)


def report(claude_dir, prices, offset_minutes, key, base_url, cfg, as_json=False):
    now_utc = datetime.now(timezone.utc)
    tz = beijing_tz(offset_minutes)
    now_local = now_utc.astimezone(tz)
    day_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_utc = day_start_local.astimezone(timezone.utc)

    agg = aggregate_day(claude_dir, day_start_utc, prices, offset_minutes)

    peak_now, desc, countdown_label, _ = peak_summary(now_utc, offset_minutes)

    # 余额
    bal = None
    bal_err = None
    if key:
        try:
            bal = fetch_balance(base_url, key)
        except urllib.error.HTTPError as e:
            bal_err = "余额接口 HTTP %s（密钥无效或账户异常，请检查 DEEPSEEK_API_KEY/ANTHROPIC_AUTH_TOKEN）" % e.code
        except urllib.error.URLError as e:
            bal_err = "余额接口无法访问：%s（网络不通时余额部分跳过，其余正常）" % e.reason
        except Exception as e:  # noqa: BLE001
            bal_err = "余额接口异常：%s" % e
    else:
        bal_err = "未配置 API Key：设置环境变量 DEEPSEEK_API_KEY（或 ANTHROPIC_AUTH_TOKEN）后再查询余额"

    if as_json:
        return json.dumps({
            "generated_at_utc": now_utc.isoformat(),
            "timezone_offset_minutes": offset_minutes,
            "now": {"local": now_local.isoformat(), "weekday": weekday_label(now_local),
                    "peak": peak_now, "desc": desc, "countdown": countdown_label},
            "balance": bal if bal else {"error": bal_err},
            "today": {
                "date": day_start_local.strftime("%Y-%m-%d"),
                "msgs": agg["msgs"], "rows": agg["rows"],
                "cost_total": round(agg["cost_total"], 6),
                "cost_peak": round(agg["cost_peak"], 6),
                "cost_idle": round(agg["cost_idle"], 6),
                "unpriced": agg["unpriced"], "unpriced_msgs": agg["unpriced_msgs"],
                "by_model": agg["by_model"],
            },
        }, ensure_ascii=False, indent=2)

    W = 72
    buf = []
    buf.append("=" * W)
    buf.append("DeepSeek 账单  |  %s %s %s" % (
        now_local.strftime("%Y-%m-%d %H:%M:%S"), weekday_label(now_local),
        "(北京时间 UTC+%d)" % (offset_minutes // 60)))
    buf.append("=" * W)

    buf.append("")
    buf.append("▶ 余额（Balance）")
    if bal is not None:
        buf.append(_balance_text(bal))
    else:
        buf.append("  %s" % (bal_err or ""))

    buf.append("")
    buf.append("▶ 时段（高峰/空闲 · 按请求时刻计价）")
    buf.append("  当前: %s（%s）" % (desc, "高价" if peak_now else "半价"))
    buf.append("  切换: %s" % countdown_label)
    buf.append("  规则: 高峰 = 北京时间 周一至周五 9:00-12:00 / 14:00-18:00；其余为空闲(半价)")

    buf.append("")
    buf.append("▶ 今日消耗（%s · 估算）" % day_start_local.strftime("%Y-%m-%d"))
    if not agg["by_model"] and not agg["unpriced"]:
        buf.append("  今日（按目标时区）暂无 DeepSeek 用量记录。")
    for name, m in sorted(agg["by_model"].items(), key=lambda kv: -kv[1]["cost"]):
        buf.append("  %-24s %s条消息" % (name, m["msgs"]))
        buf.append("    输入(缓存命中) %12s tokens" % _fmt_tokens(m["hit"]))
        buf.append("    输入(未命中)   %12s tokens" % _fmt_tokens(m["miss"]))
        buf.append("    输出           %12s tokens" % _fmt_tokens(m["out"]))
        buf.append("    小计           %s" % _fmt_yuan(m["cost"]))
    if agg["cost_total"] or agg["unpriced_msgs"]:
        buf.append("  ────────────────────────────────")
        buf.append("  高峰时段花费 %s  /  空闲时段花费 %s" % (_fmt_yuan(agg["cost_peak"]), _fmt_yuan(agg["cost_idle"])))
        buf.append("  今日合计     %s" % _fmt_yuan(agg["cost_total"] if agg["cost_total"] else None if agg["unpriced"] and not agg["cost_total"] else agg["cost_total"]))
        if agg["unpriced"]:
            buf.append("  未计价消息 %s 条（模型 %s 不在价格表，可在 ~/.config/ds-billing/config.json 补充 prices）"
                       % (agg["unpriced_msgs"], "/".join(sorted(set(agg["unpriced"])))))

    buf.append("")
    buf.append("▶ 价格表（人民币/百万 tokens，空闲=半价）")
    head = "  %-26s %10s %10s %10s" % ("模型", "命中·空闲/高峰", "未命中·空闲/高峰", "输出·空闲/高峰")
    buf.append(head)
    for model, row in sorted(prices.items()):
        buf.append("  %-26s %6.2f/%.2f    %6.2f/%.2f    %6.2f/%.2f" % (
            model, row["hit"][0], row["hit"][1], row["miss"][0], row["miss"][1],
            row["out"][0], row["out"][1]))
    buf.append("")
    buf.append("▶ 说明")
    buf.append("  今日消耗为本地日志估算（token 数来自 ~/.claude/projects），价格为内置默认值；")
    buf.append("  余额来自官方接口。价格表可用 ~/.config/ds-billing/config.json 覆盖。")
    buf.append("=" * W)
    return "\n".join(buf)


def _cache_read(name, ttl_seconds, cfg):
    """读缓存 JSON（{text, ts}）；过期/缺失返回 None。"""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        cf = os.path.join(CACHE_DIR, name)
        if os.path.exists(cf) and time.time() - os.path.getmtime(cf) < int(cfg.get("balance_cache_seconds", 60)):
            with open(cf, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and data.get("text"):
                return data["text"]
    except (OSError, ValueError):
        pass
    return None


def _cache_write(name, text):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(os.path.join(CACHE_DIR, name), "w", encoding="utf-8") as fh:
            json.dump({"text": text, "ts": time.time()}, fh)
    except OSError:
        pass


def statusline(claude_dir, prices, offset_minutes, key, base_url, cfg):
    """
    单行状态栏文本（native statusLine 子进程输出纯文本即可），格式如：
      余额：¥294.88  今日：¥2.30  峰值：1小时20分钟后进入高峰   （空闲时）
      余额：¥294.88  今日：¥2.30  峰值：1小时18分钟后进入平价   （高峰时）
    """
    seg = []
    ttl = int(cfg.get("balance_cache_seconds", 60))

    # 1) 余额（缓存 ttl 秒，避免每次工具调用都请求网络）
    bal_text = _cache_read("balance.json", ttl, cfg)
    if not bal_text and key:
        try:
            data = fetch_balance(base_url, key)
            total = None
            for b in (data.get("balance_infos") or []):
                if b.get("currency") == "CNY" or (total is None and b.get("total_balance")):
                    total = b.get("total_balance")
                    break
            if total:
                bal_text = "余额：¥%s" % total
                _cache_write("balance.json", bal_text)
        except Exception:  # noqa: BLE001
            bal_text = ""
    if bal_text:
        seg.append(bal_text)

    # 2) 今日消耗（缓存 5 分钟：需要扫日志）
    ttl_today = int(cfg.get("today_cache_seconds", 300))
    today_text = _cache_read("today.json", ttl_today, cfg)
    if not today_text:
        now_utc = datetime.now(timezone.utc)
        tz = beijing_tz(offset_minutes)
        day_start_local = now_utc.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        day_start_utc = day_start_local.astimezone(timezone.utc)
        agg = aggregate_day(claude_dir, day_start_utc, prices, offset_minutes)
        if agg["msgs"] or agg["rows"]:
            today_text = "今日：¥%.2f" % (agg["cost_total"] if agg["cost_total"] else 0.0)
            _cache_write("today.json", today_text)
    if today_text:
        seg.append(today_text)

    # 3) 峰值倒计时：当前高峰 → “X后进入平价”；当前空闲(半价) → “X后进入高峰”
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(beijing_tz(offset_minutes))
    peak_now = is_peak_at(now_local)
    nxt, nxt_peak = next_transition(now_local)
    if nxt:
        mins = (nxt - now_local).total_seconds() / 60.0
        dur = fmt_duration(mins)
        if peak_now:
            seg.append("峰值：%s后进入平价" % dur)
        else:
            seg.append("峰值：%s后进入高峰" % dur)
    else:
        seg.append("峰值：暂无法计算")
    return "  ".join(seg)


def _atomic_write_json(path, data, mode=None):
    """以临时文件 + os.replace 原子写 JSON，保留原文件权限。"""
    if mode is None:
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            mode = 0o600
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def setup_statusline(claude_dir, _prices, _offset_minutes, _key, _base_url, cfg=None,
                  refresh_interval=None):
    """
    一次性注册常驻状态栏（等价于第三方状态栏插件的 setup）：
    把 ds-billing 段并入 ~/.claude/settings.json 的 statusLine。
    - 保留用户原有 statusLine：原命令存 statusline.ds-billing.prev.cmd（供调度脚本先行输出）；
      完整原 statusLine 对象存 statusline.ds-billing.prev.sl.json（供撤销时精确还原）；
    - settings.json 整体备份到 settings.json.ds-billing.bak 仅作兜底；
    - 幂等：重复执行不覆盖已保存的原值；undo 只还原本插件注册的 statusLine 一处。
    """
    cfg = cfg or {}
    settings_path = os.path.join(claude_dir, "settings.json")
    dispatcher = os.path.join(claude_dir, "statusline.ds-billing.sh")
    prevfile = os.path.join(claude_dir, "statusline.ds-billing.prev.cmd")
    prevsl = os.path.join(claude_dir, "statusline.ds-billing.prev.sl.json")
    scriptref = os.path.join(claude_dir, "statusline.ds-billing.script")
    backup = os.path.join(claude_dir, "settings.json.ds-billing.bak")

    data = {}
    if os.path.exists(settings_path):
        with open(settings_path, "r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
    sl = data.get("statusLine")
    prev_cmd = sl.get("command") if isinstance(sl, dict) else None
    already = bool(prev_cmd) and os.path.normpath(prev_cmd) == os.path.normpath(dispatcher)

    if not already:
        # 首次注册（或用户改回非 ds-billing 后重跑）：保存"注册前"原值，供撤销精确还原
        with open(prevsl, "w", encoding="utf-8") as fh:
            json.dump({"sl": sl}, fh, ensure_ascii=False)
        with open(prevfile, "w", encoding="utf-8") as fh:
            fh.write(prev_cmd if prev_cmd else "")
        if not os.path.exists(backup):
            shutil.copy2(settings_path, backup)

    # 引擎脚本绝对路径（供调度脚本调用；随本文件位置解析）
    engine = os.path.realpath(__file__)
    with open(scriptref, "w", encoding="utf-8") as fh:
        fh.write(engine)

    dispatcher_body = r"""#!/usr/bin/env bash
# ds-billing statusline dispatcher — 由 `ds-billing setup-statusline` 生成。
# 行为：先输出原 statusLine（若存在），再输出 ds-billing 段（余额/今日/峰值倒计时）。
# 撤销：`ds-billing undo-statusline`（仅还原本插件注册的 statusLine，其余配置不动）。
set -uo pipefail
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

PREV="$CLAUDE_DIR/statusline.ds-billing.prev.cmd"
if [ -s "$PREV" ]; then
  OUT=$(bash -c "$(cat "$PREV")" 2>/dev/null)
  rc=$?
  if [ $rc -eq 0 ] && [ -n "$OUT" ]; then
    printf '%s\n' "$OUT"
  fi
fi

PY=""
for c in /usr/bin/python3 \
         /opt/homebrew/bin/python3.14 \
         /opt/homebrew/bin/python3.13 \
         /opt/homebrew/bin/python3.12 \
         /opt/homebrew/bin/python3.11 \
         /usr/local/bin/python3 \
         python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'pass' >/dev/null 2>&1; then
    PY="$c"
    break
  fi
done
[ -z "$PY" ] && exit 0

SCRIPT=""
REF="$CLAUDE_DIR/statusline.ds-billing.script"
if [ -f "$REF" ]; then
  SCRIPT="$(cat "$REF")"
  [ -f "$SCRIPT" ] || SCRIPT=""
fi
if [ -z "$SCRIPT" ] && [ -f "$CLAUDE_DIR/skills/ds-billing/scripts/ds_billing.py" ]; then
  SCRIPT="$CLAUDE_DIR/skills/ds-billing/scripts/ds_billing.py"
fi
if [ -z "$SCRIPT" ]; then
  for d in "$CLAUDE_DIR/plugins/cache"/*/ds-billing/*/; do
    [ -f "$d/scripts/ds_billing.py" ] && SCRIPT="$d/scripts/ds_billing.py" && break
  done
fi
[ -n "$SCRIPT" ] && [ -f "$SCRIPT" ] && "$PY" "$SCRIPT" statusline 2>/dev/null
exit 0
"""
    with open(dispatcher, "w", encoding="utf-8") as fh:
        fh.write(dispatcher_body)
    os.chmod(dispatcher, 0o755)

    new_sl = dict(sl) if isinstance(sl, dict) else {}
    new_sl["type"] = sl.get("type", "command") if isinstance(sl, dict) else "command"
    new_sl["command"] = dispatcher
    if refresh_interval is not None:
        if refresh_interval > 0:
            new_sl["refreshInterval"] = refresh_interval
        else:
            new_sl.pop("refreshInterval", None)
    data["statusLine"] = new_sl
    _atomic_write_json(settings_path, data)

    print("已注册 ds-billing 常驻状态栏：")
    print("  settings.json.statusLine.command = %s" % dispatcher)
    print("  注册前原值已保存（undo 只还原此字段，其余配置不受影响）")
    print("  原状态栏命令保留于 %s；settings.json 整体备份于 %s（仅兜底）" % (prevfile, backup))
    if refresh_interval is not None and refresh_interval > 0:
        print("  定时刷新 refreshInterval = %s 秒（空闲时也会自动更新倒计时）" % refresh_interval)
    elif refresh_interval is not None:
        print("  已移除 refreshInterval（恢复为仅事件驱动刷新）")
    print("提示：重启 Claude Code（或新开会话）后，底部状态栏将显示：余额 / 今日 / 峰值倒计时。")
    print("撤销：ds-billing undo-statusline")
    return 0


def undo_statusline(claude_dir, _prices=None, _offset_minutes=None, _key=None, _base_url=None, cfg=None):
    """
    外科手术式撤销：只把 statusLine 还原为注册前状态（或移除），
    settings.json 其余字段（后来安装的插件/手动改动）一律保持不动。
    """
    settings_path = os.path.join(claude_dir, "settings.json")
    dispatcher = os.path.join(claude_dir, "statusline.ds-billing.sh")
    prevfile = os.path.join(claude_dir, "statusline.ds-billing.prev.cmd")
    prevsl = os.path.join(claude_dir, "statusline.ds-billing.prev.sl.json")
    scriptref = os.path.join(claude_dir, "statusline.ds-billing.script")
    backup = os.path.join(claude_dir, "settings.json.ds-billing.bak")

    touched = False
    if os.path.exists(settings_path):
        with open(settings_path, "r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
        sl = data.get("statusLine")
        ours = isinstance(sl, dict) and os.path.normpath(str(sl.get("command", ""))) == os.path.normpath(dispatcher)
        if ours:
            # 1) 优先用注册前保存的完整 statusLine 对象精确还原（其余字段不动）
            restored = False
            if os.path.exists(prevsl):
                try:
                    with open(prevsl, "r", encoding="utf-8") as fh:
                        snap = json.load(fh)
                    orig = snap.get("sl") if isinstance(snap, dict) else None
                    if orig is None:
                        data.pop("statusLine", None)      # 注册前本无状态栏 → 移除
                    elif isinstance(orig, dict):
                        data["statusLine"] = orig
                    else:
                        data.pop("statusLine", None)
                    restored = True
                except (OSError, ValueError):
                    restored = False
            # 2) 兜底：按保存的原命令字符串还原
            if not restored:
                cmd = ""
                if os.path.exists(prevfile):
                    try:
                        with open(prevfile, "r", encoding="utf-8") as fh:
                            cmd = fh.read().strip()
                    except OSError:
                        cmd = ""
                if cmd:
                    data["statusLine"] = {"type": "command", "command": cmd}
                else:
                    data.pop("statusLine", None)
            _atomic_write_json(settings_path, data)
            touched = True
            print("已将 statusLine 还原为注册前状态；settings.json 其余字段保持不动。")
        else:
            print("当前 statusLine.command 已不是 ds-billing 调度脚本（可能已被其他修改覆盖），"
                  "未改动你的配置。")
    # 清理本插件生成的文件（含兜底备份；settings.json 本身保留现状）
    for f in (dispatcher, prevfile, prevsl, scriptref, backup):
        if os.path.exists(f):
            os.remove(f)
            touched = True
    print("已撤销 ds-billing 状态栏注册（%s）。重启 Claude Code 后生效。" %
          ("还原/清理完成" if touched else "未发现 ds-billing 注册痕迹，无需处理"))
    return 0


def peak_text(offset_minutes):
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(beijing_tz(offset_minutes))
    peak_now, desc, countdown_label, _ = peak_summary(now_utc, offset_minutes)
    return "%s %s | %s | %s" % (
        now_local.strftime("%Y-%m-%d %H:%M:%S %a"),
        desc, weekday_label(now_local), countdown_label)


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def selftest():
    ok = True
    tz = beijing_tz(480)

    def chk(name, cond):
        nonlocal ok
        print(("PASS  " if cond else "FAIL  ") + name)
        ok = ok and cond

    def loc(dt):
        return dt.replace(tzinfo=tz)

    # 动态取一个真实周一与周六，避免硬编码日期漂移
    base = datetime.now(tz).date()
    days_since_mon = base.weekday()
    monday = datetime.combine(base - timedelta(days=days_since_mon), datetime.min.time())
    saturday = monday + timedelta(days=5)
    tuesday = monday + timedelta(days=1)

    def at(day, hhmm):
        h, m = divmod(hhmm, 100)
        return loc(datetime(day.year, day.month, day.day, h, m))

    chk("周一10:00 高峰", is_peak_at(at(monday, 1000)))
    chk("周一09:00 高峰(含边界)", is_peak_at(at(monday, 900)))
    chk("周一08:59 空闲", not is_peak_at(at(monday, 859)))
    chk("周一12:00 空闲(午休开始)", not is_peak_at(at(monday, 1200)))
    chk("周一13:59 空闲", not is_peak_at(at(monday, 1359)))
    chk("周一14:00 高峰(下午开始)", is_peak_at(at(monday, 1400)))
    chk("周一17:59 高峰", is_peak_at(at(monday, 1759)))
    chk("周一18:00 空闲(下午结束)", not is_peak_at(at(monday, 1800)))
    chk("周一.weekday==0", monday.weekday() == 0)
    # 周末
    chk("周六10:00 空闲(周末)", not is_peak_at(at(saturday, 1000)))
    chk("周六.weekday==5", saturday.weekday() == 5)

    # 倒计时
    nxt, p = next_transition(at(monday, 1000))
    chk("周一10:00 -> 下一切换12:00空闲", nxt == at(monday, 1200) and p is False)
    nxt, p = next_transition(at(monday, 1230))
    chk("周一12:30 -> 下一切换14:00高峰", nxt == at(monday, 1400) and p is True)
    nxt, p = next_transition(at(saturday, 1000))
    chk("周六10:00 -> 下一切换周一09:00高峰", nxt == at(monday + timedelta(days=7), 900) and p is True)
    nxt, p = next_transition(at(monday, 1830))
    chk("周一18:30 -> 下一切换周二09:00高峰", nxt == at(tuesday, 900) and p is True)

    chk("fmt 83分钟", fmt_duration(83) == "1小时23分")
    chk("fmt 130分钟", fmt_duration(130) == "2小时10分")

    # 计费
    cost, why = cost_for("deepseek-v4-pro", 1_000_000, 0, 0, False, DEFAULT_PRICES)
    chk("pro 命中1M 空闲=0.15元", cost == 0.15 and why is None)
    cost, _ = cost_for("deepseek-v4-pro", 0, 1_000_000, 0, True, DEFAULT_PRICES)
    chk("pro 未命中1M 高峰=9.0元", cost == 9.0)
    cost, _ = cost_for("deepseek-v4-pro", 0, 0, 1_000_000, True, DEFAULT_PRICES)
    chk("pro 输出1M 高峰=27.0元", cost == 27.0)
    cost, _ = cost_for("deepseek-v4-pro", 0, 0, 1_000_000, False, DEFAULT_PRICES)
    chk("pro 输出1M 空闲=13.5元", cost == 13.5)
    cost, why = cost_for("deepseek/deepseek-v4-pro-0813", 100_000, 0, 0, False, DEFAULT_PRICES)
    chk("别名 deepseek/deepseek-v4-pro-0813 命中=0.015元", cost == 0.015 and why is None)
    cost, why = cost_for("some-unknown-model", 1, 0, 0, False, DEFAULT_PRICES)
    chk("未知模型不计价", cost is None and why is not None)

    print("SELFTEST %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="ds-billing", description="DeepSeek 余额/今日消耗/峰谷时段")
    ap.add_argument("mode", nargs="?", default="report",
                    choices=["report", "balance", "usage", "peak", "statusline", "selftest",
                             "setup-statusline", "undo-statusline"],
                    help="report(默认)/balance/usage/peak/statusline/setup-statusline/undo-statusline/selftest")
    ap.add_argument("--json", action="store_true", help="report 输出 JSON")
    ap.add_argument("--claude-dir", default=None, help="Claude 配置目录（默认 $CLAUDE_CONFIG_DIR 或 ~/.claude）")
    ap.add_argument("--key", default=None, help="DeepSeek API Key（默认读环境变量/配置文件）")
    ap.add_argument("--base-url", default=None, help="API Base URL（默认 https://api.deepseek.com）")
    ap.add_argument("--offset-minutes", type=int, default=None, help="目标时区偏移（默认 480=北京 UTC+8）")
    ap.add_argument("--model-filter", default="deepseek", help="日志模型过滤子串（默认 deepseek）")
    ap.add_argument("--refresh-interval", type=int, default=None,
                    help="setup-statusline 专用：状态栏定时刷新秒数（>0 启用；0 移除；缺省=仅事件驱动）")
    args = ap.parse_args(argv)

    cfg = load_config()
    claude_dir = args.claude_dir or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    offset = args.offset_minutes if args.offset_minutes is not None else int(cfg.get("tz_offset_minutes", 480))
    key = args.key or api_key(cfg)
    base_url = (args.base_url or cfg.get("base_url") or BASE_URL).rstrip("/")
    prices = prices_table(cfg)

    if args.mode == "selftest":
        return selftest()

    if args.mode in ("setup-statusline", "undo-statusline"):
        if args.mode == "setup-statusline":
            return setup_statusline(claude_dir, prices, offset, key, base_url, cfg,
                                    refresh_interval=args.refresh_interval)
        return undo_statusline(claude_dir, prices, offset, key, base_url, cfg)

    if args.mode == "peak":
        print(peak_text(offset))
        return 0

    if args.mode == "balance":
        if not key:
            print("未配置 API Key：请设置环境变量 DEEPSEEK_API_KEY 或 ANTHROPIC_AUTH_TOKEN")
            return 2
        try:
            data = fetch_balance(base_url, key)
        except urllib.error.HTTPError as e:
            print("余额接口 HTTP %s" % e.code)
            return 3
        except Exception as e:  # noqa: BLE001
            print("余额接口异常：%s" % e)
            return 3
        print(_balance_text(data))
        return 0

    if args.mode == "usage":
        now_utc = datetime.now(timezone.utc)
        tz = beijing_tz(offset)
        day_start_local = now_utc.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        day_start_utc = day_start_local.astimezone(timezone.utc)
        agg = aggregate_day(claude_dir, day_start_utc, prices, offset, args.model_filter)
        print(json.dumps(agg, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.mode == "statusline":
        text = statusline(claude_dir, prices, offset, key, base_url, cfg)
        print(text)
        return 0

    # report
    out = report(claude_dir, prices, offset, key, base_url, cfg, as_json=args.json)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
