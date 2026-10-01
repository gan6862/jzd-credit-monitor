# -*- coding: utf-8 -*-
"""
江中大青年 · 学分活动监控（云端版 / 本机版通用，零第三方依赖，仅用 Python 标准库）

原理：
  1. 带 Cookie 会话访问搜狗微信，用「账号名 × 活动泛词」多组关键词检索文章（可拿到发布时间）
  2. 对新文章，解析搜狗跳转链接得到真实的 mp.weixin.qq.com 地址，并抓取全文
  3. 全文（或标题/摘要）含「学分」→ 抽取学分类型/数量/起止日期 → Server酱 推送到微信

用法：
  SENDKEY=SCTxxxx  STATE_FILE=./seen.json  python monitor.py
环境变量：
  SENDKEY      Server酱 SendKey（必填）
  STATE_FILE   去重状态文件（可选；不设则不去重）
  WINDOW_HOURS 新文章时间窗（默认 72 小时，兼顾搜狗收录延迟）
               注意：未设 STATE_FILE 时窗口会自动收窄为 6 小时，否则同一篇文章会被反复推送
  DRY_RUN      设为 1 时只判定不推送、不写状态（用于验证）

------------------------------------------------------------------
v2 修复记录（2026-10-01，针对「我和国旗合个影」照片征集活动漏报）
------------------------------------------------------------------
A. 发现层（本次漏报主因）
   旧版只用「江中青年」「江中大青年」两个词检索。搜狗 type=2 是按标题/正文相关度
   召回，而公众号的活动类文章标题常常**不含公众号名**——例如《定格中国红！"我和
   国旗合个影"照片征集来啦~》，两个词都搜不到 → 该文从未进入候选 → 漏报。
   现改为「账号名 × 活动泛词」组合词表（学分/征集/报名/活动/招新…），
   检索后再用 account 字段做来源二次过滤，保证只认目标公众号。

B. 时间窗层
   旧版 WINDOW_HOURS=24。搜狗收录存在延迟，实测该文发布 44 小时后才可被检索到，
   即便被召回也会被 24h 窗口直接丢弃。现默认放宽到 72h，
   重复风险由 seen 指纹去重兜住（晚到不漏、重复不推）。

C. 判定层
   旧版只认全文里的字面「学分」。全文抓取可能被截断或失败（本次实测正文仅抓到
   1058 字，而该文的学分信息恰好在前半段，属侥幸）。现增加「标题/搜狗摘要含学分」
   兜底判定，并在推送中标出判定依据，降低截断导致的漏报。

D. 记账层（隐蔽的永久漏报）
   旧版只要抓到的文本不含「学分」，就直接写入 seen。若抓到的是验证码页/异常页/
   残缺页（文本非空但无正文），会被永久标记为"已确认无学分"→ 永久漏报。
   现增加正文质量校验（长度 + 异常页特征词），只有确认是真实正文才允许记账；
   抓取失败或质量不合格 → 不记账，下一轮自动重试。

E. 去重键
   搜狗跳转链接带一次性 token，逐轮变化会让去重失效（重复推送）。
   现改用「标题归一化指纹」作为去重键，并兼容历史 sogou_link 记录。

F. seen 回写
   旧版 sorted(seen)[-500:] 按字典序截断，会丢弃记录导致重复推送。现改为保序保留。

G. 字段抽取
   学分类型词表补充「体育艺术/文化艺术/美育/素质拓展」；
   截止日期优先取「征集/提交/报名」语境，避免把"作品展出时间"误判为截止时间。

------------------------------------------------------------------
v2.1 加固（2026-10-01，按"只要出现「学分」就必须提醒"的标准再审一遍）
------------------------------------------------------------------
H. 判定口径收敛为「标题 / 搜狗摘要 / 全文 任一出现「学分」即推送」，
   并把判定抽成 judge() 纯函数，便于用 selftest.py 做回归验证。
I. 来源识别兜底：搜狗偶尔拿不到 account 字段，旧逻辑会直接丢弃该条 → 漏报。
   现在 account 为空时，先抓全文，再按该号文末固定署名
   （"共青团江西中医药大学委员会"/"江中大青年"）二次确认来源。
J. 记账更保守：正文长度不足 CONFIRM_MIN_LEN（可能被截断）时不允许判定
   "确认无学分"，改为计入 pending 重试计数，连续 PENDING_MAX 轮仍无法确认才放弃，
   避免"抓到半截正文就永久跳过"的漏报。
K. 发现词表扩到 12 组（补投稿/打卡/二课/讲座等），并新增"本轮全部检索为空"告警，
   便于在搜狗改版或被封时第一时间发现。
L. seen 保序保留且上限 SEEN_MAX_KEEP 条，避免状态文件无限膨胀。
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
WINDOW_HOURS = float(os.environ.get("WINDOW_HOURS", "72"))
DRY_RUN = os.environ.get("DRY_RUN", "").strip() == "1"
# DEEP_SCAN=1 进入「每日深度巡检」模式：更多关键词 + 翻页 + 7 天窗口，
# 用于兜住常规每小时扫描可能漏掉的文章（依赖 STATE_FILE 去重，不会重复推送）
DEEP_SCAN = os.environ.get("DEEP_SCAN", "").strip() == "1"

ACCOUNTS = ("江中青年", "江中大青年")     # 搜狗上显示名为「江中青年」，两个名字都认
KEYWORD = "学分"
# 「账号名 × 活动泛词」组合：活动类标题往往不含公众号名，必须用泛词维度兜住
QUERIES = [
    "江中青年",
    "江中大青年",
    "江中大 学分",
    "江中青年 学分",
    "江中大 征集",
    "江中大 报名",
    "江中大 活动",
    "江中大 招新",
    "江中大 投稿",
    "江中大 打卡",
    "江中大 二课",
    "江中大 讲座",
]
# 深度巡检（DEEP_SCAN=1，每天一次）额外追加的词：更宽、更杂，用于兜住常规扫描漏掉的文章
DEEP_EXTRA_QUERIES = [
    "江中", "江中大", "江中医", "江西中医药大学 团委",
    "江中大 国庆", "江中大 中秋", "江中大 志愿者", "江中 国旗班",
    "江中大 社团", "江中大 比赛", "江中大 评选", "江中大 投票",
]
DEEP_PAGES = (1, 2)          # 深度巡检每词翻到第 2 页（常规扫描只取第 1 页）
DEEP_WINDOW_HOURS = 168      # 深度巡检覆盖最近 7 天
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
SOGOU_HOME = "https://weixin.sogou.com/"

# 该号文章文末固定出现的署名，用于 account 字段缺失时二次确认来源
ORG_MARKS = ("共青团江西中医药大学委员会", "江中大青年", "江中青年工作室",
             "江西中医药大学团委", "江中青年 在您身边")

MIN_ARTICLE_LEN = 200          # 正文小于此长度视为抓取失败
CONFIRM_MIN_LEN = 600          # 正文达到此长度，才允许判定「确认无学分」并记账
PENDING_MAX = 3                # 可疑正文最多重试几轮，超过才放弃（避免永久重试）
SEEN_MAX_KEEP = 2000           # seen 最多保留条数（按处理顺序保留最近若干条）
PUSHED_LOG_MAX = 200           # 已推送文章标题清单最多保留条数（供交叉核对读取防重）
BAD_MARKERS = ("环境异常", "去验证", "验证码", "antispider", "参数错误",
               "该内容已被发布者删除", "请在微信客户端打开", "此内容因违规无法查看",
               "系统繁忙", "访问过于频繁", "此内容发送失败")


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


def fingerprint(title):
    """标题归一化指纹：搜狗会在标题里插空格、加高亮，需去掉空白与标点后再比对"""
    return re.sub(r"[\s\u3000|｜\-—·,，。.、:：!！?？\"'“”‘’()（）\[\]【】~～]+", "", title or "")


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
def fetch_articles(sess, query, page=1):
    url = "https://weixin.sogou.com/weixin?type=2&query=" + urllib.parse.quote(query)
    if page > 1:
        url += f"&page={page}"
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


def is_valid_article(text):
    """正文质量校验：拦截验证码页/异常页/空页，避免把"抓取失败"误判成"确认无学分"而永久记账"""
    if not text or len(text) < MIN_ARTICLE_LEN:
        return False
    return not any(k in text for k in BAD_MARKERS)


def match_account(a):
    """来源账号是否命中目标公众号（按搜狗返回的 account 字段判断）"""
    acc = a.get("account") or ""
    return any(k in acc for k in ACCOUNTS)


def match_account_by_body(text):
    """account 字段缺失时的兜底：按该号文末固定署名判断来源"""
    return any(k in (text or "") for k in ORG_MARKS)


def judge(a, text):
    """判定是否需要推送：标题 / 搜狗摘要 / 全文 任一出现「学分」即推送。
    返回 (is_target, basis)"""
    if KEYWORD in (text or ""):
        return True, "全文"
    if KEYWORD in (a.get("title") or ""):
        return True, "标题"
    if KEYWORD in (a.get("snippet") or ""):
        return True, "搜狗摘要"
    return False, ""


def can_confirm_no_credit(text):
    """是否允许判定「确认无学分」并记账。
    只有正文既合法又足够长（不像被截断）时才允许，否则视为抓取可疑、下轮重试。"""
    return is_valid_article(text) and len(text or "") >= CONFIRM_MIN_LEN


# ---------- 3. 字段抽取 ----------
TYPE_WORDS = ("思想政治", "社会实践", "志愿服务", "志愿", "创新创业", "学术科研",
              "体育艺术", "文化艺术", "美育", "素质拓展", "文体活动", "技能特长",
              "劳动教育", "第二课堂")


def _windows(text, kw=KEYWORD, w=45):
    """返回「学分」一词前后各 w 字的上下文片段，避免全文乱匹配"""
    t = text.replace(" ", "")
    return [t[max(0, i - w): i + w] for i in (m.start() for m in re.finditer(kw, t))]


def _seg_around(text, kw, w=60):
    """返回关键词附近的一段文本"""
    t = text.replace(" ", "")
    i = t.find(kw)
    if i < 0:
        return ""
    return t[max(0, i - w): i + w]


DATE_RE = r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]"


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

    # 起始日期：优先「活动时间/征集时间」语境，其次学分/报名语境
    start = "原文未明确"
    for kw in ("活动时间", "征集时间", "作品征集", "提交时间", "报名时间", "活动开始"):
        seg = _seg_around(t, kw, 60)
        m = re.search(DATE_RE, seg)
        if m:
            start = f"{m.group(1)}月{m.group(2)}日"
            break
    if start == "原文未明确":
        ctx = wins + [_seg_around(t, k, 30) for k in ("报名", "截止", "活动", "举办")]
        found = []
        for c in ctx:
            for m in re.finditer(DATE_RE, c):
                d = f"{m.group(1)}月{m.group(2)}日"
                if d not in found:
                    found.append(d)
        if found:
            start = found[0]

    # 截止日期：先找显式「截止」，再取征集/提交/报名语境里日期区间的末端
    # （注意：不要用"作品展出时间"，那是展出日期而非参加截止日期）
    end = "原文未明确"
    m = re.search(r"截止[^\d]{0,12}" + DATE_RE, t)
    if m:
        end = f"{m.group(1)}月{m.group(2)}日"
    else:
        for kw in ("作品征集", "征集时间", "活动时间", "提交时间", "报名时间", "征集"):
            seg = _seg_around(t, kw, 60)
            # 只保留"征集/提交/报名"这一段，避免把后面的"作品展出时间"当成参加截止时间
            seg = re.split(r"作品展出|展出时间|展览|作品展示", seg)[0]
            ds = re.findall(DATE_RE, seg)
            if ds:
                end = f"{ds[-1][0]}月{ds[-1][1]}日"
                break

    # 摘录含学分的关键句
    quote = "（无）"
    for seg in re.split(r"[。；\n]", text):
        if KEYWORD in seg and len(seg) > 6:
            quote = seg.strip()[:80]
            break
    return ctype, qty, start, end, quote


# ---------- 4. 推送 ----------
def push(title, desp):
    if DRY_RUN:
        print(f"[dry-run] 本应推送：{title}")
        print("[dry-run] 推送正文如下：")
        print(desp)
        return True
    if not SENDKEY:
        print("[error] 未配置 SENDKEY")
        return False
    url = f"https://sctapi.ftqq.com/{SENDKEY}.send"
    data = urllib.parse.urlencode({"title": title, "desp": desp}).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", "ignore")
        ok = ("SUCCESS" in body) or ('"errno":0' in body.replace(" ", "")) \
            or ('"code":0' in body.replace(" ", ""))
        print(("[ok] 推送成功 " if ok else "[error] 推送失败 ") + body[:100])
        return ok
    except Exception as e:
        print(f"[error] 推送异常: {e}")
        return False


# ---------- 主流程 ----------
def main():
    state = {"seen": [], "pending": {}, "last_run": 0}
    if STATE_FILE and os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            pass
    seen_list = list(state.get("seen", []))          # 保序列表，用于回写时按处理顺序保留
    seen = set(seen_list)
    pending = dict(state.get("pending", {}) or {})   # {标题指纹: 已重试轮数}
    # 已推送文章的标题清单：供「每日交叉核对」读取，用于避免补推时重复打扰
    pushed_log = list(state.get("pushed", []) or [])

    if not SENDKEY and not DRY_RUN:
        print("[error] 缺少环境变量 SENDKEY：本轮只检查不推送（请到仓库 Settings→Secrets 配置 SENDKEY）")

    # 时间窗保护：宽窗口只在有 STATE_FILE 精确去重时才安全。
    # 无状态文件时若也用宽窗口，同一篇文章会在窗口内被反复推送，故强制收窄。
    if DEEP_SCAN:
        effective_window = DEEP_WINDOW_HOURS if STATE_FILE else min(DEEP_WINDOW_HOURS, 6)
    else:
        effective_window = WINDOW_HOURS if STATE_FILE else min(WINDOW_HOURS, 6)
    scan_queries = QUERIES + (DEEP_EXTRA_QUERIES if DEEP_SCAN else [])
    pages = DEEP_PAGES if DEEP_SCAN else (1,)
    print(f"[info] 模式={'深度巡检' if DEEP_SCAN else '常规扫描'}，"
          f"关键词 {len(scan_queries)} 组 × {len(pages)} 页，时间窗生效值={effective_window}"
          f"（去重方式：{'状态文件' if STATE_FILE else '时间窗'}）")

    sess = Session()
    articles = []
    empty_hits = 0
    for q in scan_queries:
        for p in pages:
            got = fetch_articles(sess, q, p)
            if not got:
                empty_hits += 1
            articles += got
            time.sleep(1)
    if not articles:
        print("[alert] 本轮全部检索均无结果：可能是搜狗改版、被验证码全面拦截或网络不通，请查看运行日志")
    elif empty_hits:
        print(f"[warn] 有 {empty_hits} 次检索返回 0 条（可能被验证码拦截），下次运行时自动补齐")

    uniq = {}
    for a in articles:
        uniq.setdefault(fingerprint(a["title"]), a)     # 用标题指纹去重，避免 token 变化导致重复
    print(f"[info] 搜索到 {len(articles)} 条结果（去重后 {len(uniq)} 条）")

    now = time.time()
    pushed = 0
    for fp, a in uniq.items():
        known_source = match_account(a)
        # account 字段有值但不匹配 → 明确是别的公众号，直接跳过；
        # account 为空 → 保留，等抓到全文后按文末署名二次确认，避免整篇被误丢。
        if not known_source and (a.get("account") or "").strip():
            continue
        if a["pub_ts"] is not None and (now - a["pub_ts"]) / 3600 > effective_window:
            continue
        # 去重：标题指纹（新）或搜狗链接（兼容历史记录）
        if STATE_FILE and (fp in seen or a.get("sogou_link") in seen):
            continue

        ago = f"{(now - a['pub_ts']) / 3600:.1f} 小时前" if a["pub_ts"] else "发布时间未知"
        print(f"[info] 新文章：{a['title'][:40]}（{ago}）")
        mp, text = resolve_and_fetch(sess, a["sogou_link"])
        time.sleep(1)

        if not known_source:
            if match_account_by_body(text):
                print("[info] 搜狗未返回来源账号，按文末署名确认为「江中大青年」")
            else:
                print(f"[warn] 来源无法确认，跳过：{a['title'][:30]}")
                continue

        is_target, basis = judge(a, text)
        if not is_target:
            if can_confirm_no_credit(text):
                if STATE_FILE and not DRY_RUN:
                    seen_list.append(fp)
                    seen.add(fp)
                    pending.pop(fp, None)
                print(f"[info] 正文完整且确认无学分，已记账：{a['title'][:30]}")
            else:
                cnt = pending.get(fp, 0) + 1
                reason = ("抓取失败" if not is_valid_article(text)
                          else f"正文仅 {len(text or '')} 字，疑似截断")
                if STATE_FILE and not DRY_RUN:
                    if cnt >= PENDING_MAX:
                        pending.pop(fp, None)
                        seen_list.append(fp)
                        seen.add(fp)
                        print(f"[warn] {reason}，已重试 {cnt} 轮仍无法确认，放弃以避免永久重试：{a['title'][:24]}")
                    else:
                        pending[fp] = cnt
                        print(f"[warn] {reason}，未记账，第 {cnt}/{PENDING_MAX} 轮，下一轮重试：{a['title'][:24]}")
                else:
                    print(f"[warn] {reason}，下一轮重试：{a['title'][:24]}")
            continue

        print(f"[info] 命中「学分」（判定依据：{basis}）→ 准备推送")
        # 字段抽取优先用全文；仅标题/摘要命中时，退回用标题+摘要抽取
        src = text if basis == "全文" else f"{a['title']} {a.get('snippet') or ''}"
        ctype, qty, start, end, quote = extract_fields(src)
        if basis != "全文":
            quote = (a.get("snippet") or "").strip()[:80] or quote
        primary = mp or a.get("sogou_link")
        desp = "\n\n".join([
            f"**检测到「学分」关键词**（判定依据：{basis}）：{a['title']}",
            "",
            f"👉 [点击直达原文]({primary})",
            f"备用：[按标题搜索](https://weixin.sogou.com/weixin?type=2&query={urllib.parse.quote(a['title'])})",
            "",
            f"**含「学分」的原文摘录**：{quote}",
            "",
            "---",
            "自动解析（仅供参考，以原文为准）",
            f"- 学分类型：{ctype}",
            f"- 学分数量：{qty}",
            f"- 活动开始：{start}",
            f"- 活动截止：{end}",
        ])
        if push(f"学分提醒：{a['title'][:20]}", desp):
            pushed += 1
            if STATE_FILE and not DRY_RUN:
                seen_list.append(fp)
                seen.add(fp)                # 推送成功才记已处理
                pending.pop(fp, None)
                pushed_log.append({"fp": fp, "title": a["title"], "ts": int(now)})
        else:
            print(f"[warn] 推送失败，下一轮重试：{a['title'][:24]}")

    if STATE_FILE and not DRY_RUN:
        # 清理已不在本轮结果中的重试记录，防止 pending 无限膨胀
        pending = {k: v for k, v in pending.items() if k in uniq}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"seen": seen_list[-SEEN_MAX_KEEP:], "pending": pending,
                       "pushed": pushed_log[-PUSHED_LOG_MAX:], "last_run": int(now)},
                      f, ensure_ascii=False, indent=2)
    print(f"[info] 本次推送 {pushed} 条")


if __name__ == "__main__":
    main()


def main_handler(event, context):        # 腾讯云函数 SCF 入口
    main()
    return "done"
