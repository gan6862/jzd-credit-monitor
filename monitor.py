# -*- coding: utf-8 -*-
"""
江中大青年 · 学分活动监控（云端版 / 本机版通用，零第三方依赖，仅用 Python 标准库）

原理：
  1. 带 Cookie 会话访问搜狗微信，搜索目标公众号的文章列表（可拿到发布时间）
  2. 对新文章，解析搜狗跳转链接得到真实的 mp.weixin.qq.com 地址，并抓取全文
  3. 全文含「学分」→ 正则抽取学分类型/数量/起止日期 → Server酱 推送到微信

用法：
  SENDKEY=SCTxxxx  STATE_FILE=./seen.json  python monitor.py
环境变量：
  SENDKEY      Server酱 SendKey（必填）
  STATE_FILE   去重状态文件（可选；不设则按 WINDOW_HOURS 时间窗去重）
  WINDOW_HOURS 新文章时间窗（默认 24 小时）
"""
import os
import re
import sys
import json
import time
import html
import http.cookiejar
import urllib.parse
import urllib.request

SENDKEY = os.environ.get("SENDKEY", "").strip()
STATE_FILE = os.environ.get("STATE_FILE", "").strip()
WINDOW_HOURS = float(os.environ.get("WINDOW_HOURS", "24"))
ACCOUNTS = ("江中青年", "江中大青年")     # 搜狗上显示名为「江中青年」
KEYWORD = "学分"
QUERIES = ["江中青年", "江中大青年"]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
SOGOU_HOME = "https://weixin.sogou.com/"


class Session:
    """带 Cookie 的会话：搜狗反爬会检查 SNUID 等 cookie，必须先访问首页预热"""

    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        try:
            self.get(SOGOU_HOME)
        except Exception as e:
            print(f"[warn] 首页预热失败: {e}")

    def get(self, url, referer=None, timeout=20):
        headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
        if referer:
            headers["Referer"] = referer
        with self.op.open(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore")


# ---------- 通用工具 ----------
def strip_tags(s):
    s = re.sub(r"<script[\s\S]*?</script>", " ", s)
    s = re.sub(r"<style[\s\S]*?</style>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def parse_time(block):
    m = re.search(r"timeConvert\('(\d+)'\)", block)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    txt = strip_tags(block)
    now = time.time()
    m = re.search(r"(\d+)\s*小时前", txt)
    if m:
        return now - int(m.group(1)) * 3600
    m = re.search(r"(\d+)\s*分钟前", txt)
    if m:
        return now - int(m.group(1)) * 60
    if "今天" in txt:
        return now - 3600
    m = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", txt)
    if m:
        try:
            return time.mktime(time.strptime(f"{m.group(1)}-{m.group(2)}-{m.group(3)}", "%Y-%m-%d"))
        except Exception:
            return None
    return None


# ---------- 1. 搜索文章列表 ----------
def fetch_articles(sess, query):
    url = "https://weixin.sogou.com/weixin?type=2&query=" + urllib.parse.quote(query)
    try:
        page = sess.get(url)
    except Exception as e:
        print(f"[warn] 搜索失败 {query}: {e}")
        return []
    if "VerifyCode" in page or "antispider" in page:
        print(f"[warn] 搜索被验证码拦截: {query}")
        return []
    out = []
    for block in page.split('id="sogou_vr_11002601_box_')[1:]:
        m = re.search(r'<h3>[\s\S]*?<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>', block)
        if not m:
            continue
        link, title = html.unescape(m.group(1)), strip_tags(m.group(2))
        link = re.sub(r"\s+", "%20", link)          # 搜狗返回的 href 可能含未转义空格
        if link.startswith("/link"):
            link = "https://weixin.sogou.com" + link
        sn = re.search(r'<p class="txt-info"[^>]*>([\s\S]*?)</p>', block)
        snippet = strip_tags(sn.group(1)) if sn else ""
        account = ""
        for pat in (r'<span class="all-time-y2"[^>]*>([\s\S]*?)</span>',
                    r'id="weixin_account[^"]*"[^>]*>([\s\S]*?)</a>'):
            am = re.search(pat, block)
            if am:
                account = strip_tags(am.group(1))
                break
        sp = re.search(r'<div class="s-p"[^>]*>([\s\S]*?)</div>', block)
        out.append({"title": title, "sogou_link": link, "snippet": snippet,
                    "account": account, "pub_ts": parse_time(sp.group(1)) if sp else None})
    return out


# ---------- 2. 解析真实文章地址并抓全文 ----------
def resolve_and_fetch(sess, sogou_link):
    """返回 (mp_url, 全文文本)；失败返回 (None, '')"""
    try:
        body = sess.get(sogou_link, referer=SOGOU_HOME)
    except Exception as e:
        print(f"[warn] 跳转失败: {e}")
        return None, ""
    parts = re.findall(r"url\s*\+=?\s*'([^']*)'", body)
    if not parts:
        return None, ""
    mp = "".join(parts).replace("@", "")
    if "mp.weixin.qq.com" not in mp:
        return None, ""
    try:
        sess.get(mp, referer=SOGOU_HOME)
    except Exception:
        pass
    try:
        raw = sess.get(mp, referer=SOGOU_HOME)
    except Exception as e:
        print(f"[warn] 抓全文失败: {e}")
        return mp, ""
    return mp, strip_tags(raw)


# ---------- 3. 字段抽取 ----------
TYPE_WORDS = ("思想政治", "社会实践", "志愿服务", "志愿", "创新创业", "学术科研",
              "文体活动", "技能特长", "劳动教育", "第二课堂")


def _windows(text, kw=KEYWORD, w=45):
    """返回「学分」一词前后各 w 字的上下文片段，避免全文乱匹配"""
    t = text.replace(" ", "")
    return [t[max(0, i - w): i + w] for i in (m.start() for m in re.finditer(kw, t))]


def extract_fields(text):
    wins = _windows(text)
    t = text.replace(" ", "")

    # 学分类型：只在学分附近的上下文里找类型词
    ctype = "原文未明确"
    for win in wins:
        hit = None
        for w in TYPE_WORDS:
            if w in win:
                hit = w + "学分"
                break
        if hit:
            ctype = hit
            break
    if ctype == "原文未明确" and "第二课堂" in t:
        ctype = "第二课堂学分（类别未明确）"

    # 学分数量：优先最可靠的「X学分」，其次「学分X」，最后上下文内的「X分」
    qty = "原文未明确"
    for win in wins:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:个)?学分", win)
        if m:
            qty = m.group(1) + " 分"
            break
        m = re.search(r"学分\s*(?:为|共)?\s*(\d+(?:\.\d+)?)", win)
        if m:
            qty = m.group(1) + " 分"
            break
        m = re.search(r"(?:获|获得|可获|给予|加|记)\s*(\d+(?:\.\d+)?)\s*分", win)
        if m:
            qty = m.group(1) + " 分"
            break

    # 日期：从学分/报名/截止/活动 等关键词的上下文中取
    ctx = wins + [t[max(0, i - 30): i + 30]
                  for i in (m.start() for m in re.finditer("报名|截止|活动(?:时间|开始)|举办", t))]
    found = []
    for c in ctx:
        for m in re.finditer(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", c):
            d = f"{m.group(1)}月{m.group(2)}日"
            if d not in found:
                found.append(d)
    start = found[0] if found else "原文未明确"
    end = "原文未明确"
    m = re.search(r"截止[^\d]{0,10}(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", t)
    if m:
        end = f"{m.group(1)}月{m.group(2)}日"
    elif len(found) >= 2:
        end = found[-1]

    # 摘录含学分的关键句
    quote = "（无）"
    for seg in re.split(r"[。；\n]", text):
        if KEYWORD in seg and len(seg) > 6:
            quote = seg.strip()[:80]
            break
    return ctype, qty, start, end, quote


# ---------- 4. 推送 ----------
def push(title, desp):
    if not SENDKEY:
        print("[error] 未配置 SENDKEY")
        return False
    url = f"https://sctapi.ftqq.com/{SENDKEY}.send"
    data = urllib.parse.urlencode({"title": title, "desp": desp}).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", "ignore")
        ok = ("SUCCESS" in body) or ('"errno":0' in body.replace(" ", ""))
        print(("[ok] 推送成功 " if ok else "[error] 推送失败 ") + body[:100])
        return ok
    except Exception as e:
        print(f"[error] 推送异常: {e}")
        return False


# ---------- 主流程 ----------
def main():
    if not SENDKEY:
        print("[error] 缺少环境变量 SENDKEY，退出")
        return
    state = {"seen": [], "last_run": 0}
    if STATE_FILE and os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            pass
    seen = set(state.get("seen", []))

    sess = Session()
    articles = []
    for q in QUERIES:
        articles += fetch_articles(sess, q)
        time.sleep(1)

    uniq = {}
    for a in articles:
        uniq.setdefault(a["sogou_link"], a)
    print(f"[info] 搜索到 {len(uniq)} 条结果")

    now = time.time()
    pushed = 0
    for link, a in uniq.items():
        if not any(acc in (a["account"] or "") for acc in ACCOUNTS):   # 只认来源字段
            continue
        if a["pub_ts"] is None or (now - a["pub_ts"]) / 3600 > WINDOW_HOURS:
            continue
        if STATE_FILE and link in seen:
            continue

        print(f"[info] 新文章：{a['title'][:30]}（{(now - a['pub_ts']) / 3600:.1f} 小时前）")
        mp, text = resolve_and_fetch(sess, link)
        time.sleep(1)

        if not text:
            # 全文抓取失败（可能撞验证码）→ 绝不记为已处理，下一小时自动重试
            print(f"[warn] 全文获取失败，下一轮重试：{a['title'][:24]}")
            continue

        if KEYWORD not in text:
            if STATE_FILE:
                seen.add(link)          # 确认无学分，才标记为已处理
            continue

        ctype, qty, start, end, quote = extract_fields(text)
        primary = mp or link
        desp = "\n\n".join([
            f"**检测到「学分」关键词**：{a['title']}",
            "",
            f"👉 [点击直达原文]({primary})",
            f"备用：[按标题搜索](https://weixin.sogou.com/weixin?type=2&query={urllib.parse.quote(a['title'])})",
            "",
            f"**含「学分」的原文摘录**：{quote}",
            "",
            "---",
            f"自动解析（仅供参考，以原文为准）",
            f"- 学分类型：{ctype}",
            f"- 学分数量：{qty}",
            f"- 活动开始：{start}",
            f"- 活动截止：{end}",
        ])
        if push(f"学分提醒：{a['title'][:18]}", desp):
            pushed += 1
            if STATE_FILE:
                seen.add(link)          # 推送成功才记已处理
        else:
            print(f"[warn] 推送失败，下一轮重试：{a['title'][:24]}")

    if STATE_FILE:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"seen": sorted(seen)[-500:], "last_run": int(now)}, f,
                      ensure_ascii=False, indent=2)
    print(f"[info] 本次推送 {pushed} 条")


if __name__ == "__main__":
    main()


def main_handler(event, context):        # 腾讯云函数 SCF 入口
    main()
    return "done"
