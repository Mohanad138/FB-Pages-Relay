import os
import re
import sys
import json
import asyncio
import time
import hashlib
import requests
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaWebPage, MessageMediaPhoto

# ============ الإعدادات ============
# المفتاح الداخلي (مثل "fabrizio") يتتبع حالة النشر. لو غيّرت المصدر غيّر المفتاح.
# لإضافة صفحة ثانية: انسخ كتلة وغيّر المصدر وأسماء الأسرار (FB_PAGE_ID_2 ...).
# لتعطيل إنستغرام لصفحة معينة: احذف سطر ig_user_id_env منها.
PAGES = {
    "fabrizio": {
        "source": "FabrizioRomanoTG",          # يوزرنيم القناة العامة بدون @
        "page_id_env": "FB_PAGE_ID_1",
        "token_env": "FB_PAGE_TOKEN_1",
        "ig_user_id_env": "IG_USER_ID_1",      # حساب الإنستغرام المربوط بالصفحة
        "mode": "translate_ar",                # translate_ar | rephrase_ar | copy
        "skip_podcast": True,
        "skip_rt": True,               # يتخطى الريتويت (RT @...)
        "skip_preview_video": True,    # يتخطى معاينات فيديو X
        "min_letters": 15,             # يتخطى منشورات المعاينة اللي نصها فاضي تقريباً (اسم + ايموجي)
    },
}

STATE_FILE = "state.json"
FIRST_RUN_COUNT = 5
FETCH_LIMIT = 40
GRAPH = "https://graph.facebook.com/v21.0"
IG_MAX_TRIES = 3          # لو منشور فشل على إنستغرام 3 مرات متتالية نتخطاه
IG_CAPTION_MAX = 2200
# الأرخص أولاً (الترجمة بسيطة). لو انتهت حصته أو ما توفر ينتقل للي بعده (لكل موديل حصة مستقلة)
GEMINI_MODELS = ["gemini-flash-lite-latest", "gemini-flash-latest"]

# نص يضاف بنهاية كل منشور + أول تعليق بعد النشر (فيسبوك)
FOOTER = (
    "----------------------\n"
    "يمكنكم متابعة الاخبار لحظة بلحظة عبر قناتنا على التلكرام\n"
    "رابط القناة في اول تعليق"
)
FIRST_COMMENT = "رابط قناة التلكرام 👇\nhttps://t.me/FabriAr1"

# فوتر إنستغرام: الرابط في البايو وبدون تعليق
IG_FOOTER = (
    "----------------------\n"
    "يمكنكم متابعة الاخبار لحظة بلحظة عبر قناتنا على التلكرام\n"
    "رابط القناة في البايو"
)

PROMPTS = {
    "translate_ar": """ترجم خبر كرة القدم التالي إلى العربية بأسلوب طبيعي كأن كاتباً عربياً كتبه (ليس حرفياً).
- حافظ على تقسيم الأسطر.
- بدون هاشتاغات، وبدون روابط أو إشارات لحسابات.
- إذا كان إعلاناً أو ترويجاً (سعر، خصم، اشتراك، رعاية، كود خصم، رابط شراء) أجب SKIP فقط. وعند الشك انشره.
- أجب بالنص النهائي فقط.

النص:
""",
    "rephrase_ar": """أعد صياغة الخبر التالي بالعربية بأسلوب صحفي طبيعي مع الحفاظ على كل الأرقام والتفاصيل.
- إذا كان إعلاناً أو ترويجاً مدفوعاً أجب بكلمة SKIP فقط، وعند الشك انشره.
- أجب بالنص النهائي فقط.

النص الأصلي:
""",
}


def need(name):
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"[ERROR] secret missing or empty: {name} — check the exact name in Settings > Secrets and the env block in run.yml")
        sys.exit(1)
    return v


# ============ الحالة ============
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def h(text):
    # نحسب الهاش على نص المصدر الخام وليس على ناتج الذكاء الاصطناعي
    return hashlib.sha1(text.strip().encode()).hexdigest()


# ============ التنظيف على مستوى الكود ============
def strip_hashtags(t):
    t = re.sub(r"(?<!\S)#[^\s#]+", "", t or "")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def clean_text(text, source):
    t = text or ""
    # توقيع التغريدة: "— Fabrizio Romano (@FabrizioRomano)" + سطر التاريخ
    t = re.sub(r"[—–-]\s*Fabrizio Romano\s*\(@FabrizioRomano\)\s*(?:[A-Z][a-z]{2,8}\.? \d{1,2}, \d{4})?", "", t)
    t = re.sub(r"https?://t\.co/\S+", "", t)
    t = re.sub(r"https?://(www\.)?(t\.me|telegram\.me)/\S+", "", t)
    t = re.sub(r"@Sabereen_Social", "", t, flags=re.I)
    t = re.sub(r"@" + re.escape(source) + r"\b", "", t, flags=re.I)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return strip_hashtags(t)


def is_rt(text):
    return bool(re.match(r"\s*RT\s+@", text or "", re.I))


def letters_count(text):
    t = re.sub(r"@\w+", "", text or "")
    t = re.sub(r"https?://\S+", "", t)
    return len(re.findall(r"[^\W\d_]", t))


def preview_is_video(wp):
    if wp is None:
        return False
    t = (getattr(wp, "type", "") or "").lower()
    return t in ("video", "gif") or bool(getattr(wp, "duration", None)) or bool(getattr(wp, "embed_url", None))


def is_podcast_promo(text):
    t = text.lower()
    return bool(re.match(r"\s*new pod", t)) and "here we go" not in t


def enforce_here_we_go(raw, out):
    # القاعدة تنفذ بالكود: "هير وي كو" فقط لو النص الأصلي فيه Here We Go حرفياً
    has = "here we go" in raw.lower()
    out = re.sub(r"^\s*هير\s*وي\s*(كو|غو)\s*", "", out)
    if has:
        out = "هير وي كو" + out
    return out


# ============ Gemini ============
def gemini_generate(prompt, gemini_key):
    last = "no model tried"
    for model in GEMINI_MODELS:
        for attempt in range(2):
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": gemini_key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.3, "maxOutputTokens": 700},
                },
                timeout=90,
            )
            if r.status_code == 200:
                data = r.json()
                try:
                    return data["candidates"][0]["content"]["parts"][0]["text"]
                except Exception:
                    raise RuntimeError(f"Gemini returned no text ({model}): {str(data)[:300]}")
            last = f"{model} -> {r.status_code}: {r.text[:200]}"
            print(f"[gemini] {last}")
            # 429 بسبب الدقيقة (مو اليوم): نصبر شوية ونعيد مرة وحدة
            if r.status_code == 429 and "PerDay" not in r.text and attempt == 0:
                time.sleep(25)
                continue
            break
        if not any(c in last for c in ("429", "404", "503")):
            break  # خطأ غريب: ما ننتقل لموديل ثاني
    raise RuntimeError(f"Gemini failed: {last}")


def ai_process(raw, mode, gemini_key):
    if mode == "copy":
        return raw
    out = strip_hashtags(gemini_generate(PROMPTS[mode] + raw, gemini_key))
    if out.upper().startswith("SKIP"):
        return None
    if mode == "translate_ar":
        out = enforce_here_we_go(raw, out)
    return out


# ============ فيسبوك ============
def fb_check(r):
    try:
        body = r.json()
    except Exception:
        raise RuntimeError(f"Facebook non-JSON response {r.status_code}: {r.text[:200]}")
    if r.status_code != 200 or "error" in body:
        err = body.get("error", {})
        if err.get("code") == 190:
            raise RuntimeError("Facebook token invalid/expired (code 190) — generate a new Page token")
        raise RuntimeError(f"Facebook error {r.status_code}: {body}")
    return body


def fb_post(page_id, token, text, images):
    base = f"{GRAPH}/{page_id}"
    if not images:
        return fb_check(requests.post(f"{base}/feed", data={"message": text, "access_token": token}, timeout=60))
    if len(images) == 1:
        data = {"access_token": token}
        if text:
            data["message"] = text
        return fb_check(requests.post(f"{base}/photos", data=data, files={"source": ("img.jpg", images[0])}, timeout=120))
    # ألبوم: ننشر كل صورة غير منشورة ثم منشور واحد يجمعها
    ids = []
    for img in images[:10]:
        b = fb_check(requests.post(
            f"{base}/photos",
            data={"access_token": token, "published": "false"},
            files={"source": ("img.jpg", img)}, timeout=120))
        ids.append(b["id"])
    data = {"access_token": token, "message": text}
    for i, pid in enumerate(ids):
        data[f"attached_media[{i}]"] = json.dumps({"media_fbid": pid})
    return fb_check(requests.post(f"{base}/feed", data=data, timeout=60))


def fb_comment(token, post_id, message):
    r = requests.post(
        f"{GRAPH}/{post_id}/comments",
        data={"message": message, "access_token": token},
        timeout=60,
    )
    return fb_check(r)


# ============ إنستغرام ============
def fb_photo_url(page_id, token, img):
    """نرفع الصورة للصفحة كصورة غير منشورة (ما تظهر لأحد) ونأخذ رابطها العام لإنستغرام."""
    b = fb_check(requests.post(
        f"{GRAPH}/{page_id}/photos",
        data={"access_token": token, "published": "false"},
        files={"source": ("img.jpg", img)}, timeout=120))
    info = fb_check(requests.get(
        f"{GRAPH}/{b['id']}",
        params={"fields": "images", "access_token": token}, timeout=60))
    imgs = info.get("images") or []
    if not imgs:
        raise RuntimeError(f"no image url returned for photo {b['id']}")
    return imgs[0]["source"]          # الأكبر أولاً


def ig_create(ig_id, token, **params):
    params["access_token"] = token
    return fb_check(requests.post(f"{GRAPH}/{ig_id}/media", data=params, timeout=60))["id"]


def ig_wait(container, token):
    for _ in range(18):               # حتى 90 ثانية
        r = fb_check(requests.get(
            f"{GRAPH}/{container}",
            params={"fields": "status_code,status", "access_token": token}, timeout=60))
        s = r.get("status_code")
        if s == "FINISHED":
            return
        if s in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram container {s}: {r.get('status')}")
        time.sleep(5)
    raise RuntimeError("Instagram container timeout")


def ig_publish(ig_id, token, creation_id):
    return fb_check(requests.post(
        f"{GRAPH}/{ig_id}/media_publish",
        data={"creation_id": creation_id, "access_token": token}, timeout=60))


def build_ig_caption(text):
    if not text:
        return IG_FOOTER
    room = IG_CAPTION_MAX - len(IG_FOOTER) - 2
    if len(text) > room:
        text = text[:room - 1].rstrip() + "…"
    return f"{text}\n\n{IG_FOOTER}"


def ig_post(page_id, ig_id, token, text, images):
    caption = build_ig_caption(text)
    urls = [fb_photo_url(page_id, token, img) for img in images[:10]]
    if len(urls) == 1:
        c = ig_create(ig_id, token, image_url=urls[0], caption=caption)
    else:
        kids = [ig_create(ig_id, token, image_url=u, is_carousel_item="true") for u in urls]
        for k in kids:
            ig_wait(k, token)
        c = ig_create(ig_id, token, media_type="CAROUSEL", children=",".join(kids), caption=caption)
    ig_wait(c, token)
    return ig_publish(ig_id, token, c)


# ============ تيليجرام ============
def classify(m):
    """photo | text | skip"""
    if m.video or m.gif or m.document or m.audio or m.voice or m.video_note or m.sticker:
        return "skip"          # فيديو، ملفات، ومعاينات فيديو X
    if m.photo:
        return "photo"
    if m.media is not None and m.web_preview is None:
        return "skip"          # استفتاء أو شي غريب
    return "text"


def group_messages(msgs):
    items, seen = [], {}
    for m in msgs:
        if m.grouped_id:
            if m.grouped_id in seen:
                seen[m.grouped_id].append(m)
                continue
            seen[m.grouped_id] = [m]
            items.append(seen[m.grouped_id])
        else:
            items.append([m])
    return items


async def fetch_new(client, source, last_id):
    if last_id is None:
        # أول تشغيل: آخر 5 فقط
        latest = [m async for m in client.iter_messages(source, limit=FIRST_RUN_COUNT)]
        if not latest:
            return [], 0
        last_id = min(m.id for m in latest) - 1
    msgs = [m async for m in client.iter_messages(source, min_id=last_id, reverse=True, limit=FETCH_LIMIT)]
    return group_messages(msgs), last_id


async def run_page(client, key, cfg, state, gemini_key):
    page_id = need(cfg["page_id_env"])
    token = need(cfg["token_env"])
    ig_id = need(cfg["ig_user_id_env"]) if cfg.get("ig_user_id_env") else None

    st = state.setdefault(key, {"last_id": None, "hashes": []})
    st.setdefault("hashes", [])
    if ig_id:
        st.setdefault("ig_last_id", None)
        st.setdefault("ig_hashes", [])

    # كل منصة لها تقدمها الخاص: فشل إنستغرام ما يوقف فيسبوك والعكس
    start = st["last_id"]
    if start is not None and ig_id and st["ig_last_id"] is not None:
        start = min(start, st["ig_last_id"])
    items, first = await fetch_new(client, cfg["source"], start)
    if st["last_id"] is None:
        st["last_id"] = first
    if ig_id and st["ig_last_id"] is None:
        # أول تفعيل لإنستغرام: نبدأ من الجديد فقط بدون نشر القديم
        st["ig_last_id"] = st["last_id"]
    print(f"[{key}] {len(items)} new items")

    blocked = {"fb": False, "ig": False}
    failed = False

    for grp in items:
        top = max(m.id for m in grp)

        todo = []
        if top > st["last_id"] and not blocked["fb"]:
            todo.append("fb")
        if ig_id and top > st["ig_last_id"] and not blocked["ig"]:
            todo.append("ig")
        if not todo:
            continue

        def advance(platforms, why=None):
            for p in platforms:
                if p == "fb":
                    st["last_id"] = top
                else:
                    st["ig_last_id"] = top
            save_state(state)
            if why:
                print(f"[{key}] {why} {top}")

        kinds = [classify(m) for m in grp]
        raw = next((m.message for m in grp if m.message), "") or ""
        raw = clean_text(raw, cfg["source"])
        has_photo = any(k == "photo" for k in kinds)
        wp = next((m.media.webpage for m in grp if isinstance(m.media, MessageMediaWebPage)), None)
        is_preview = wp is not None and not any(isinstance(m.media, MessageMediaPhoto) for m in grp)
        if wp is not None:
            print(f"[{key}] preview info id={top} type={getattr(wp, 'type', None)} duration={getattr(wp, 'duration', None)} embed={bool(getattr(wp, 'embed_url', None))}")

        if "skip" in kinds:
            advance(todo, "[SKIP media]"); continue
        if cfg.get("skip_rt") and is_rt(raw):
            advance(todo, "[SKIP RT]"); continue
        if cfg.get("skip_preview_video") and is_preview and preview_is_video(wp):
            advance(todo, "[SKIP preview video]"); continue
        if is_preview and letters_count(raw) < cfg.get("min_letters", 15):
            advance(todo, "[SKIP empty quote]"); continue
        if not raw and not has_photo:
            advance(todo, "[SKIP empty]"); continue
        if cfg.get("skip_podcast") and is_podcast_promo(raw):
            advance(todo, "[SKIP podcast]"); continue

        # منع التكرار لكل منصة على حدة
        hh = h(raw) if raw else None
        post_fb = "fb" in todo and not (hh and hh in st["hashes"])
        post_ig = "ig" in todo and not (hh and hh in st["ig_hashes"])
        if not post_fb and not post_ig:
            advance(todo, "[SKIP duplicate]"); continue

        text = ""
        if raw:
            text = ai_process(raw, cfg["mode"], gemini_key)   # لو فشل Gemini يتوقف التشغيل وتبقى الحالة
            if text is None:
                advance(todo, "[SKIP ad]"); continue

        images = []
        for m, k in zip(grp, kinds):
            if k == "photo":
                data = await client.download_media(m, file=bytes)
                if data:
                    images.append(data)

        if not text and not images:
            advance(todo, "[SKIP nothing to post]"); continue

        if post_ig and not images:
            print(f"[{key}] IG: text-only post skipped {top}")
            post_ig = False

        idle = [p for p in todo if not ((p == "fb" and post_fb) or (p == "ig" and post_ig))]
        if idle:
            advance(idle)
        if not post_fb and not post_ig:
            continue

        # ---------- فيسبوك ----------
        if post_fb:
            try:
                ftext = f"{text}\n\n{FOOTER}" if text else FOOTER
                res = fb_post(page_id, token, ftext, images)
                post_id = res.get("post_id") or res.get("id")
                # التعليق الأول: لو فشل لا نفشل المنشور (وإلا ينشر مرة ثانية)
                try:
                    fb_comment(token, post_id, FIRST_COMMENT)
                except Exception as e:
                    print(f"[{key}] WARNING comment failed for {post_id}: {e}")
                if hh:
                    st["hashes"] = (st["hashes"] + [hh])[-100:]
                advance(["fb"], "[FB OK]")
            except Exception as e:
                blocked["fb"] = True
                failed = True
                print(f"[{key}] FB FAILED {top}: {e}")

        # ---------- إنستغرام ----------
        if post_ig:
            try:
                ig_post(page_id, ig_id, token, text, images)
                if hh:
                    st["ig_hashes"] = (st["ig_hashes"] + [hh])[-100:]
                st["ig_fail"] = {}
                advance(["ig"], "[IG OK]")
            except Exception as e:
                print(f"[{key}] IG FAILED {top}: {e}")
                f = st.get("ig_fail") or {}
                n = f.get("n", 0) + 1 if f.get("id") == top else 1
                st["ig_fail"] = {"id": top, "n": n}
                if n >= IG_MAX_TRIES:
                    st["ig_fail"] = {}
                    advance(["ig"], f"[IG GIVING UP after {n} tries]")
                else:
                    blocked["ig"] = True
                    failed = True
                    save_state(state)

    if failed:
        raise RuntimeError("one or more posts failed (see log above)")


async def main():
    api_id = int(need("TG_API_ID"))
    api_hash = need("TG_API_HASH")
    session = need("TG_SESSION")
    gemini_key = need("GEMINI_API_KEY")

    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        print("[ERROR] TG_SESSION is not authorized — generate a new session string")
        sys.exit(1)

    state = load_state()
    failed = False
    try:
        for key, cfg in PAGES.items():
            try:
                await run_page(client, key, cfg, state, gemini_key)
            except Exception as e:
                failed = True
                print(f"[{key}] FAILED: {e}")
    finally:
        save_state(state)
        await client.disconnect()
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
