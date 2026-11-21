#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
漫剧雷达 · 数据自动更新脚本

流程：
  1. 抓取 SOURCE_URLS 里的榜单文章（逗号分隔）
  2. 交给 LLM 抽取「剧名 / 平台 / 热度 / 题材」
  3. 聚合回题材结构，覆盖基线里的 count、samples、heat、trend
  4. 输出 data.json（App 远程端点直接读它）

设计原则：
  认剧名、归题材这种事交给模型；id、alias、naming 这些结构字段一律不动。
  模型只负责它擅长的那部分，输出才稳。

环境变量：
  SOURCE_URLS  必填，榜单文章地址，多个用逗号分隔
  LLM_KEY      必填
  LLM_BASE     选填，默认 https://api.deepseek.com/v1
  LLM_MODEL    选填，默认 deepseek-chat
"""

import os
import re
import sys
import json
import datetime
import urllib.request
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BASELINE = os.path.join(HERE, 'baseline.json')
OUTPUT = os.path.join(ROOT, 'data.json')

GENRE_HINT = """
xuanhuan  玄幻仙侠（修真、仙魔、妖神、宗门、剑、上古）
shijing   市井生活流（家庭、邻里、同事、学区房、人情往来、中年生活）
niandai   年代穿越（七零、八零、六零、知青、下乡、供销社、高干）
chuanshu  穿书古言 / 宅斗（穿书、诰命、太子妃、宅斗、科举、宫廷、侯门）
jiating   家庭伦理 / 后妈带娃（后妈、继母、婆家、认亲、整顿全家、带娃）
mieshi    天灾末世 / 囤货（末世、暴雪、囤货、丧尸、废土、求生）
zhichang  职场反差（同事、老板、保安、甲方、打工人、升职）
chongsheng 重生逆袭 / 打脸（重生、逆袭、丑小鸭、替嫁、真假千金、赘婿）
lingyi    灵异 / 诡异（规则怪谈、诡异、灵异、副本、SSS 级）
keju      科举 / 知识逆袭（状元、寒门、书生、史料、制度、算术）
"""

PROMPT = """你是 AI 短剧行业分析师。下面是从一篇榜单文章里提取的正文，请抽取其中的作品信息。

对每一部作品输出四个字段：
- name：剧名，照抄原文，不要改写、不要补全
- platform："hongguo" 或 "douyin"（红果写 hongguo，抖音/端原生写 douyin）
- heat：热度，保留原文单位，比如 "11578万"、"21.84亿"；原文没写就填 ""
- genre：从下面的题材 id 里选一个最贴近的，只能选一个

题材 id 对照表：
%s

另外，根据文章对各个题材走向的描述，给出趋势判断：
- "trends"：形如 {"shijing": "rising"}，只填文章里明确提到趋势的题材
- 取值只能是 rising / stable / declining

再抽一下榜单门槛（第 100 名的数据）：
- "benchmark"：形如 {"hongguo": {"value": 5868, "unit": "万"}, "douyin": {"value": 4.49, "unit": "亿"}}
- hongguo 填红果榜第 100 名的峰值热度，douyin 填抖音榜第 100 名的播放增量
- unit 只能用 "万" 或 "亿"，value 只填数字
- 文章里没明确写就别给这个字段，不要猜

硬性要求：
1. 只输出 JSON，不要任何解释文字
2. 不要编造文章里没有出现的作品
3. 拿不准题材的作品，genre 填 ""

输出格式：
{"works": [{"name": "...", "platform": "hongguo", "heat": "11578万", "genre": "xuanhuan"}], "trends": {}, "benchmark": {"hongguo": {"value": 5868, "unit": "万"}, "douyin": {"value": 4.49, "unit": "亿"}}}

文章正文：
---
%s
---
""" % (GENRE_HINT, '%s')


class TextExtractor(HTMLParser):
    SKIP = {'script', 'style', 'nav', 'footer', 'header', 'aside', 'form'}

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.buf = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth += 1
        if tag in ('p', 'div', 'br', 'h1', 'h2', 'h3', 'li', 'tr'):
            self.buf.append('\n')

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if not self.depth:
            self.buf.append(data)

    def text(self):
        raw = ''.join(self.buf)
        raw = re.sub(r'[ \t\u3000]+', '', raw)
        raw = re.sub(r'\n{2,}', '\n', raw)
        return raw.strip()


def fetch(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/122.0 Safari/537.36',
        'Accept-Language': 'zh-CN,zh;q=0.9',
    })
    with urllib.request.urlopen(req, timeout=40) as resp:
        raw = resp.read()
    for enc in ('utf-8', 'gb18030', 'gbk'):
        try:
            html = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        html = raw.decode('utf-8', 'ignore')

    p = TextExtractor()
    p.feed(html)
    return p.text()


def ask_llm(text):
    """不依赖任何第三方库，直接用 urllib 打 OpenAI 兼容接口。"""
    key = os.environ.get('LLM_KEY')
    if not key:
        raise RuntimeError('缺少 LLM_KEY 环境变量')

    base = (os.environ.get('LLM_BASE') or 'https://api.deepseek.com/v1').rstrip('/')
    model = os.environ.get('LLM_MODEL') or 'deepseek-chat'

    payload = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': PROMPT % text[:14000]}],
        'response_format': {'type': 'json_object'},
        'temperature': 0.1,
    }).encode('utf-8')

    req = urllib.request.Request(
        base + '/chat/completions',
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + key,
        },
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = json.loads(resp.read().decode('utf-8'))

    return json.loads(body['choices'][0]['message']['content'])


def parse_heat(s):
    """统一换算成「万」。解析不出来返回 0。"""
    if not s:
        return 0.0
    m = re.search(r'([\d.]+)\s*(亿|万)?', str(s))
    if not m:
        return 0.0
    v = float(m.group(1))
    unit = m.group(2)
    if unit == '亿':
        v *= 10000
    return v


def load_previous():
    """
    优先读上一期结果（仓库根的 data.json），没有才回落到内置基线。
    这样每次是在上一期基础上更新变化量，而不是每次都从基线重算。
    """
    path = OUTPUT if os.path.exists(OUTPUT) else BASELINE
    print('基准文件：%s' % path)
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def main():
    urls = [u.strip() for u in (os.environ.get('SOURCE_URLS') or '').split(',') if u.strip()]
    if not urls:
        print('没有配置 SOURCE_URLS，退出')
        sys.exit(1)

    base = load_previous()
    before = json.dumps({'g': base.get('genres'), 'b': base.get('benchmark')}, sort_keys=True, ensure_ascii=False)

    parsed = []
    for u in urls:
        try:
            t = fetch(u)
        except Exception as e:
            print('抓取失败 %s：%s' % (u, str(e)[:60]))
            continue
        if len(t) < 200:
            print('正文太短，跳过：%s' % u)
            continue
        print('抓到 %4d 字：%s' % (len(t), u))
        try:
            r = ask_llm(t)
            n = len(r.get('works') or [])
            print('  解析出 %d 部' % n)
            if n:
                parsed.append(r)
        except Exception as e:
            print('  解析失败：%s' % str(e)[:70])

    if not parsed:
        print('所有源都解析失败，退出')
        sys.exit(1)

    # 多源合并去重：同一部剧出现在多个榜单，只算一次
    works, seen = [], set()
    for r in parsed:
        for w in (r.get('works') or []):
            key = (str(w.get('name', ''))[:40], w.get('platform'))
            if key not in seen:
                seen.add(key)
                works.append(w)

    trends = {}
    for r in parsed:
        for k, v in (r.get('trends') or {}).items():
            if v in ('rising', 'stable', 'declining'):
                trends[k] = v

    print('合并去重后 %d 部作品（来自 %d 个源）' % (len(works), len(parsed)))

    # 榜单门槛（第 100 名）也跟着走，复盘页和标题页都靠它
    bmv = {}
    for r in parsed:
        for k in ('hongguo', 'douyin'):
            v = (r.get('benchmark') or {}).get(k) or {}
            if k not in bmv and isinstance(v.get('value'), (int, float)) and v['value'] > 0:
                bmv[k] = v
    for k in ('hongguo', 'douyin'):
        v = bmv.get(k) or {}
        val = v.get('value')
        if isinstance(val, (int, float)) and val > 0:
            unit = v.get('unit')
            base['benchmark'][k]['threshold'] = val
            if unit in ('万', '亿'):
                base['benchmark'][k]['unit'] = unit
            print('门槛更新 %s：%s%s' % (k, val, base['benchmark'][k]['unit']))

    if len(works) < 5:
        print('抽取结果太少（可能不是榜单页），放弃这次更新')
        sys.exit(1)

    by_id = {g['id']: g for g in base['genres']}

    counts = {'hongguo': {}, 'douyin': {}}
    heats = {'hongguo': {}, 'douyin': {}}
    samples = {'hongguo': {}, 'douyin': {}}

    for w in works:
        p = w.get('platform')
        g = w.get('genre')
        if p not in counts or not g or g not in by_id:
            continue
        counts[p][g] = counts[p].get(g, 0) + 1
        heats[p].setdefault(g, []).append(parse_heat(w.get('heat')))
        samples[p].setdefault(g, []).append(w)

    # 热度归一化：用当期最高平均热度做 100 分基准
    peak = 1.0
    for p in heats:
        for g, arr in heats[p].items():
            vals = [v for v in arr if v > 0]
            if vals:
                peak = max(peak, sum(vals) / len(vals))

    enough = len(works) >= 40
    print('样本量 %d 部，%s' % (len(works), '足以更新评分字段' if enough else '偏少，只更新样本、不动评分'))

    # 样本覆盖不到整张百强榜，所以按样本内占比外推到 100 部，
    # 否则抓多抓少会直接改变供给空间，评分跟着忽高忽低
    tot_p = {}
    for p in ('hongguo', 'douyin'):
        tot_p[p] = sum(counts[p].values()) or 1

    changed = 0
    for gid, g in by_id.items():
        if enough:
            for p in ('hongguo', 'douyin'):
                raw = counts[p].get(gid, 0)
                # 样本里一次都没出现，不代表它不在榜，保持原值不动
                if raw > 0:
                    g[p]['count'] = int(round(raw * 100.0 / tot_p[p]))

                vals = [v for v in heats[p].get(gid, []) if v > 0]
                if vals:
                    g[p]['heat'] = max(1, min(100, round(sum(vals) / len(vals) / peak * 100)))

                if gid in trends and trends[gid] in ('rising', 'stable', 'declining'):
                    g[p]['trend'] = trends[gid]

        # 样本取该题材在两端热度最高的三条
        merged = []
        for p in ('hongguo', 'douyin'):
            for w in samples[p].get(gid, []):
                merged.append({
                    'n': w.get('name', ''),
                    'p': '红果' if p == 'hongguo' else '抖音',
                    'h': w.get('heat') or '—',
                    'v': 1,
                    'why': '',
                })
        if merged:
            g['samples'] = merged[:3]
            changed += 1

    after = json.dumps({'g': base.get('genres'), 'b': base.get('benchmark')}, sort_keys=True, ensure_ascii=False)

    if before == after:
        print('抓到的还是同一批作品，数据无实质变化')
        print('跳过写入，updated 保持为 %s —— 新鲜度才不会被刷成假的' % base.get('updated'))
        return

    base['updated'] = datetime.date.today().isoformat()

    with open(OUTPUT, 'w', encoding='utf-8') as f:
        json.dump(base, f, ensure_ascii=False, indent=2)

    print('已写入 %s，%d 个题材的样本有更新，日期改为 %s' % (OUTPUT, changed, base['updated']))


if __name__ == '__main__':
    main()
