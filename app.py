import os
import json
import hashlib
import uuid
import platform
import time
from datetime import datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS

try:
    from curl_cffi import requests as cf_requests
    HAS_CURL_CFFI = True
    print("[启动] curl_cffi 可用，将使用浏览器指纹模式（可绕过 Cloudflare）")
except ImportError:
    import requests as cf_requests
    HAS_CURL_CFFI = False
    print("[启动] curl_cffi 未安装，使用普通 requests")
    print("[提示] 安装命令：pip install curl_cffi")

app = Flask(__name__)
CORS(app)
# 始终使用 app.py 所在目录的 config.json，避免任务计划启动时工作目录不对
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
SNAPSHOT_FILE = os.path.join(BASE_DIR, "bookmark_snapshot.json")


def get_chrome_bookmarks_path():
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA", "")
        return os.path.join(base, "Google", "Chrome", "User Data", "Default", "Bookmarks")
    elif system == "Darwin":
        return os.path.expanduser("~/Library/Application Support/Google/Chrome/Default/Bookmarks")
    else:
        return os.path.expanduser("~/.config/google-chrome/Default/Bookmarks")


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {"monitors": [], "cookies": {}, "selector_rules": {}}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        print(f"[警告] config.json 读取失败: {e}，使用空配置")
        return {"monitors": [], "cookies": {}, "selector_rules": {}}
    cfg.setdefault("monitors", [])
    cfg.setdefault("cookies", {})
    cfg.setdefault("selector_rules", {})
    # 兼容旧版本：如果 config 里有 bookmark_snapshot 则迁移到单独文件
    if "bookmark_snapshot" in cfg:
        try:
            _save_snapshot(cfg.pop("bookmark_snapshot"))
            save_config(cfg)
            print("[迁移] bookmark_snapshot 已从 config.json 迁移到 bookmark_snapshot.json")
        except Exception as e:
            print(f"[警告] 迁移 bookmark_snapshot 失败: {e}")
            cfg.pop("bookmark_snapshot", None)
    return cfg


def _load_snapshot():
    if not os.path.exists(SNAPSHOT_FILE):
        return {}
    try:
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_snapshot(data):
    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def save_config(data):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_hostname(url):
    try:
        return urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return ""


def normalize_rule_key(key):
    """规范化规则 key：去除协议头，统一小写，去首尾空格和末尾斜杠。"""
    k = key.lower().strip()
    for prefix in ("https://", "http://"):
        if k.startswith(prefix):
            k = k[len(prefix):]
    return k.rstrip("/")


def url_to_match_str(url):
    """将 URL 转为用于匹配的字符串：hostname（去 www.）+ path（去末尾斜杠，去查询串）。"""
    try:
        p = urlparse(url)
        host = p.netloc.lower().split(":")[0]
        if host.startswith("www."):
            host = host[4:]
        path = p.path.rstrip("/")
        return host + path
    except Exception:
        return ""


def _best_rule(url, rules):
    """返回 (best_key, best_val) 最精确匹配（key 最长者优先）。"""
    if not rules:
        return "", {}
    match_str = url_to_match_str(url)
    if not match_str:
        return "", {}
    best_key, best_val = "", {}
    for key, val in rules.items():
        k = normalize_rule_key(key)
        # 去掉规则 key 里的 www. 前缀
        if k.startswith("www."):
            k = k[4:]
        key_host = k.split("/")[0]
        url_host = match_str.split("/")[0]
        # hostname 必须完全一致（或是子域名）
        if url_host != key_host and not url_host.endswith("." + key_host):
            continue
        # 路径前缀匹配：
        #   - 纯域名规则（k == key_host）匹配该域名下所有 URL
        #   - 带路径规则（k 含 /）要求 match_str 等于 k 或以 k+"/" 开头
        if k == key_host:
            matched = True
        else:
            matched = (match_str == k or match_str.startswith(k + "/"))
        if matched and len(k) > len(best_key):
            best_key = k
            best_val = val if isinstance(val, dict) else {"selector": val or "", "delay": 2, "use_playwright": False}
    return best_key, best_val


def match_selector_rule(url, rules):
    """从规则表找最精确匹配的选择器。
    规则 key 支持纯域名（site.com）或域名+路径前缀（site.com/path），越精确优先。
    """
    _, val = _best_rule(url, rules)
    if not val:
        return ""
    return val.get("selector", "") if isinstance(val, dict) else (val or "")


def match_rule_obj(url, rules):
    """返回匹配的完整规则对象（含 delay / use_playwright），最精确优先。"""
    _, val = _best_rule(url, rules)
    return val


def parse_bookmark_node(node, path=None):
    if path is None:
        path = []
    node_type = node.get("type") or "folder"
    if node_type == "url":
        return {"type": "url", "name": node.get("name", ""), "url": node.get("url", ""), "folder_path": path}
    elif node_type == "folder":
        folder_name = node.get("name", "")
        current_path = path + [folder_name]
        children = []
        for child in node.get("children", []):
            parsed = parse_bookmark_node(child, current_path)
            if parsed:
                children.append(parsed)
        return {"type": "folder", "name": folder_name, "folder_path": path, "children": children}
    return None


def flatten_bookmarks(tree):
    """把书签树展平为 {url: {name, folder_path}} 字典，用于快照对比。"""
    result = {}
    def walk(node):
        if node.get("type") == "url":
            result[node["url"]] = {"name": node["name"], "folder_path": node.get("folder_path", [])}
        for child in node.get("children", []):
            walk(child)
    for node in tree:
        walk(node)
    return result


def get_cookie_header_for(url, cookie_store=None):
    try:
        hostname = get_hostname(url)
        if cookie_store is None:
            cookie_store = load_config().get("cookies", {})
        print(f"[Cookie] 查找 {hostname}，已保存域名：{list(cookie_store.keys())}")
        for stored_domain, cookie_str in cookie_store.items():
            stored = stored_domain.lower().strip()
            if hostname == stored or hostname.endswith("." + stored) or stored.endswith("." + hostname):
                print(f"[Cookie] ✅ 匹配成功：{hostname} → {stored}")
                return cookie_str
        print(f"[Cookie] ❌ 未找到匹配：{hostname}")
    except Exception as e:
        print(f"[Cookie] 异常：{e}")
    return ""


def extract_lines(el):
    """
    智能提取元素内容为行列表，每行代表一个"条目"。
    策略：
    1. 找出直接子 Tag 中出现次数最多的标签名（主导标签）
    2. 若主导标签占比 >= 50% 且数量 >= 3，认为是列表容器，
       对每个主导标签子元素各提取一行（递归处理嵌套）
    3. 若子节点不构成列表，尝试递归向下找列表结构（最多2层）
    4. 实在找不到列表则整体 get_text 作为一行
    """
    from collections import Counter
    from bs4 import Tag

    def _get_text(node):
        return node.get_text(separator=" ", strip=True)

    def _try_extract(node, depth=0):
        child_tags = [c for c in node.children if isinstance(c, Tag)]
        if not child_tags:
            t = _get_text(node)
            return [t] if t and len(t) >= 3 else [], False

        tag_counts = Counter(c.name for c in child_tags)
        dominant_tag, dominant_count = tag_counts.most_common(1)[0]
        ratio = dominant_count / len(child_tags)

        if dominant_count >= 3 and ratio >= 0.5:
            # 找到列表结构
            lines = []
            for child in child_tags:
                if child.name == dominant_tag:
                    sub, _ = _try_extract(child, depth + 1)
                    if sub:
                        lines.append("  ".join(sub))
                else:
                    # 非主导子节点（thead/caption/p 等）整体保留
                    t = _get_text(child)
                    if t and len(t) >= 3:
                        lines.append(t)
            lines = [l for l in lines if l and len(l) >= 3]
            return lines, True
        elif depth < 3:
            # 非列表容器，尝试递归子节点找列表
            # 优先找最多子节点的那个子 Tag
            best_child = max(child_tags,
                             key=lambda c: len([x for x in c.children if isinstance(x, Tag)]),
                             default=None)
            if best_child:
                sub_lines, found = _try_extract(best_child, depth + 1)
                if found:
                    return sub_lines, True
            # 所有子节点都没找到列表，整体提取
            t = _get_text(node)
            return [t] if t and len(t) >= 3 else [], False
        else:
            t = _get_text(node)
            return [t] if t and len(t) >= 3 else [], False

    lines, _ = _try_extract(el)
    return lines


def fetch_content_playwright(url, selector, cookie_store=None):
    """
    用 Playwright 无头浏览器抓取 JS 渲染页面。
    自动注入 Cookie，等待页面完全渲染后提取内容。
    """
    from playwright.sync_api import sync_playwright

    cookie_str = get_cookie_header_for(url, cookie_store)
    hostname   = get_hostname(url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        # 先访问域名根路径建立上下文，再注入 Cookie，再导航目标页
        base_url = f"https://{hostname}"
        page.goto(base_url, wait_until="domcontentloaded", timeout=15000)
        if cookie_str:
            cookies = []
            for part in cookie_str.split(";"):
                part = part.strip()
                if "=" in part:
                    name, _, value = part.partition("=")
                    cookies.append({
                        "name":     name.strip(),
                        "value":    value.strip(),
                        "domain":   hostname,
                        "path":     "/",
                        "sameSite": "None",
                        "secure":   True,
                    })
            if cookies:
                context.add_cookies(cookies)

        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # 等待选择器出现（最多 8s），超时则等固定 3s
        if selector.strip():
            try:
                page.wait_for_selector(selector, timeout=8000)
            except Exception:
                page.wait_for_timeout(3000)
        else:
            page.wait_for_timeout(3000)

        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    # 去噪
    for ns in ["input[type=hidden]", "style", "noscript"]:
        for el in soup.select(ns):
            el.decompose()

    if selector.strip():
        elements = soup.select(selector)
        if not elements:
            raise ValueError(f"CSS 选择器「{selector}」未匹配到任何元素（JS 渲染后仍未找到）")
        lines = []
        for el in elements:
            lines.extend(extract_lines(el))
        text = "\n".join(lines) if lines else ""
    else:
        body = soup.find("body")
        text = body.get_text(separator=" ", strip=True) if body else ""

    if not text.strip():
        raise ValueError("提取内容为空")
    return text, hashlib.md5(text.encode()).hexdigest(), 200


# 检测 Playwright 是否可用
try:
    from playwright.sync_api import sync_playwright as _pw_check
    HAS_PLAYWRIGHT = True
    print("[启动] Playwright 可用，JS 渲染页面将使用无头浏览器抓取")
except ImportError:
    HAS_PLAYWRIGHT = False
    print("[启动] Playwright 未安装，JS 渲染页面将无法抓取选择器内容")
    print("[提示] 安装命令：pip install playwright && playwright install chromium")


def fetch_content(url, selector, cookie_store=None, retries=2, use_playwright=False):
    import requests as std_requests
    cookie_str = get_cookie_header_for(url, cookie_store)
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    if cookie_str:
        headers["Cookie"] = cookie_str

    def do_request(use_curl):
        if use_curl and HAS_CURL_CFFI:
            return cf_requests.get(url, headers=headers, impersonate="chrome124", timeout=20, allow_redirects=True)
        else:
            return std_requests.Session().get(url, headers=headers, timeout=20, allow_redirects=True)

    def check_and_parse(resp):
        if resp.status_code == 404:
            raise ValueError("HTTP 404 —— 页面不存在")
        if resp.status_code == 403:
            hint = "Cookie 未设置" if not cookie_str else "Cookie 可能已过期"
            raise ValueError(f"HTTP 403 Forbidden —— {hint}")
        if resp.status_code == 401:
            raise ValueError("HTTP 401 —— 需要登录，请添加 Cookie")
        if resp.status_code == 429:
            raise ValueError("HTTP 429 —— 访问频率过高")
        resp.raise_for_status()
        if any(kw in resp.url for kw in ("/login", "/signin", "/auth", "/captcha")):
            hint = ("未设置 Cookie，请在「🍪 Cookie」中添加" if not cookie_str
                    else "Cookie 已携带但仍被重定向，可能已过期，请重新复制")
            raise ValueError(f"被重定向到登录页 ({resp.url}) —— {hint}")
        soup = BeautifulSoup(resp.text, "html.parser")

        # 移除会产生误报的动态噪音元素
        NOISE_SELECTORS = [
            "input[type=hidden]",
            "input[name*=token]", "input[name*=hash]", "input[name*=nonce]",
            "input[name*=time]",  "input[name*=rand]", "input[name*=csrf]",
            "style", "noscript",
        ]
        for ns in NOISE_SELECTORS:
            for el in soup.select(ns):
                el.decompose()

        # 检测是否为 JS 渲染页面：选择器匹配失败时，检查是否有真实内容结构
        has_content_structure = bool(soup.select(
            "article, main, .content, #content, table tr, ul li, ol li, "
            ".list, .article, .post, .thread, .item"
        ))
        raw_body_text = (soup.find("body") or soup).get_text(separator=" ", strip=True)
        is_spa = not has_content_structure

        if selector.strip():
            elements = soup.select(selector)
            if not elements and (is_spa or use_playwright):
                if HAS_PLAYWRIGHT:
                    raise _SPAError()  # 触发 Playwright 降级
                raise ValueError(
                    "此页面由 JavaScript 动态渲染，CSS 选择器无法匹配。\n"
                    "请安装 Playwright 以启用无头浏览器支持：\n"
                    "  pip install playwright\n"
                    "  playwright install chromium"
                )
            if not elements:
                raise ValueError(f"CSS 选择器「{selector}」未匹配到任何元素")
            lines = []
            for el in elements:
                lines.extend(extract_lines(el))
            text = "\n".join(lines) if lines else ""
        else:
            if is_spa:
                import re as _re
                script_data = []
                for sc in soup.find_all("script"):
                    sc_text = (sc.string or "").strip()
                    if sc_text and len(sc_text) > 50 and ('{' in sc_text or '[' in sc_text):
                        cleaned = _re.sub(r'[\x00-\x1f]', ' ', sc_text)[:2000]
                        script_data.append(cleaned)
                if script_data:
                    text = "\n---\n".join(script_data)
                else:
                    text = resp.text[:5000]
            else:
                text = raw_body_text
        if not text.strip():
            raise ValueError("提取内容为空，选择器可能有误或页面需要 JS 渲染")
        return text, hashlib.md5(text.encode()).hexdigest(), resp.status_code

    class _SPAError(Exception):
        pass

    FALLBACK = ("tls", "openssl", "invalid library", "ssl", "empty reply", "connection reset", "(52)", "(35)", "(6)")
    last_err = None
    use_curl = HAS_CURL_CFFI
    for attempt in range(retries + 1):
        try:
            return check_and_parse(do_request(use_curl))
        except _SPAError:
            # JS 渲染页面 + 有选择器 → 切换到 Playwright
            return fetch_content_playwright(url, selector, cookie_store)
        except ValueError:
            raise
        except Exception as e:
            last_err = e
            if use_curl and any(k in str(e).lower() for k in FALLBACK):
                use_curl = False
                continue
            if attempt < retries:
                time.sleep(1.5 ** attempt)
    raise ConnectionError(f"请求失败（重试 {retries} 次）：{last_err}")


# ─── Routes ──────────────────────────────────────────────────────────────────


@app.route("/api/debug-render", methods=["POST"])
def debug_render():
    """保存 Playwright 渲染后的 HTML 到临时文件，方便在浏览器里查看结构"""
    data = request.get_json()
    url  = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url 不能为空"}), 400
    if not HAS_PLAYWRIGHT:
        return jsonify({"error": "Playwright 未安装"}), 400

    config       = load_config()
    cookie_store = config.get("cookies", {})
    cookie_str   = get_cookie_header_for(url, cookie_store)
    hostname     = get_hostname(url)

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ))
            page = context.new_page()
            page.goto(f"https://{hostname}", wait_until="domcontentloaded", timeout=15000)
            if cookie_str:
                cookies = []
                for part in cookie_str.split(";"):
                    part = part.strip()
                    if "=" in part:
                        name, _, value = part.partition("=")
                        cookies.append({"name": name.strip(), "value": value.strip(),
                                        "domain": hostname, "path": "/",
                                        "sameSite": "None", "secure": True})
                if cookies:
                    context.add_cookies(cookies)
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(5000)
            html = page.content()
            browser.close()

        # 保存到临时文件
        import tempfile, os
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.html',
                                          delete=False, encoding='utf-8',
                                          dir=os.path.dirname(os.path.abspath(__file__)))
        tmp.write(html)
        tmp.close()
        fname = os.path.basename(tmp.name)
        return jsonify({"file": fname, "url": f"/debug-html/{fname}",
                        "size": len(html), "hint": "在浏览器里打开上面的 URL 查看渲染后的页面"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/debug-html/<filename>")
def serve_debug_html(filename):
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    if not os.path.exists(path) or not filename.endswith('.html'):
        return "Not found", 404
    with open(path, encoding='utf-8') as f:
        return f.read(), 200, {'Content-Type': 'text/html; charset=utf-8'}


@app.route("/api/test-selector", methods=["POST"])
def test_selector():
    """
    调试接口：抓取指定 URL，用给定选择器提取内容，返回匹配结果预览。
    请求体：{ "url": "...", "selector": "..." }
    复用 fetch_content 的完整降级逻辑（curl_cffi → requests）。
    """
    data     = request.get_json()
    url      = (data.get("url") or "").strip()
    selector = (data.get("selector") or "").strip()
    search_text   = (data.get("search_text") or "").strip()
    force_playwright = bool(data.get("force_playwright"))
    if not url:
        return jsonify({"error": "url 不能为空"}), 400

    config       = load_config()
    cookie_store = config.get("cookies", {})
    debug_info   = []

    try:
        # force_playwright：直接用 Playwright 渲染，跳过静态抓取
        if force_playwright and HAS_PLAYWRIGHT:
            from playwright.sync_api import sync_playwright
            cookie_str_pw = get_cookie_header_for(url, cookie_store)
            hostname_pw   = get_hostname(url)
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ))
                page = context.new_page()
                page.goto(f"https://{hostname_pw}", wait_until="domcontentloaded", timeout=15000)
                if cookie_str_pw:
                    cookies_pw = []
                    for part in cookie_str_pw.split(";"):
                        part = part.strip()
                        if "=" in part:
                            name, _, value = part.partition("=")
                            cookies_pw.append({"name": name.strip(), "value": value.strip(),
                                               "domain": hostname_pw, "path": "/",
                                               "sameSite": "None", "secure": True})
                    if cookies_pw:
                        context.add_cookies(cookies_pw)
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(4000)
                html_pw = page.content()
                browser.close()
            soup = BeautifulSoup(html_pw, "html.parser")
            for ns in ["script", "style", "noscript"]:
                for el in soup.select(ns):
                    el.decompose()
            debug_info.append("⚡ 使用 Playwright 渲染后提取")

            if not selector:
                body = soup.find("body")
                text = body.get_text(separator="\n", strip=True)[:1000] if body else ""
                return jsonify({"matched": 0, "hint": "未填选择器，Playwright渲染后页面前1000字符：", "preview": text.splitlines()[:20], "debug": debug_info})

            matched = soup.select(selector)
            if not matched:
                # 输出高频 class 帮助调试
                from collections import Counter as _Counter
                all_classes = []
                for tag in soup.find_all(True):
                    for cls in (tag.get("class") or []):
                        all_classes.append(cls)
                top_classes = [f".{c}（{n}）" for c, n in _Counter(all_classes).most_common()]
                debug_info.append(f"页面所有 class（共{len(_Counter(all_classes))}个）：{', '.join(top_classes)}")
                return jsonify({"matched": 0, "hint": "Playwright渲染后选择器仍未匹配，请检查选择器", "debug": debug_info, "preview": []})

            lines = []
            for el in matched:
                lines.extend(extract_lines(el) or [el.get_text(separator=" ", strip=True)[:100]])
            return jsonify({"selector_ok": True, "matched": len(matched), "lines": len(lines),
                            "preview": lines[:15], "debug": debug_info,
                            "note": "⚡ 使用Playwright渲染后提取"})

        # 用空选择器先抓页面，再单独做选择器匹配（获取原始 soup）
        import requests as std_requests
        cookie_str = get_cookie_header_for(url, cookie_store)
        headers = {
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        }
        if cookie_str:
            headers["Cookie"] = cookie_str

        FALLBACK = ("tls", "openssl", "invalid library", "ssl", "empty reply", "connection reset", "(52)", "(35)", "(6)")
        resp = None
        use_curl = HAS_CURL_CFFI
        last_err = None
        for attempt in range(3):
            try:
                if use_curl and HAS_CURL_CFFI:
                    resp = cf_requests.get(url, headers=headers, impersonate="chrome124", timeout=20, allow_redirects=True)
                else:
                    resp = std_requests.Session().get(url, headers=headers, timeout=20, allow_redirects=True)
                break
            except Exception as e:
                last_err = e
                if use_curl and any(k in str(e).lower() for k in FALLBACK):
                    use_curl = False
                    continue
                if attempt < 2:
                    time.sleep(1)
        if resp is None:
            return jsonify({"error": f"请求失败：{last_err}"}), 500

        soup = BeautifulSoup(resp.text, "html.parser")

        # SPA 检测：在去噪前，直接从原始 HTML 统计 body 内非标签文本量
        import re as _re
        body_match = _re.search(r'<body[^>]*>(.*?)</body>', resp.text, _re.DOTALL | _re.IGNORECASE)
        if body_match:
            body_html = body_match.group(1)
            # 去掉所有标签只留文本
            body_plain = _re.sub(r'<[^>]+>', '', body_html)
            body_plain = _re.sub(r'\s+', ' ', body_plain).strip()
        else:
            body_plain = ""
        body_text_len = len(body_plain)
        # SPA判断：body纯文本长度 < 5000，且没有常见内容容器（列表/文章/表格）
        has_content_structure = bool(soup.select(
            "article, main, .content, #content, table tr, ul li, ol li, "
            ".list, .article, .post, .thread, .item"
        ))
        is_spa = (body_text_len < 5000) and not has_content_structure
        print(f"[SPA检测] body纯文本长度={body_text_len}, has_content={has_content_structure}, is_spa={is_spa}, url={url[:60]}")

        for ns in ["input[type=hidden]", "script", "style", "noscript"]:
            for el in soup.select(ns):
                el.decompose()

        if not selector and not search_text:
            body = soup.find("body")
            text = body.get_text(separator="\n", strip=True)[:1000] if body else ""
            return jsonify({"matched": 0, "hint": "未填选择器，页面前1000字符：", "preview": text.splitlines()[:20]})

        # 调试信息：检查页面是否有登录态，以及关键元素是否存在
        debug_info = []
        has_login  = bool(soup.select(
            "a[href*=logout], a[href*=signout], .logout, #myinfo, "
            ".user-info, .user-name, .username, [class*=user-avatar], "
            "a[href*=profile], .sign-out, button[class*=logout]"
        ))
        cookie_used = get_cookie_header_for(url, cookie_store)
        cookie_domain = None
        for d in cookie_store:
            h = get_hostname(url)
            if h == d or h.endswith('.' + d) or d in h:
                cookie_domain = d
                break
        debug_info.append(f"Cookie：{'✅ 已匹配（' + cookie_domain + '）' if cookie_domain else '❌ 未找到匹配的 Cookie，请在「🍪 Cookie」中添加 ' + get_hostname(url)}")
        # Cookie 内容预览（只显示 name，不显示 value）
        if cookie_domain:
            raw_cookie = cookie_store.get(cookie_domain, "")
            cookie_names = [p.split("=")[0].strip() for p in raw_cookie.split(";") if "=" in p]
            debug_info.append(f"Cookie 字段名：{cookie_names[:10]}")
            debug_info.append(f"Cookie 长度：{len(raw_cookie)} 字符")
        debug_info.append(f"登录态检测：{'✅ 疑似已登录' if has_login else '⚠️ 未检测到登录标志（不影响实际效果）'}")
        # SPA 检测结果
        if is_spa:
            pw_hint = "将自动使用 Playwright 无头浏览器" if HAS_PLAYWRIGHT else "请安装 Playwright：pip install playwright && python -m playwright install chromium"
            debug_info.append(f"⚠️ JS渲染页面（body文本 {body_text_len} 字符）—— {pw_hint}")
        delform = soup.find(id="delform")
        if delform:
            from bs4 import Tag as _Tag
            direct_children = [c.name for c in delform.children if isinstance(c, _Tag)]
            debug_info.append(f"#delform 直接子标签：{direct_children[:10]}")
            trs_with_tbody  = delform.select("table tbody tr")
            trs_no_tbody    = delform.select("table tr")
            trs_any         = delform.select("tr")
            dls             = delform.select("dl")
            divs_item       = delform.select("div.item, div.thread, li")
            debug_info.append(f"table tbody tr：{len(trs_with_tbody)} 个")
            debug_info.append(f"table tr（无tbody）：{len(trs_no_tbody)} 个")
            debug_info.append(f"任意 tr：{len(trs_any)} 个")
            debug_info.append(f"dl 元素：{len(dls)} 个")
            debug_info.append(f"div.item/div.thread/li：{len(divs_item)} 个")
            trs = trs_no_tbody or trs_any
            if trs:
                from bs4 import Tag as _Tag2
                first_tr = trs[0]
                child_tags = [c.name for c in first_tr.children if isinstance(c, _Tag2)]
                debug_info.append(f"第1个 tr 子标签：{child_tags}")
                if len(trs) > 1:
                    child_tags2 = [c.name for c in trs[1].children if isinstance(c, _Tag2)]
                    debug_info.append(f"第2个 tr 子标签：{child_tags2}")
        else:
            # 通用结构探测：显示 body 下前两层标签分布
            from bs4 import Tag as _Tag
            body = soup.find("body")
            if body:
                top_tags = [c.name for c in body.children if isinstance(c, _Tag)]
                debug_info.append(f"body 直接子标签：{top_tags[:8]}")

        # ── 文本搜索模式 ──────────────────────────────────────────
        if search_text:
            # 如果是 SPA 页面或强制，先用 Playwright 渲染
            search_soup = soup
            if (is_spa or force_playwright) and HAS_PLAYWRIGHT:
                debug_info.append("🔄 JS渲染页面，使用 Playwright 渲染后搜索...")
                try:
                    from playwright.sync_api import sync_playwright
                    cookie_str_pw = get_cookie_header_for(url, cookie_store)
                    hostname_pw   = get_hostname(url)
                    with sync_playwright() as p:
                        browser = p.chromium.launch(headless=True)
                        context = browser.new_context(user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                        ))
                        page = browser.new_page()
                        # 先访问根路径建立上下文，再注入 Cookie，再导航目标页
                        page.goto(f"https://{hostname_pw}", wait_until="domcontentloaded", timeout=15000)
                        if cookie_str_pw:
                            cookies = []
                            for part in cookie_str_pw.split(";"):
                                part = part.strip()
                                if "=" in part:
                                    name, _, value = part.partition("=")
                                    cookies.append({
                                        "name": name.strip(), "value": value.strip(),
                                        "domain": hostname_pw, "path": "/",
                                        "sameSite": "None", "secure": True,
                                    })
                            if cookies:
                                context.add_cookies(cookies)
                        # 再导航到目标页面
                        page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(5000)
                        html_pw = page.content()
                        browser.close()
                    search_soup = BeautifulSoup(html_pw, "html.parser")
                    for ns in ["script", "style", "noscript"]:
                        for el in search_soup.select(ns):
                            el.decompose()
                    rendered_text = search_soup.get_text(separator=" ", strip=True)[:500]
                    debug_info.append(f"渲染后页面文本预览：{rendered_text}")
                except Exception as pw_err:
                    debug_info.append(f"❌ Playwright 渲染失败：{pw_err}")

            results = []
            for tag in search_soup.find_all(True):
                tag_text = tag.get_text(strip=True)
                if search_text.lower() in tag_text.lower() and len(tag_text) < len(search_text) * 5:
                    classes = tag.get("class") or []
                    tag_id  = tag.get("id")
                    if tag_id:
                        sel = f"#{tag_id}"
                    elif classes:
                        sel = tag.name + "." + ".".join(classes)
                    else:
                        sel = tag.name
                    parent = tag.parent
                    parent_cls = ""
                    if parent and parent.name not in ("body", "html", "[document]"):
                        p_classes = parent.get("class") or []
                        p_id = parent.get("id")
                        if p_id:
                            parent_cls = f"#{p_id} > "
                        elif p_classes:
                            parent_cls = f"{parent.name}.{'.'.join(p_classes)} > "
                    full_sel = parent_cls + sel
                    results.append({
                        "selector": full_sel,
                        "simple":   ("." + classes[0]) if classes else (f"#{tag_id}" if tag_id else tag.name),
                        "text":     tag_text[:120],
                        "tag":      tag.name,
                    })
            if results:
                return jsonify({"search_results": results[:10], "debug": debug_info})
            return jsonify({
                "search_results": [],
                "hint": f"未找到包含「{search_text}」的元素" + ("（JS渲染后仍未找到）" if is_spa else ""),
                "debug": debug_info,
            })

        matched = soup.select(selector) if selector else []
        if not matched and is_spa and selector and HAS_PLAYWRIGHT:
            # JS 渲染页面，用 Playwright 重新抓取
            debug_info.append("🔄 检测到 JS 渲染页面，切换到 Playwright 无头浏览器...")
            try:
                text, _, _ = fetch_content_playwright(url, selector, cookie_store)
                lines = text.splitlines()
                return jsonify({
                    "matched": 1,
                    "lines": len(lines),
                    "preview": lines[:20],
                    "selector_ok": True,
                    "debug": debug_info,
                    "note": "⚡ 使用 Playwright 渲染后提取",
                })
            except Exception as pw_err:
                debug_info.append(f"❌ Playwright 也失败了：{pw_err}")

        if not matched:
            import re as _re
            candidates = []
            # 策略0：去掉 tbody（BeautifulSoup 不自动补全 tbody，浏览器会）
            if "tbody" in selector:
                no_tbody = _re.sub(r'\s*>?\s*tbody\s*>?\s*', ' ', selector).strip()
                no_tbody = _re.sub(r'\s+', ' ', no_tbody)
                candidates.append(no_tbody)
            loose1 = selector.replace(" > ", " ")
            if loose1 != selector:
                candidates.append(loose1)
            parts = [p.strip() for p in selector.replace(" > ", ">").split(">")]
            if len(parts) > 1:
                last = parts[-1]
                if last in ("th", "td", "th:first-child", "td:first-child"):
                    candidates.append(" > ".join(parts[:-1]))
                    candidates.append(parts[-2] if len(parts) >= 2 else "tr")
                else:
                    candidates.append(last)
                    candidates.append(" ".join(parts[-2:]))
                if parts[-1] not in ("th", "td"):
                    candidates.append(" > ".join(parts[:-1]))

            best_sel, best_els = None, []
            for cand in candidates:
                try:
                    els = soup.select(cand)
                    if len(els) > len(best_els):
                        best_sel, best_els = cand, els
                except Exception:
                    pass

            if best_sel and best_els:
                preview_lines = []
                for el in best_els[:5]:
                    preview_lines.extend(extract_lines(el) or [el.get_text(separator=" ", strip=True)[:100]])
                return jsonify({
                    "matched": 0,
                    "hint": f"选择器未匹配，建议改用：「{best_sel}」（匹配 {len(best_els)} 个元素）",
                    "loose_selector": best_sel,
                    "preview": preview_lines[:10],
                    "debug": debug_info,
                })
            # 找不到建议时，输出页面里高频 class 名帮助调试
            from collections import Counter as _Counter
            all_classes = []
            for tag in soup.find_all(True):
                for cls in (tag.get("class") or []):
                    all_classes.append(cls)
            top_classes = [f".{c}（{n}）" for c, n in _Counter(all_classes).most_common()]
            debug_info.append(f"页面所有 class（共{len(_Counter(all_classes))}个）：{', '.join(top_classes)}")
            return jsonify({
                "matched": 0,
                "hint": "选择器未匹配任何元素，请检查选择器是否正确",
                "loose_selector": None,
                "preview": [],
                "debug": debug_info,
            })

        lines = []
        for el in matched:
            lines.extend(extract_lines(el))
        return jsonify({
            "matched":     len(matched),
            "lines":       len(lines),
            "preview":     lines[:20],
            "selector_ok": True,
            "debug":       debug_info,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/bookmarks", methods=["GET"])
def get_bookmarks():
    path = get_chrome_bookmarks_path()
    if not os.path.exists(path):
        return jsonify({"error": f"Chrome 书签文件未找到：{path}"}), 404
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    rules = load_config().get("selector_rules", {})
    tree = []
    for key in ("bookmark_bar", "other", "synced"):
        node = raw.get("roots", {}).get(key)
        if node:
            parsed = parse_bookmark_node(node)
            if parsed:
                tree.append(parsed)

    def attach_rule(node):
        if node.get("type") == "url":
            node["rule_selector"] = match_selector_rule(node["url"], rules)
        for c in node.get("children", []):
            attach_rule(c)
    for n in tree:
        attach_rule(n)
    return jsonify({"bookmarks": tree})


@app.route("/api/bookmarks/sync", methods=["GET"])
def bookmarks_sync():
    """
    对比当前 Chrome 书签与上次保存的快照，返回：
    - added:   新增的书签列表（不在快照中）
    - removed: 被删除的书签列表（快照中有、当前没有）
    同时将当前书签保存为新快照。
    removed 中正在被监控的项目同步删除。
    """
    bm_path = get_chrome_bookmarks_path()
    if not os.path.exists(bm_path):
        return jsonify({"error": "Chrome 书签文件未找到"}), 404

    with open(bm_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    tree = []
    for key in ("bookmark_bar", "other", "synced"):
        node = raw.get("roots", {}).get(key)
        if node:
            parsed = parse_bookmark_node(node)
            if parsed:
                tree.append(parsed)
    current_flat = flatten_bookmarks(tree)

    config = load_config()
    snapshot = _load_snapshot()

    # 如果是第一次（快照为空），直接保存快照，不报告差异
    if not snapshot:
        _save_snapshot(current_flat)
        return jsonify({"added": [], "removed": [], "first_run": True})

    current_urls = set(current_flat.keys())
    snapshot_urls = set(snapshot.keys())

    added_urls   = current_urls - snapshot_urls
    removed_urls = snapshot_urls - current_urls

    added = [
        {"url": u, "name": current_flat[u]["name"], "folder_path": current_flat[u]["folder_path"]}
        for u in added_urls
    ]
    removed = [
        {"url": u, "name": snapshot[u]["name"], "folder_path": snapshot[u]["folder_path"]}
        for u in removed_urls
    ]

    # 被删除的书签如果在监控列表中，自动移除
    monitored_urls = {m["url"] for m in config["monitors"]}
    if removed_urls:
        config["monitors"] = [m for m in config["monitors"] if m["url"] not in removed_urls]
        save_config(config)
        for r in removed:
            r["was_monitored"] = r["url"] in monitored_urls

    # 更新快照（单独文件，不影响 config.json）
    _save_snapshot(current_flat)

    return jsonify({
        "added": added,
        "removed": removed,
        "first_run": False
    })


@app.route("/api/monitors", methods=["GET"])
def get_monitors():
    return jsonify({"monitors": load_config().get("monitors", [])})


@app.route("/api/monitors", methods=["POST"])
def add_monitor():
    data        = request.get_json()
    name        = (data.get("name") or "").strip()
    url         = (data.get("url") or "").strip()
    selector    = (data.get("selector") or "").strip()
    folder_path = data.get("folder_path") or []
    if not name or not url:
        return jsonify({"error": "name 和 url 不能为空"}), 400
    config = load_config()
    if any(m["url"] == url for m in config["monitors"]):
        return jsonify({"error": "该 URL 已在监控列表中"}), 409
    if not selector:
        selector = match_selector_rule(url, config.get("selector_rules", {}))
    new_item = {
        "id": str(uuid.uuid4()), "name": name, "url": url, "selector": selector,
        "folder_path": folder_path, "status": "pending", "last_hash": "",
        "last_text_preview": "", "last_checked": "", "added_at": datetime.now().isoformat(),
    }
    config["monitors"].append(new_item)
    save_config(config)
    return jsonify({"monitor": new_item}), 201


@app.route("/api/monitors/batch", methods=["POST"])
def add_monitors_batch():
    data     = request.get_json()
    items    = data.get("items") or []
    selector = (data.get("selector") or "").strip()
    config        = load_config()
    rules         = config.get("selector_rules", {})
    existing_urls = {m["url"] for m in config["monitors"]}
    added, skipped = [], []
    for item in items:
        name        = (item.get("name") or "").strip()
        url         = (item.get("url") or "").strip()
        folder_path = item.get("folder_path") or []
        if not name or not url or url in existing_urls:
            if url in existing_urls:
                skipped.append(url)
            continue
        effective_selector = selector or match_selector_rule(url, rules)
        new_item = {
            "id": str(uuid.uuid4()), "name": name, "url": url,
            "selector": effective_selector, "folder_path": folder_path,
            "status": "pending", "last_hash": "", "last_text_preview": "",
            "last_checked": "", "added_at": datetime.now().isoformat(),
        }
        config["monitors"].append(new_item)
        existing_urls.add(url)
        added.append(new_item)
    save_config(config)
    return jsonify({"added": len(added), "skipped": len(skipped), "monitors": config["monitors"]}), 201


@app.route("/api/monitors/<string:monitor_id>", methods=["PATCH"])
def update_monitor(monitor_id):
    """编辑监控项的 selector 或 name，修改后重置 hash 重新采集基准。"""
    data   = request.get_json()
    config = load_config()
    for m in config["monitors"]:
        if m["id"] == monitor_id:
            if "selector" in data:
                m["selector"] = (data["selector"] or "").strip()
            if "name" in data:
                new_name = (data["name"] or "").strip()
                if new_name:
                    m["name"] = new_name
            m["last_hash"] = ""
            m["status"]    = "pending"
            save_config(config)
            return jsonify({"monitor": m})
    return jsonify({"error": "未找到该监控项"}), 404


@app.route("/api/monitors/<string:monitor_id>", methods=["DELETE"])
def delete_monitor(monitor_id):
    config = load_config()
    before = len(config["monitors"])
    config["monitors"] = [m for m in config["monitors"] if m["id"] != monitor_id]
    if len(config["monitors"]) == before:
        return jsonify({"error": "未找到该监控项"}), 404
    save_config(config)
    return jsonify({"success": True})


@app.route("/api/monitors/<string:monitor_id>/check", methods=["POST"])
def check_one_monitor(monitor_id):
    config  = load_config()
    monitors = config.get("monitors", [])
    m = next((x for x in monitors if x["id"] == monitor_id), None)
    if not m:
        return jsonify({"error": "未找到监控项"}), 404
    cookie_store = config.get("cookies", {})
    raw_rules    = config.get("selector_rules", {})
    rule_obj     = match_rule_obj(m["url"], raw_rules)
    use_pw       = rule_obj.get("use_playwright", False) if rule_obj else False
    try:
        m["status"] = "checking"
        save_config(config)
        text, new_hash, _ = fetch_content(m["url"], m.get("selector", ""), cookie_store=cookie_store, use_playwright=use_pw)
        old_hash = m.get("last_hash", "")
        old_text = m.get("last_text", "")
        if not old_hash:
            m["status"] = "unchanged"
            m["last_text_preview"] = f"[首次采集 {len(text)} 字符] " + text[:200]
            m["diff"] = ""
        elif new_hash != old_hash:
            m["status"] = "changed"
            m["last_text_preview"] = text[:200]
            m["diff"] = build_diff(old_text, text)
        else:
            m["status"] = "unchanged"
        m["last_hash"]    = new_hash
        m["last_text"]    = text
        m["last_checked"] = datetime.utcnow().isoformat()
    except Exception as e:
        m["status"] = "error"
        m["last_text_preview"] = str(e)[:200]
        m["last_checked"] = datetime.utcnow().isoformat()
    save_config(config)
    return jsonify({"monitor": m})



def reset_monitor(monitor_id):
    config = load_config()
    for m in config["monitors"]:
        if m["id"] == monitor_id:
            m["status"]    = "unchanged"
            m["last_hash"] = ""
            m["last_text"] = ""
            m["diff"]      = ""
            save_config(config)
            return jsonify({"monitor": m})
    return jsonify({"error": "未找到该监控项"}), 404


@app.route("/api/monitors/reset-all", methods=["POST"])
def reset_all_monitors():
    """清空所有监控项的 hash/text/diff，下次检查重新建立基准。"""
    config = load_config()
    for m in config["monitors"]:
        m["last_hash"] = ""
        m["last_text"] = ""
        m["diff"]      = ""
        m["status"]    = "pending"
    save_config(config)
    return jsonify({"reset": len(config["monitors"])})


@app.route("/api/monitors/batch-delete", methods=["POST"])
def batch_delete_monitors():
    data          = request.get_json()
    ids_to_delete = set(data.get("ids") or [])
    if not ids_to_delete:
        return jsonify({"error": "ids 不能为空"}), 400
    config = load_config()
    before = len(config["monitors"])
    config["monitors"] = [m for m in config["monitors"] if m["id"] not in ids_to_delete]
    save_config(config)
    return jsonify({"deleted": before - len(config["monitors"]), "monitors": config["monitors"]})


# ── 域名选择器规则 ──────────────────────────────────────────────────────────

@app.route("/api/selector-rules", methods=["GET"])
def get_selector_rules():
    rules = load_config().get("selector_rules", {})
    # 统一返回对象格式，兼容旧的字符串格式
    normalized = {}
    for k, v in rules.items():
        if isinstance(v, dict):
            normalized[k] = v
        else:
            normalized[k] = {"selector": v, "delay": 2}
    return jsonify({"rules": normalized})


@app.route("/api/selector-rules", methods=["POST"])
def save_selector_rule():
    data           = request.get_json()
    domain         = (data.get("domain") or "").strip().lower()
    selector       = (data.get("selector") or "").strip()
    delay          = int(data.get("delay") or 2)
    use_playwright = bool(data.get("use_playwright"))
    if not domain:
        return jsonify({"error": "domain 不能为空"}), 400
    config = load_config()
    if selector or delay != 2 or use_playwright:
        config["selector_rules"][domain] = {"selector": selector, "delay": delay, "use_playwright": use_playwright}
    else:
        config["selector_rules"].pop(domain, None)
    save_config(config)
    return jsonify({"rules": {
        k: (v if isinstance(v, dict) else {"selector": v, "delay": 2, "use_playwright": False})
        for k, v in config["selector_rules"].items()
    }})


@app.route("/api/selector-rules/<path:domain>", methods=["DELETE"])
def delete_selector_rule(domain):
    config = load_config()
    if domain not in config.get("selector_rules", {}):
        return jsonify({"error": "未找到该规则"}), 404
    del config["selector_rules"][domain]
    save_config(config)
    return jsonify({"rules": config["selector_rules"]})


@app.route("/api/selector-rules/batch-apply", methods=["POST"])
def batch_apply_selector_rule():
    """将某条规则批量应用到已有匹配的监控项，覆盖其 selector。"""
    data     = request.get_json()
    domain   = (data.get("domain") or "").strip().lower()
    selector = (data.get("selector") or "").strip()
    if not domain:
        return jsonify({"error": "domain 不能为空"}), 400
    config  = load_config()
    # selector 可能来自前端直接传，也可能需要从规则里取
    if not selector:
        rule = config.get("selector_rules", {}).get(domain, {})
        selector = rule.get("selector", "") if isinstance(rule, dict) else (rule or "")
    updated = 0
    rule_key = normalize_rule_key(domain)
    if rule_key.startswith("www."):
        rule_key = rule_key[4:]
    key_host = rule_key.split("/")[0]
    is_domain_only = (rule_key == key_host)

    for m in config["monitors"]:
        m_match = url_to_match_str(m["url"])
        m_host  = m_match.split("/")[0]
        # hostname 必须匹配
        if m_host != key_host and not m_host.endswith("." + key_host):
            continue
        # 路径前缀匹配
        if is_domain_only:
            path_match = True
        else:
            path_match = (m_match == rule_key or m_match.startswith(rule_key + "/"))
        if path_match:
            m["selector"]  = selector
            m["last_hash"] = ""
            m["status"]    = "pending"
            updated += 1
    save_config(config)
    return jsonify({"updated": updated, "monitors": config["monitors"]})


# ─── Diff ────────────────────────────────────────────────────────────────────

def build_diff(old_text, new_text, context=2):
    """
    行级 diff：每行是一个条目（帖子、列表项等），精确显示新增/删除了哪行。
    对于没有行结构的文本，回退到句子级切分。
    返回 list of {"type": "equal"|"insert"|"delete"|"fold", "text": str}
    """
    import difflib, re

    def to_lines(text):
        # 优先用换行切分；若全是一行则按句子切分
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if len(lines) <= 1:
            lines = [p.strip() for p in re.split(r'(?<=[。！？\.\!\?])\s+', text) if p.strip()]
        return lines

    old_lines = to_lines(old_text)
    new_lines = to_lines(new_text)

    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    result  = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            segs = old_lines[i1:i2]
            if len(segs) > context * 2 + 1:
                for s in segs[:context]:
                    result.append({"type": "equal", "text": s})
                result.append({"type": "fold", "text": f"… 折叠 {len(segs) - context*2} 行 …"})
                for s in segs[-context:]:
                    result.append({"type": "equal", "text": s})
            else:
                for s in segs:
                    result.append({"type": "equal", "text": s})
        if tag in ("replace", "delete"):
            for s in old_lines[i1:i2]:
                result.append({"type": "delete", "text": s})
        if tag in ("replace", "insert"):
            for s in new_lines[j1:j2]:
                result.append({"type": "insert", "text": s})
    return result


@app.route("/api/monitors/<string:monitor_id>/diff", methods=["GET"])
def get_diff(monitor_id):
    """返回监控项的变更 diff。"""
    config = load_config()
    for m in config["monitors"]:
        if m["id"] == monitor_id:
            return jsonify({
                "diff":    m.get("diff", []),
                "url":     m.get("url", ""),
                "name":    m.get("name", ""),
                "checked": m.get("last_checked", ""),
            })
    return jsonify({"error": "未找到该监控项"}), 404


# ── 检查 ─────────────────────────────────────────────────────────────────────

@app.route("/api/check/progress", methods=["GET"])
def check_progress():
    monitors = load_config().get("monitors", [])
    return jsonify({
        "total":   len(monitors),
        "done":    sum(1 for m in monitors if m.get("status") not in ("checking", "pending")),
        "changed": sum(1 for m in monitors if m.get("status") == "changed"),
        "errors":  sum(1 for m in monitors if m.get("status") == "error"),
    })


@app.route("/api/check", methods=["POST"])
def check_updates():
    """
    触发全量更新检查。
    - 同一域名的请求串行执行，间隔由域名规则的 delay 字段控制（默认 2s）
    - 不同域名之间并发（最多 10 个域名同时跑）
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from collections import defaultdict

    config   = load_config()
    monitors = config.get("monitors", [])
    if not monitors:
        return jsonify({"results": [], "monitors": []})

    cookie_store = config.get("cookies", {})
    # 按域名读取延迟配置（selector_rules 里的 delay 字段，默认 2s）
    raw_rules = config.get("selector_rules", {})

    for m in monitors:
        m["status"] = "checking"
    save_config(config)

    def check_one(m):
        try:
            hostname = get_hostname(m["url"])
            rule_obj = match_rule_obj(m["url"], raw_rules)
            use_pw = rule_obj.get("use_playwright", False)
            text, new_hash, _ = fetch_content(m["url"], m.get("selector", ""), cookie_store=cookie_store, use_playwright=use_pw)
            old_hash = m.get("last_hash", "")
            old_text = m.get("last_text", "")
            if not old_hash:
                m["status"]            = "unchanged"
                m["last_text_preview"] = f"[首次采集 {len(text)} 字符] " + text[:200]
                m["diff"]              = ""
            elif new_hash != old_hash:
                m["status"]            = "changed"
                m["last_text_preview"] = text[:200]
                m["diff"]              = build_diff(old_text, text)
            else:
                m["status"]            = "unchanged"
                m["last_text_preview"] = text[:200]
                m["diff"]              = ""
            m["last_hash"] = new_hash
            m["last_text"] = text
        except Exception as e:
            m["status"]            = "error"
            m["last_text_preview"] = str(e)
        m["last_checked"] = datetime.now().isoformat()
        return m

    # 按域名分组
    domain_groups = defaultdict(list)
    for m in monitors:
        domain_groups[get_hostname(m["url"])].append(m)

    completed_count = [0]

    def check_domain_group(domain, items):
        """同一域名的所有监控项串行执行，请求间加延迟（延迟按各项 URL 匹配最精确规则取）"""
        for i, m in enumerate(items):
            check_one(m)
            completed_count[0] += 1
            if completed_count[0] % 5 == 0:
                save_config(config)
            # 同域名下一条请求前等待（最后一条不等），延迟取该 URL 匹配规则的 delay
            if i < len(items) - 1:
                rule_obj = match_rule_obj(m["url"], raw_rules)
                delay = rule_obj.get("delay", 2) if rule_obj else 2
                time.sleep(delay)

    # 不同域名并发，最多 10 个域名同时
    MAX_DOMAIN_WORKERS = min(10, len(domain_groups))
    with ThreadPoolExecutor(max_workers=MAX_DOMAIN_WORKERS) as executor:
        futures = {
            executor.submit(check_domain_group, domain, items): domain
            for domain, items in domain_groups.items()
        }
        for future in as_completed(futures):
            future.result()
            save_config(config)

    save_config(config)
    return jsonify({"results": [{"id": m["id"], "status": m["status"]} for m in monitors], "monitors": monitors})


# ── Cookie ───────────────────────────────────────────────────────────────────

@app.route("/api/cookies", methods=["GET"])
def get_cookies():
    store = load_config().get("cookies", {})
    return jsonify({"cookies": {d: f"已保存（{len(v)} 字符）" for d, v in store.items()}})


@app.route("/api/cookies", methods=["POST"])
def save_cookie():
    data   = request.get_json()
    domain = (data.get("domain") or "").strip().lower()
    cookie = (data.get("cookie") or "").strip()
    if not domain or not cookie:
        return jsonify({"error": "domain 和 cookie 不能为空"}), 400
    config = load_config()
    config["cookies"][domain] = cookie
    save_config(config)
    return jsonify({"success": True, "domain": domain})


@app.route("/api/cookies/<path:domain>", methods=["DELETE"])
def delete_cookie(domain):
    config = load_config()
    if domain not in config.get("cookies", {}):
        return jsonify({"error": "未找到该域名的 Cookie"}), 404
    del config["cookies"][domain]
    save_config(config)
    return jsonify({"success": True})


if __name__ == "__main__":
    app.run(debug=True, port=5000)