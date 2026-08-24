#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
命理解读师 · DeepSeek 一键接口
==============================

把「排盘 → 格局判定 → DeepSeek 解读 → HTML 命盘」整条链路封装成一个脚本，
你只要输入生辰信息，就能拿到一份可视化命盘报告。

用法（命令行）：
    # 公历 1991-8-15 丑时(1) 男，默认解读当前流年
    py mingli_api.py --solar 1991-8-15 --hour 1 --gender 男

    # 农历 + 指定流年 2027
    py mingli_api.py --lunar 1991-7-6 --hour 1 --gender 男 --year 2027

    # 什么都不带，进入交互式问答（推荐第一次用）
    py mingli_api.py

    # 指定模型 / API Key / 输出目录
    py mingli_api.py --solar 1991-8-15 --hour 1 --gender 男 --model deepseek-v4-pro --api-key sk-你的DeepSeek密钥 --outdir ./output

依赖：
    pip install iztro-py          # 排盘引擎（必须）
    # 其余只用 Python 标准库，无需 requests / openai

DeepSeek API Key 二选一：
    1. 命令行 --api-key sk-xxx
    2. 环境变量 DEEPSEEK_API_KEY=sk-xxx
"""

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.request




# 统一控制台输出编码为 UTF-8，避免 Windows GBK 控制台打印 emoji/箭头时抛 UnicodeEncodeError
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(SCRIPT_DIR, "scripts")
TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "templates", "chart_template.html")
sys.path.insert(0, SCRIPTS_DIR)

# 复用项目内的排盘与 HTML 生成引擎
from calculate_chart import build_chart, enrich_chart   # noqa: E402
from generate_html import generate_html                 # noqa: E402

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-pro"          # DeepSeek Pro 模型（另有 deepseek-v4-flash）
DEFAULT_TEMPERATURE = 0.7

HOUR_NAMES = {
    0: '早子时 (23:00-00:00)', 1: '丑时 (01:00-03:00)',
    2: '寅时 (03:00-05:00)', 3: '卯时 (05:00-07:00)',
    4: '辰时 (07:00-09:00)', 5: '巳时 (09:00-11:00)',
    6: '午时 (11:00-13:00)', 7: '未时 (13:00-15:00)',
    8: '申时 (15:00-17:00)', 9: '酉时 (17:00-19:00)',
    10: '戌时 (19:00-21:00)', 11: '亥时 (21:00-23:00)',
    12: '晚子时 (23:00-00:00)',
}


# ---------------------------------------------------------------------------
# 参考资料加载
# ---------------------------------------------------------------------------
def load_reference(name):
    """读取 references 下的参考文档，作为系统提示的一部分。"""
    path = os.path.join(SCRIPT_DIR, "references", name)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 排盘数据 → 可读文本（喂给 LLM）
# ---------------------------------------------------------------------------
def summarize_chart(chart):
    """把 chart.json 压缩成 LLM 易读的中文摘要。"""
    lines = []
    lines.append("【基本信息】")
    lines.append("公历：" + str(chart.get('solar_date') or '—'))
    lines.append("农历：" + str(chart.get('lunar_date') or '—'))
    lines.append("性别：" + str(chart.get('gender')))
    lines.append("五行局：" + str(chart.get('five_elements')))
    lines.append("命宫地支：" + str(chart.get('soul_palace_branch')) + "    身宫地支：" + str(chart.get('body_palace_branch')))
    mutagens = chart.get('year_mutagens', []) or []
    mut_str = "；".join(m['star'] + m['mutagen'] + '@' + m['palace'] + '(' + m['branch'] + ')' for m in mutagens) or '—'
    lines.append("生年四化：" + mut_str)

    lines.append("")
    lines.append("【十二宫分布】（宫名(天干地支) 主星 | 辅星 | 标记）")
    for p in chart["palaces"]:
        tags = "、".join(p.get("tags", [])) or ""
        tag_str = ("  [" + tags + "]") if tags else ""
        major = "、".join(p.get("major_stars", [])) or "空宫"
        minor = "、".join(p.get("minor_stars", [])) or "无"
        dizhi = p.get("dizhi", "")
        lines.append(p['name'] + "(" + dizhi + ") 主星：" + major + " | 辅星：" + minor + tag_str)
        if p.get("is_empty_palace"):
            borrowed = "、".join(p.get("borrowed_major_stars", [])) or "无"
            coef = p.get("energy_coefficient", 0.7)
            lines.append("    ↳ 空宫，借对宫星曜：" + borrowed + "，能量系数 " + str(coef))

    lines.append("")
    gz = chart.get("gezhi_analysis") or {}
    if gz:
        m = gz.get("quant_metrics", {})
        lines.append("【宏观格局判定】")
        lines.append("格局：" + str(gz.get('gezhi_name')) + "（" + str(gz.get('tech_alias')) + "）")
        lines.append("描述：" + str(gz.get('description')))
        lines.append("三维量化：风险耐受度 " + str(m.get('risk_tolerance')) + "/5，波动恢复力 " + str(m.get('fluctuation_resilience')) + "/5，决策激进度 " + str(m.get('decision_aggressiveness')) + "/5")
        for w in gz.get("stress_warnings", []):
            lines.append("压力预警：" + str(w.get('type')) + " —— " + str(w.get('desc')))

    lines.append("")
    tta = chart.get("time_travel_analysis") or {}
    if tta:
        lines.append("【时空压力审计】")
        lines.append("目标流年：" + str(tta.get('target_year')) + "年 " + str(tta.get('liunian_stem')) + str(tta.get('liunian_branch')) + "；流年化忌星：" + str(tta.get('liunian_ji_star') or '—') + "；大限化忌星：" + str(tta.get('daxian_ji_star') or '—') + "；当前大限宫：" + str(tta.get('current_decadal_palace') or '—'))
        hotspots = tta.get("stress_hotspots", []) or []
        if hotspots:
            lines.append("高危热点（压力分≥2.5）：")
            for h in hotspots:
                lines.append("    " + str(h['palace_name']) + "(" + str(h['earthly_branch']) + ") 压力分 " + str(h['stress_score']) + " —— " + "；".join(h['trigger_sources']))
        else:
            lines.append("高危热点：无")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DeepSeek 调用
# ---------------------------------------------------------------------------
def chat_completion(api_key, messages, model=DEFAULT_MODEL,
                    base_url=DEEPSEEK_BASE_URL, temperature=DEFAULT_TEMPERATURE):
    """调用 DeepSeek chat completions（OpenAI 兼容）。"""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
        "response_format": {"type": "json_object"},
    }

    def _post(body):
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", "Bearer " + api_key)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.read().decode("utf-8")

    try:
        raw = _post(json.dumps(payload).encode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        # 若模型/网关不支持 response_format，去掉后重试一次
        if e.code == 400 and "response_format" in detail:
            payload.pop("response_format", None)
            raw = _post(json.dumps(payload).encode("utf-8"))
        else:
            raise RuntimeError("DeepSeek API 返回 " + str(e.code) + "：" + detail) from e

    result = json.loads(raw)
    return result["choices"][0]["message"]["content"]


def extract_json(text):
    """从模型输出里稳健地取出 JSON 对象。"""
    text = text.strip()
    fence = chr(96) * 3  # 三个反引号组成的 markdown 代码围栏
    if text.startswith(fence):
        # 去掉代码围栏和可能的 json 前缀
        text = text.strip(chr(96))
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("模型输出里没有找到 JSON：" + "\n" + text[:500])
    return json.loads(text[start:end + 1])


# ---------------------------------------------------------------------------
# Prompt 组装
# ---------------------------------------------------------------------------
def build_messages(chart, target_year):
    system = "你是一位有主见的命理咨询师，负责把紫微斗数排盘数据翻译成有温度、有判断力、大白话的解读。\n\n" \
             + load_reference('interpretation_guide.md') + "\n\n" \
             + load_reference('stars_reference.md') + "\n\n" \
             + load_reference('four_hua_reference.md') + "\n\n" \
             + "【输出要求】\n" \
             + "1. 只输出一个合法的 JSON 对象，不要输出任何解释、前言或 markdown 代码块。\n" \
             + "2. 严格按照下面的 reading.json 结构：\n" \
             + "{\n" \
             + "  \"current_decadal_branch\": \"当前大限宫位地支，如'辰'；可留空字符串，系统会自动定位\",\n" \
             + "  \"current_decadal_display\": \"当前大限展示文字，如'辰宫·天机·天梁'；可留空字符串\",\n" \
             + "  \"cards\": [\n" \
             + "    {\n" \
             + "      \"title\": \"章节标题\",\n" \
             + "      \"badge\": \"主星名，如'紫微·贪狼'\",\n" \
             + "      \"full\": true,\n" \
             + "      \"highlight\": true,\n" \
             + "      \"body\": \"解读正文，支持 <strong>强调</strong> <em>金色文字</em> <span class='warn'>警告</span> <span class='good'>利好</span> <br>换行\",\n" \
             + "      \"probabilities\": [{\"label\": \"推算置信度\", \"pct\": 70}, {\"label\": \"校准后可达\", \"pct\": 85}]\n" \
             + "    }\n" \
             + "  ],\n" \
             + "  \"calibration_questions\": [{\"text\": \"问题\", \"hint\": \"补充说明\"}]\n" \
             + "}\n" \
             + "3. cards 至少要覆盖这 5 章（按顺序）：\n" \
             + "   ① 命盘底色 · 先天禀赋（分析命宫主星组合张力，给 3-5 个性格关键词；full + highlight）\n" \
             + "   ② 事业 · 官禄宫（职业倾向、适合路线）\n" \
             + "   ③ 财运 · 财帛宫（财运模式、积累方式）\n" \
             + "   ④ 感情 · 夫妻宫（缘分模式、伴侣特征）\n" \
             + "   ⑤ 当前大限（这十年的核心课题与机遇风险，含 1-2 个近期流年提示）\n" \
             + "4. 注意：第 0 章「宏观格局仪表盘」和第 6 章「时空压力审计」由系统自动渲染，你不要写这两章；但解读时要把「格局判定」当基调锚点，把「高危热点宫位」自然带进大限/流年提示。\n" \
             + "5. 命宫若为空宫，要讲清「借对宫星曜、能量打折」的逻辑。\n" \
             + "6. 语气遵守解释指南：不说「你一定/命中注定」，说「这个格局倾向于/先天禀赋偏向」；先讲积极面，再温和提醒；多用类比翻译术语。\n" \
             + "7. 每章都要有：核心判断一句话 + 展开说明 + 实际影响 + 建议提醒。\n" \
             + "8. calibration_questions 选 3-5 个，感情/婚姻与财务必问。\n" \
             + "9. 禁止预言死亡、制造恐惧、替代决策。"

    user = "请解读下面这张紫微斗数命盘（目标流年：" + str(target_year) + " 年）。\n\n" \
           + summarize_chart(chart) + "\n\n请直接输出 reading.json。"

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(solar=None, lunar=None, hour=None, gender=None, leap=False, year=None,
        model=DEFAULT_MODEL, api_key=None, outdir=None, temperature=DEFAULT_TEMPERATURE):
    if not solar and not lunar:
        raise ValueError("必须提供 --solar 或 --lunar 之一")
    if hour is None:
        raise ValueError("必须提供 --hour（时辰索引 0-12）")
    if gender not in ("男", "女"):
        raise ValueError("--gender 只能是 男 或 女")

    api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("未找到 DeepSeek API Key，请用 --api-key 或设置环境变量 DEEPSEEK_API_KEY")

    target_year = year or datetime.date.today().year
    outdir = outdir or os.path.join(SCRIPT_DIR, "output")
    os.makedirs(outdir, exist_ok=True)

    is_lunar = bool(lunar)
    date_str = lunar if is_lunar else solar

    print("① 排盘中……（" + ("农历" if is_lunar else "公历") + " " + date_str + " " + HOUR_NAMES.get(hour, '') + " " + gender + "）")
    chart = build_chart(date_str, hour, gender, is_lunar=is_lunar, is_leap=leap)
    chart = enrich_chart(chart, target_year)

    print("② 调用 DeepSeek（" + model + "）解读中……（可能需几十秒）")
    messages = build_messages(chart, target_year)
    content = chat_completion(api_key, messages, model=model, temperature=temperature)
    reading = extract_json(content)

    # 兜底：确保必要字段存在
    reading.setdefault("cards", [])
    reading.setdefault("calibration_questions", [])
    reading.setdefault("hand_reading", {"items": []})

    chart_path = os.path.join(outdir, "chart.json")
    reading_path = os.path.join(outdir, "reading.json")
    html_path = os.path.join(outdir, "mingpan.html")
    with open(chart_path, "w", encoding="utf-8") as f:
        json.dump(chart, f, ensure_ascii=False, indent=2)
    with open(reading_path, "w", encoding="utf-8") as f:
        json.dump(reading, f, ensure_ascii=False, indent=2)

    print("③ 生成 HTML 命盘……")
    html = generate_html(chart, reading, TEMPLATE_PATH)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    print("\n✅ 完成！")
    print("   命盘 HTML：" + html_path)
    print("   排盘数据：" + chart_path)
    print("   解读数据：" + reading_path)
    print("   用浏览器打开 HTML 即可查看，右上角可切换「典藏/赛博」双主题。")
    return html_path


# ---------------------------------------------------------------------------
# 交互式模式
# ---------------------------------------------------------------------------
def interactive():
    print("=" * 56)
    print("  命理解读师 · DeepSeek 接口（交互式）")
    print("=" * 56)

    cal = input("历法是公历还是农历？[公历/农历]（默认公历）").strip() or "公历"
    date_str = input("出生年月日（格式 YYYY-M-D，如 1991-8-15）：").strip()
    if not date_str:
        raise SystemExit("已取消：未输入日期")

    print("\n时辰索引：")
    for i in range(13):
        print("  " + str(i).rjust(2) + " = " + HOUR_NAMES[i])
    hour = int(input("出生时辰索引（0-12）：").strip())

    gender = input("性别 [男/女]：").strip()
    if gender not in ("男", "女"):
        raise SystemExit("性别只能是 男 或 女")

    year_s = input("目标流年年份（默认 " + str(datetime.date.today().year) + "）：").strip()
    year = int(year_s) if year_s else None

    leap = False
    if cal == "农历":
        leap = (input("是否闰月？[y/N]：").strip().lower() == "y")

    kwargs = dict(hour=hour, gender=gender, leap=leap, year=year)
    if cal == "农历":
        kwargs["lunar"] = date_str
    else:
        kwargs["solar"] = date_str

    run(**kwargs)


def main():
    parser = argparse.ArgumentParser(description="命理解读师 · DeepSeek 一键接口")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--solar", help="公历日期，格式 YYYY-M-D")
    group.add_argument("--lunar", help="农历日期，格式 YYYY-M-D")
    parser.add_argument("--hour", type=int, help="时辰索引 0-12（0=早子 1=丑 … 12=晚子）")
    parser.add_argument("--gender", choices=["男", "女"], help="性别")
    parser.add_argument("--leap", action="store_true", help="农历闰月（仅 --lunar 有效）")
    parser.add_argument("--year", type=int, help="目标流年年份（默认当前年份）")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="DeepSeek 模型名（默认 deepseek-v4-pro）")
    parser.add_argument("--api-key", help="DeepSeek API Key（或环境变量 DEEPSEEK_API_KEY）")
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE, help="采样温度（默认 0.7）")
    parser.add_argument("--outdir", help="输出目录（默认 ./output）")
    args = parser.parse_args()

    if not (args.solar or args.lunar):
        interactive()
        return

    run(solar=args.solar, lunar=args.lunar, hour=args.hour, gender=args.gender,
        leap=args.leap, year=args.year, model=args.model, api_key=args.api_key,
        outdir=args.outdir, temperature=args.temperature)


if __name__ == "__main__":
    main()
