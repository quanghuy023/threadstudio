"""Playwright-powered Threads post & comment scraper with GraphQL/SSR extraction and spam filtering."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time as _time
from typing import Any
import urllib.request

from ..core.models import CommentMode, SegmentItem

_SPAM_PATTERNS = re.compile(
    r"(shopee\.vn|shope\.ee|s\.shopee|lazada\.vn|tiktok\.com/@|bio\s+link|link\s+mua|giỏ\s+hàng|aff|pass\s+đồ)",
    re.IGNORECASE,
)

# Threads UI chrome that leaks into DOM text spans (translation affordance, etc.)
_THREADS_UI_LINE = re.compile(
    r"^(translate|see translation|view translation|dịch|xem bản dịch|bản dịch|"
    r"author|tác giả|người đăng|original poster)$",
    re.IGNORECASE,
)
_THREADS_UI_TAIL = re.compile(
    r"[\s\n]*\b(translate|see translation|view translation|dịch|xem bản dịch|"
    r"author|tác giả|người đăng)\b\s*$",
    re.IGNORECASE,
)
_RELATIVE_TIME_LABEL = re.compile(
    r"^(\d+[smhdw]|(\d+)\s*(giờ|phút|ngày|giây|tuần|tháng|năm)|vừa xong|hôm qua)$",
    re.IGNORECASE,
)


def _is_relative_time_label(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _RELATIVE_TIME_LABEL.match(t):
        return True
    return bool(re.match(r"^\d{1,3}[hH]$", t))


def _time_ago_from_taken(taken_at: int) -> str:
    """Format like Threads short relative labels (23h, 45m, 3d)."""
    try:
        delta = max(0, int(_time.time()) - int(taken_at))
    except (TypeError, ValueError):
        return ""
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86_400:
        return f"{delta // 3600}h"
    if delta < 604_800:
        return f"{delta // 86_400}d"
    if delta < 2_592_000:
        return f"{delta // 604_800}w"
    return f"{max(1, delta // 2_592_000)}mo"


def _strip_threads_ui_noise(text: str) -> str:
    if not text:
        return ""
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        s = _THREADS_UI_TAIL.sub("", line.strip()).strip()
        if not s or _THREADS_UI_LINE.match(s) or _is_relative_time_label(s):
            continue
        lines.append(s)
    return "\n".join(lines).strip()


def is_spam_comment(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    if _SPAM_PATTERNS.search(t):
        return True
    # Pure punctuation, single dot, or "chấm / hóng" noise
    alnum = re.sub(r"[^\wà-ỹÀ-Ỹ]", "", t)
    if len(alnum) < 3 or alnum.lower() in {"cham", "chấm", "hong", "hóng", "up"}:
        return True
    return False


def open_threads_login_session(profile_dir: Path) -> None:
    """Launch a visible Chromium window with persistent profile so user can log into Threads."""
    from playwright.sync_api import sync_playwright

    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1100, "height": 820},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.threads.com/login", wait_until="domcontentloaded")
        # Wait until the user closes the browser window after signing in
        try:
            page.wait_for_event("close", timeout=600_000)
        except Exception:
            pass
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def scrape_threads_url(
    url: str,
    *,
    profile_dir: Path,
    media_cache_dir: Path,
    comment_mode: str = CommentMode.TOP_LIKES_WITH_AUTHOR.value,
    max_comments: int = 6,
) -> tuple[str, list[SegmentItem]]:
    """Scrape a Threads post URL and return (title_slug, ordered SegmentItems)."""
    from playwright.sync_api import sync_playwright

    media_cache_dir.mkdir(parents=True, exist_ok=True)
    captured_payloads: list[Any] = []

    with sync_playwright() as p:
        use_persistent = (profile_dir / "Default").exists() or any(profile_dir.glob("*Cookie*"))
        if use_persistent:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=True,
                viewport={"width": 1280, "height": 1600},
                args=["--disable-blink-features=AutomationControlled"],
            )
        else:
            browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 1600},
                locale="vi-VN",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            )

        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def _on_response(resp) -> None:
            if "graphql" in resp.url or "api/v1" in resp.url:
                try:
                    data = resp.json()
                    captured_payloads.append(data)
                except Exception:
                    pass

        page.on("response", _on_response)
        page.goto(url.strip(), wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(2500)

        # Scroll 3 times to load additional comment threads
        for _ in range(3):
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(900)

        # Extract embedded Relay/SJS JSON scripts from HTML
        scripts_json = page.evaluate(
            """() => Array.from(document.querySelectorAll('script[type="application/json"]'))
                        .map(s => s.textContent).filter(Boolean)"""
        )
        for raw_s in scripts_json:
            if "thread_items" in raw_s or "text_post_app_info" in raw_s:
                try:
                    captured_payloads.append(json.loads(raw_s))
                except Exception:
                    pass

        dom_fallback = _extract_dom_fallback(page)
        ctx.close()

    posts = _collect_post_objects(captured_payloads, page_url=url.strip())
    if dom_fallback:
        posts = _merge_posts_with_dom(posts, dom_fallback, url.strip())
    elif not posts:
        posts = []

    if not posts:
        raise RuntimeError("Không đọc được nội dung bài viết Threads. Hãy kiểm tra link hoặc bấm Đăng nhập Threads.")

    root, replies = _split_root_and_replies(posts, url.strip())
    filtered = _select_comments(root, replies, comment_mode=comment_mode, max_comments=max_comments)

    all_items = [root, *filtered]
    segments: list[SegmentItem] = []
    for idx, item in enumerate(all_items):
        seg_id = "root" if idx == 0 else f"cmt_{idx:02d}"
        avatar_local = _download_asset(item.get("avatar_url", ""), media_cache_dir, f"{seg_id}_avatar")
        img_locals: list[str] = []
        for img_i, img_url in enumerate(item.get("image_urls", [])[:2]):
            local_p = _download_asset(img_url, media_cache_dir, f"{seg_id}_img_{img_i}")
            if local_p:
                img_locals.append(local_p)

        segments.append(
            SegmentItem(
                seg_id=seg_id,
                is_root_post=(idx == 0),
                username=item.get("username", "threads_user"),
                time_ago=str(item.get("time_ago", "") or "").strip(),
                avatar_url=item.get("avatar_url", ""),
                avatar_local=avatar_local,
                raw_text=_strip_threads_ui_noise(item.get("text", "")),
                spoken_text=_strip_threads_ui_noise(item.get("text", "")),
                image_urls=item.get("image_urls", []),
                image_locals=img_locals,
                likes=int(item.get("likes", 0)),
                replies=int(item.get("replies", 0)),
                reposts=int(item.get("reposts", 0)),
            )
        )

    slug = _make_slug(root.get("text", "") or root.get("username", "threads"))
    return slug, segments


def parse_manual_script(raw_script: str) -> tuple[str, list[SegmentItem]]:
    """Turn user-entered blocks (separated by blank lines or `---`) into SegmentItems."""
    blocks = [b.strip() for b in re.split(r"\n\s*---+\s*\n|\n\s*\n", raw_script.strip()) if b.strip()]
    if not blocks:
        raise ValueError("Nội dung kịch bản trống.")

    segments: list[SegmentItem] = []
    for idx, blk in enumerate(blocks):
        username = "tac_gia_threads" if idx == 0 else f"nguoi_dung_{idx:02d}"
        likes = max(50, 2400 // (idx + 1))
        text = blk
        # Support optional `@username: content` prefix
        m = re.match(r"^@?([a-zA-Z0-9._]{2,30})\s*\|\s*(\d+)\s*:\s*(.+)$", blk, re.DOTALL)
        if m:
            username, likes_s, text = m.group(1), m.group(2), m.group(3).strip()
            likes = int(likes_s)
        else:
            m2 = re.match(r"^@([a-zA-Z0-9._]{2,30})\s*:\s*(.+)$", blk, re.DOTALL)
            if m2:
                username, text = m2.group(1), m2.group(2).strip()

        segments.append(
            SegmentItem(
                seg_id="root" if idx == 0 else f"cmt_{idx:02d}",
                is_root_post=(idx == 0),
                username=username,
                raw_text=text,
                spoken_text=text,
                likes=likes,
                replies=max(2, likes // 35),
                reposts=max(1, likes // 18),
            )
        )
    return _make_slug(segments[0].raw_text), segments


def sample_video1_script() -> str:
    """Exact viral Vietnamese words thread from Video 1 for instant 1-click benchmarking."""
    return (
        '@yumemikawa | 7800: Có ai thích từ “nức nở” trong tiếng Việt giống mình không. Đối với mình đây là từ mình thích nhất. '
        "Nó rất đặc biệt và mình ko tìm đc bất cứ từ nước ngoài nào cho mình cảm giác giống nó hết\n\n"
        '@threednatam_ | 1100: Từ tiếng Việt mình thích nhất là chữ “thương”. Trong tiếng Anh dù từ nào đi nữa nó cũng không trọn gói được cái tình và cái ý của chữ “thương”.\n\n'
        '@griffin.jinbe.67 | 1000: với t thì là từ “thổn thức”, nó diễn tả cảm xúc dâng trào, gần như nức nở nhưng lại lặng im trong lòng.\n\n'
        '@putimain | 565: tui thích từ dỗi nữa =)))) kiểu chẳng tìm được cái từ nào ở nc bạn mang cái nghĩa nũng nịu nnay\n\n'
        '@motchaucay | 410: tôi lại ấn tượng với từ “lả lơi” 😍 nói chung khả năng biểu đạt của tiếng Việt quá đỉnh, chỉ một từ ngữ nhỏ bé, khi đọc lên đã gợi bao nhiêu hình dung\n\n'
        '@mem_tra_can | 233: của tui là “thanh” tui có cảm giác nó sạch mà nó mát vl :)))'
    )


def _parse_url_target(url: str) -> tuple[str, str]:
    """Extract @username and post shortcode/id from a Threads permalink."""
    m = re.search(r"/@([A-Za-z0-9._]+)/post/([A-Za-z0-9_-]+)", url)
    if m:
        return m.group(1).lower(), m.group(2)
    m2 = re.search(r"/post/([A-Za-z0-9_-]+)", url)
    if m2:
        return "", m2.group(1)
    return "", ""


def _norm_user(name: str) -> str:
    return (name or "").strip().lower().lstrip("@")


def _text_key(text: str, n: int = 56) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())[:n]


def _find_api_match(dom_row: dict[str, Any], api_posts: list[dict[str, Any]]) -> dict[str, Any] | None:
    du = _norm_user(dom_row.get("username", ""))
    dt = _text_key(dom_row.get("text", ""))
    for p in api_posts:
        if _norm_user(p.get("username", "")) != du:
            continue
        pt = _text_key(p.get("text", ""))
        if not dt or not pt:
            return p
        if pt.startswith(dt[: min(32, len(dt))]) or dt.startswith(pt[: min(32, len(pt))]):
            return p
        if dt in pt or pt in dt:
            return p
    for p in api_posts:
        if _norm_user(p.get("username", "")) == du:
            return p
    return None


def _merge_posts_with_dom(
    api_posts: list[dict[str, Any]], dom_posts: list[dict[str, Any]], url: str
) -> list[dict[str, Any]]:
    """Reorder/enrich API rows using on-screen card order (first card = root post)."""
    if not dom_posts:
        return api_posts
    url_user, url_code = _parse_url_target(url)
    dom0_user = _norm_user(dom_posts[0].get("username", ""))
    if url_user and dom0_user != _norm_user(url_user):
        for i, d in enumerate(dom_posts):
            if _norm_user(d.get("username", "")) == _norm_user(url_user):
                dom_posts = dom_posts[i:]
                break
        else:
            return api_posts or dom_posts

    merged: list[dict[str, Any]] = []
    used_keys: set[str] = set()
    for idx, dom in enumerate(dom_posts):
        hit = _find_api_match(dom, api_posts) if api_posts else None
        if hit:
            key = str(hit.get("pk") or _text_key(hit.get("text", "")))
            row = {**hit}
            if len((row.get("text") or "").strip()) < len((dom.get("text") or "").strip()):
                row["text"] = dom.get("text", row.get("text", ""))
            if dom.get("avatar_url") and not row.get("avatar_url"):
                row["avatar_url"] = dom["avatar_url"]
            if dom.get("time_ago"):
                row["time_ago"] = dom["time_ago"]
        else:
            key = f"{_norm_user(dom.get('username', ''))}:{_text_key(dom.get('text', ''))}"
            row = {
                **dom,
                "is_reply": idx > 0,
                "thread_order": idx,
            }
        if key in used_keys:
            continue
        used_keys.add(key)
        row["thread_order"] = idx
        row["is_reply"] = idx > 0
        merged.append(row)

    if url_code and api_posts:
        for p in api_posts:
            code = str(p.get("code") or "")
            pk = str(p.get("pk") or "")
            if code != url_code and url_code not in pk:
                continue
            key = str(p.get("pk") or _text_key(p.get("text", "")))
            if key in used_keys:
                continue
            if _norm_user(p.get("username", "")) == _norm_user(url_user):
                merged.insert(0, {**p, "thread_order": 0, "is_reply": False})
                used_keys.add(key)
                break

    if not merged:
        return api_posts or dom_posts

    root_user = _norm_user(merged[0].get("username", "")) if merged else ""
    if api_posts:
        for p in api_posts:
            key = str(p.get("pk") or f"{_norm_user(p.get('username', ''))}:{_text_key(p.get('text', ''))}")
            if key in used_keys:
                continue
            if _norm_user(p.get("username", "")) == root_user and not p.get("is_reply"):
                continue
            merged.append(p)
            used_keys.add(key)
    return merged if merged else (api_posts or dom_posts)


def _matches_url_post(p: dict[str, Any], url_code: str) -> bool:
    if not url_code:
        return False
    if str(p.get("code") or "") == url_code:
        return True
    pk = str(p.get("pk") or "")
    return bool(pk and url_code in pk)


def _pick_best_thread_block(
    blocks: list[list[dict[str, Any]]], url_user: str, url_code: str
) -> list[dict[str, Any]] | None:
    if not blocks:
        return None
    best: list[dict[str, Any]] | None = None
    best_score = -1
    for block in blocks:
        if not block:
            continue
        score = 0
        head = block[0]
        if url_user and _norm_user(head.get("username", "")) == _norm_user(url_user):
            score += 10_000
        if url_user and any(_norm_user(p.get("username", "")) == _norm_user(url_user) for p in block):
            score += 2_000
        if url_code:
            if any(str(p.get("code") or "") == url_code for p in block):
                score += 5_000
            if any(str(p.get("pk") or "") == url_code for p in block):
                score += 5_000
        score += min(len(block), 50)
        if score > best_score:
            best_score = score
            best = block
    return best


def _split_root_and_replies(posts: list[dict[str, Any]], url: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Pick the real root post (author) instead of assuming API walk order == DOM order."""
    if len(posts) == 1:
        return posts[0], []

    url_user, url_code = _parse_url_target(url)

    if url_user and _norm_user(posts[0].get("username", "")) == _norm_user(url_user):
        if not url_code or _matches_url_post(posts[0], url_code) or not any(
            _matches_url_post(p, url_code) for p in posts
        ):
            return posts[0], posts[1:]

    def _matches_url(p: dict[str, Any]) -> bool:
        return _matches_url_post(p, url_code)

    def _username_match(p: dict[str, Any]) -> bool:
        return bool(url_user) and _norm_user(p.get("username", "")) == _norm_user(url_user)

    # 1) Exact permalink match (username + code/pk)
    for p in posts:
        if _username_match(p) and _matches_url(p):
            rest = [x for x in posts if x is not p]
            return p, rest

    # 2) Post id from URL only
    if url_code:
        for p in posts:
            if _matches_url(p):
                rest = [x for x in posts if x is not p]
                return p, rest

    # 3) Author on permalink without code match — prefer non-reply + most replies (thread head)
    if url_user:
        author_posts = [p for p in posts if _norm_user(p.get("username", "")) == _norm_user(url_user)]
        if author_posts:
            roots = [p for p in author_posts if not p.get("is_reply")]
            pool = roots or author_posts
            root = max(pool, key=lambda x: (int(x.get("replies", 0)), len(x.get("text", ""))))
            rest = [x for x in posts if x is not root]
            return root, rest

    # 4) Any node explicitly marked not-a-reply
    non_replies = [p for p in posts if not p.get("is_reply")]
    if non_replies:
        root = max(non_replies, key=lambda x: (int(x.get("replies", 0)), len(x.get("text", ""))))
        rest = [x for x in posts if x is not root]
        return root, rest

    # 5) Heuristic: thread head usually has highest direct_reply_count among top-like posts
    root = max(posts, key=lambda x: (int(x.get("replies", 0)), int(x.get("likes", 0)), len(x.get("text", ""))))
    rest = [x for x in posts if x is not root]
    return root, rest


def _collect_post_objects(payloads: list[Any], *, page_url: str = "") -> list[dict[str, Any]]:
    seen_pks: set[str] = set()
    extracted: list[dict[str, Any]] = []
    thread_items_hits: list[list[dict[str, Any]]] = []

    def _post_dict(p: dict[str, Any], *, thread_order: int | None = None) -> dict[str, Any] | None:
        pk = str(p.get("pk") or p.get("id") or "")
        user = p.get("user") or {}
        username = user.get("username") or ""
        caption = p.get("caption") or {}
        text = caption.get("text") if isinstance(caption, dict) else ""
        if not text:
            frags = (((p.get("text_post_app_info") or {}).get("text_fragments") or {}).get("fragments") or [])
            text = "".join(f.get("plaintext", "") for f in frags if isinstance(f, dict))
        text = _strip_threads_ui_noise((text or "").strip())
        images = _extract_post_images(p)
        if not username or (not text and not images):
            return None
        dedup_key = pk or f"{username}:{text[:50]}"
        if dedup_key in seen_pks:
            return None
        seen_pks.add(dedup_key)

        info = p.get("text_post_app_info") or {}
        is_reply = bool(info.get("is_reply")) or bool(info.get("parent_post_id"))
        if not is_reply and info.get("reply_to_author"):
            is_reply = True
        time_ago = ""
        taken = p.get("taken_at")
        if taken:
            time_ago = _time_ago_from_taken(int(taken))
        return {
            "username": username,
            "time_ago": time_ago,
            "avatar_url": user.get("profile_pic_url") or "",
            "text": text,
            "image_urls": images,
            "likes": int(p.get("like_count") or 0),
            "replies": int(info.get("direct_reply_count") or p.get("comment_count") or 0),
            "reposts": int((info.get("repost_count") or 0) + (info.get("quote_count") or 0)),
            "pk": pk,
            "code": str(p.get("code") or ""),
            "is_reply": is_reply,
            "thread_order": thread_order,
        }

    def _try_parse_post(p: dict[str, Any], *, thread_order: int | None = None) -> None:
        row = _post_dict(p, thread_order=thread_order)
        if row:
            extracted.append(row)

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            thread_items = node.get("thread_items")
            if isinstance(thread_items, list) and thread_items:
                ordered: list[dict[str, Any]] = []
                for i, item in enumerate(thread_items):
                    if not isinstance(item, dict):
                        continue
                    post = item.get("post")
                    if isinstance(post, dict):
                        row = _post_dict(post, thread_order=i)
                        if row:
                            ordered.append(row)
                if ordered:
                    thread_items_hits.append(ordered)
            if "post" in node and isinstance(node["post"], dict):
                _try_parse_post(node["post"])
            elif "user" in node and ("caption" in node or "text_post_app_info" in node):
                _try_parse_post(node)
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for elem in node:
                _walk(elem)

    for payload in payloads:
        _walk(payload)

    url_user, url_code = _parse_url_target(page_url)
    if thread_items_hits:
        best = _pick_best_thread_block(thread_items_hits, url_user, url_code)
        if best:
            return best

    return extracted


def _extract_post_images(p: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    carousel = p.get("carousel_media") or []
    candidates_containers = carousel if carousel else [p]
    for item in candidates_containers:
        if not isinstance(item, dict):
            continue
        # Skip video thumbnails if video_versions exist? Still useful if post is image meme
        img_v2 = (item.get("image_versions2") or {}).get("candidates") or []
        if img_v2 and isinstance(img_v2[0], dict) and img_v2[0].get("url"):
            if not item.get("video_versions"):
                urls.append(img_v2[0]["url"])
    return urls


def _extract_dom_fallback(page) -> list[dict[str, Any]]:
    try:
        return page.evaluate(
            """() => {
                const results = [];
                const pathMatch = window.location.pathname.match(/\\/@([^/]+)\\/post\\/([^/?#]+)/i);
                const expectUser = pathMatch ? pathMatch[1].toLowerCase() : '';
                const root = document.querySelector('main') || document.body;
                const articles = root.querySelectorAll('[data-pressable-container="true"]');
                articles.forEach((el) => {
                    const userLink = el.querySelector('a[href^="/@"]');
                    if (!userLink) return;
                    const username = userLink.getAttribute('href').replace('/@', '').split('/')[0];
                    const userLower = username.toLowerCase();
                    if (expectUser && userLower !== expectUser && results.length === 0) {
                        return;
                    }
                    const avatarImg = el.querySelector('img[alt*="profile"], img');
                    const isUiNoise = (t) => /^(translate|see translation|view translation|dịch|xem bản dịch|bản dịch|author|tác giả|người đăng|original poster)$/i.test(t);
                    const isTimeLabel = (t) => /^(\\d+[smhdw]|\\d+\\s*(giờ|phút|ngày|giây|tuần|tháng|năm)|vừa xong|hôm qua)$/i.test(t) || /^\\d{1,3}h$/i.test(t);
                    let timeAgo = '';
                    const timeNode = el.querySelector('time');
                    if (timeNode) {
                        const t = (timeNode.innerText || '').trim();
                        if (t) timeAgo = t;
                    }
                    const spans = Array.from(el.querySelectorAll('span[dir="auto"]'))
                        .filter((s) => {
                            const t = (s.innerText || '').trim();
                            if (isUiNoise(t)) return false;
                            const btn = s.closest('[role="button"], button, a');
                            if (btn && isUiNoise(t)) return false;
                            return true;
                        })
                        .map(s => s.innerText.trim())
                        .filter(t => t && t !== username && !/^[0-9.,KMB]+$/.test(t) && t.length > 2 && !isUiNoise(t));
                    const body = [];
                    for (const t of spans) {
                        if (!timeAgo && isTimeLabel(t)) {
                            timeAgo = t;
                            continue;
                        }
                        if (isTimeLabel(t)) continue;
                        body.push(t);
                    }
                    const text = body.join('\\n').trim();
                    if (username && text) {
                        results.push({
                            username,
                            time_ago: timeAgo,
                            avatar_url: avatarImg ? avatarImg.src : '',
                            text,
                            image_urls: [],
                            likes: 0,
                            replies: 0,
                            reposts: 0
                        });
                    }
                });
                return results;
            }"""
        )
    except Exception:
        return []


def _select_comments(
    root: dict[str, Any],
    replies: list[dict[str, Any]],
    *,
    comment_mode: str,
    max_comments: int,
) -> list[dict[str, Any]]:
    author = root.get("username", "")
    clean: list[dict[str, Any]] = []
    for r in replies:
        txt = r.get("text", "")
        if r.get("username") != author and is_spam_comment(txt):
            continue
        if not txt and not r.get("image_urls"):
            continue
        clean.append(r)

    if comment_mode == CommentMode.AUTHOR_ONLY.value:
        # Keep chronological order for multi-part author storytelling!
        author_items = [r for r in clean if r.get("username") == author]
        return author_items[:max_comments]

    if comment_mode == CommentMode.TOP_LIKES_NO_AUTHOR.value:
        others = [r for r in clean if r.get("username") != author]
        others.sort(key=lambda x: int(x.get("likes", 0)), reverse=True)
        return others[:max_comments]

    # TOP_LIKES_WITH_AUTHOR
    clean.sort(key=lambda x: int(x.get("likes", 0)), reverse=True)
    return clean[:max_comments]


def _download_asset(url: str, cache_dir: Path, prefix: str) -> str:
    if not url or not url.startswith("http"):
        return ""
    ext = ".png" if ".png" in url.lower() else ".jpg"
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
    dest = cache_dir / f"{prefix}_{digest}{ext}"
    if dest.exists() and dest.stat().st_size > 100:
        return str(dest)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            dest.write_bytes(resp.read())
        return str(dest)
    except Exception:
        return ""


def _make_slug(text: str) -> str:
    words = re.findall(r"[a-zA-Z0-9à-ỹÀ-ỸđĐ]+", text.lower())[:4]
    if not words:
        return "threads_video"
    raw = "_".join(words)
    # Transliterate Vietnamese accents for clean MP4 filenames (like giamsuc.mp4 in Video 2)
    vi_map = str.maketrans(
        "àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ",
        "aaaaaaaaaaaaaaaaaeeeeeeeeeeeiiiiiooooooooooooooooouuuuuuuuuuuyyyyyd",
    )
    slug = re.sub(r"[^a-z0-9_]", "", raw.translate(vi_map))
    return slug[:32] or "threads_video"

