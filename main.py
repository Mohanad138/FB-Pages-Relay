import os
import re
import sys
import json
import asyncio
import time
import io
import math
import hashlib
import subprocess
import requests
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaWebPage, MessageMediaPhoto

try:
    from PIL import Image
except Exception:   # لو pillow غير مثبتة، إنستغرام يستخدم الصورة كما هي
    Image = None

# ============ الإعدادات ============
# المفتاح الداخلي (مثل "fabrizio") يتتبع حالة النشر. لو غيّرت المصدر غيّر المفتاح.
# لإضافة صفحة ثانية: انسخ كتلة وغيّر المصدر وأسماء الأسرار (FB_PAGE_ID_2 ...).
PAGES = {
    "fabrizio": {
        "source": "FabrizioRomanoTG",          # يوزرنيم القناة العامة بدون @
        "page_id_env": "FB_PAGE_ID_1",
        "token_env": "FB_PAGE_TOKEN_1",
        "mode": "translate_ar",                # translate_ar | rephrase_ar | copy
        "skip_podcast": True,
        "skip_rt": True,               # يتخطى الريتويت (RT @...)
        "skip_preview_video": True,    # يتخطى معاينات فيديو X
        "min_letters": 15,             # يتخطى منشورات المعاينة اللي نصها فاضي تقريباً (اسم + ايموجي)
        "min_score": 7,                # ينشر فقط المهم: درجة التفاعل المتوقعة >= هذا الرقم (0 = ينشر كل شي)
        "instagram": True,             # ينشر نفس المنشور على إنستغرام (لو الحساب Professional ومربوط بالصفحة)
    },
}

STATE_FILE = "state.json"
FIRST_RUN_COUNT = 5
COOLDOWN_SECONDS = 300   # بعد فشل Gemini: استراحة 5 دقائق قبل المحاولة التالية
FETCH_LIMIT = 40
GRAPH = "https://graph.facebook.com/v21.0"
# الأرخص أولاً (الترجمة بسيطة). لو انتهت حصته أو ما توفر ينتقل للي بعده (لكل موديل حصة مستقلة)
GEMINI_MODELS = ["gemini-flash-lite-latest", "gemini-flash-latest", "gemma-3-27b-it"]

# نص يضاف بنهاية كل منشور + أول تعليق بعد النشر
FOOTER = (
    "----------------------\n"
    "يمكنكم متابعة الاخبار لحظة بلحظة عبر قناتنا على التلكرام\n"
    "رابط القناة في اول تعليق"
)
FIRST_COMMENT = "تابعنا على التلكرام 👇\nhttps://t.me/FabriAr1"

# إنستغرام ما يسمح برابط قابل للضغط بالتعليقات، فنقول الرابط بالبايو
IG_FOOTER = (
    "----------------------\n"
    "يمكنكم متابعة الاخبار لحظة بلحظة عبر قناتنا على التلكرام\n"
    "رابط القناة في البايو"
)
IG_MAX_TRIES = 8   # عدد محاولات إعادة النشر على إنستغرام قبل الاستسلام

PROMPTS = {
    "translate_ar": """أنت محرر صفحة أخبار كرة قدم عربية هدفها جذب التفاعل.
أعطِ الخبر درجة من 1 إلى 10 حسب احتمال تفاعل الجمهور العربي معه:
- 9-10: صفقة مؤكدة أو Here we go، أو نجم عالمي (ميسي، رونالدو، مبابي، هالاند، صلاح...)، أو نادٍ كبير (ريال مدريد، برشلونة، سيتي، يونايتد، ليفربول، أرسنال، تشلسي، باريس، بايرن، يوفنتوس، ميلان، إنتر، الهلال، النصر...)، أو تعيين/إقالة مدرب كبير.
- 6-8: صفقة متقدمة أو مفاوضات جدية أو تجديد عقد أو إصابة نجم أو لاعب عربي معروف.
- 3-5: شائعات عادية أو أندية متوسطة أو لاعبون أقل شهرة.
- 1-2: أخبار هامشية أو دوريات صغيرة أو ناشئون مجهولون.

الصيغة: السطر الأول فقط SCORE: الرقم، ثم ترجم الخبر إلى العربية بأسلوب طبيعي كأن كاتباً عربياً كتبه (ليس حرفياً).
- حافظ على تقسيم الأسطر.
- بدون هاشتاغات، وبدون روابط أو إشارات لحسابات.
- إذا كان إعلاناً أو ترويجاً (سعر، خصم، اشتراك، رعاية، كود خصم، رابط شراء) أجب SKIP فقط. وعند الشك انشره.
- أجب بالصيغة المطلوبة فقط بدون أي شرح.

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
        code = 0
        for attempt in range(3):
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": gemini_key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.3, "maxOutputTokens": 700},
                },
                timeout=90,
            )
            code = r.status_code
            if code == 200:
                data = r.json()
                try:
                    return data["candidates"][0]["content"]["parts"][0]["text"]
                except Exception:
                    raise RuntimeError(f"Gemini returned no text ({model}): {str(data)[:300]}")
            last = f"{model} -> {code}: {r.text[:200]}"
            print(f"[gemini] {last}")
            # 503 (ضغط مؤقت) أو 429 بسبب الدقيقة: ننتظر ونعيد على نفس الموديل
            transient = code == 503 or (code == 429 and "PerDay" not in r.text)
            if transient and attempt < 2:
                time.sleep(8 * (attempt + 1))   # 8 ثواني ثم 16
                continue
            break   # حصة اليوم خلصت أو الموديل غير متوفر: ننتقل للموديل التالي
        if code not in (429, 404, 500, 503):
            break   # خطأ غريب (مثل 400): ما ننتقل لموديل ثاني
    raise RuntimeError(f"Gemini failed: {last}")


def parse_score(out):
    m = re.match(r"\s*SCORE\s*[:：]\s*(\d{1,2})[^\n]*\n?", out, re.I)
    if not m:
        return None, out
    return min(int(m.group(1)), 10), out[m.end():].strip()


def ai_process(raw, mode, gemini_key, min_score=0):
    """يرجع (النص, سبب_التخطي). لو النص None فالسبب: ad أو low score N"""
    if mode == "copy":
        return raw, None
    out = strip_hashtags(gemini_generate(PROMPTS[mode] + raw, gemini_key))
    if out.upper().startswith("SKIP"):
        return None, "ad"
    if mode == "translate_ar":
        score, out = parse_score(out)
        if out.upper().startswith("SKIP"):
            return None, "ad"
        if "here we go" in raw.lower():
            score = 10          # Here we go تُنشر دائماً
        if score is not None:
            print(f"[score] {score} (min {min_score})")
            if score < min_score:
                return None, f"low score {score}"
        out = enforce_here_we_go(raw, out)
    return out, None


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


def git_persist_state():
    """يحفظ state.json بالريبو فوراً (يُستدعى بعد كل منشور) حتى لا يضيع التقدم فيتكرر النشر."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return True

    def sh(*args):
        return subprocess.run(args, capture_output=True, text=True)

    sh("git", "config", "user.name", "bot")
    sh("git", "config", "user.email", "bot@users.noreply.github.com")
    sh("git", "add", STATE_FILE)
    if sh("git", "diff", "--cached", "--quiet").returncode == 0:
        return True   # ما تغير شي
    sh("git", "commit", "-m", "update state")
    for i in range(5):
        if sh("git", "push").returncode == 0:
            return True
        # تعارض: نفضّل نسختنا من state.json (الأحدث)
        r = sh("git", "pull", "--rebase", "-X", "theirs", "origin", "main")
        if r.returncode != 0:
            sh("git", "rebase", "--abort")
        time.sleep(3)
    print("[git] WARNING: could not push state.json after 5 tries")
    return False


# ============ إنستغرام ============
def fit_for_instagram(data):
    """JPEG وبنسبة أبعاد مقبولة (4:5 إلى 1.91:1) وعرض كافي"""
    if Image is None:
        return data
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return data
    w, h = im.size
    if w < 640:
        im = im.resize((640, max(1, int(h * 640 / w))), Image.LANCZOS)
        w, h = im.size
    ratio = w / h
    if ratio < 0.8:        # طويلة: نوسّع الجوانب
        new_w = math.ceil(h * 0.8)
        canvas = Image.new("RGB", (new_w, h), (20, 20, 20))
        canvas.paste(im, ((new_w - w) // 2, 0))
        im = canvas
    elif ratio > 1.91:     # عريضة: نوسّع الأعلى والأسفل
        new_h = math.ceil(w / 1.91)
        canvas = Image.new("RGB", (w, new_h), (20, 20, 20))
        canvas.paste(im, (0, (new_h - h) // 2))
        im = canvas
    out = io.BytesIO()
    im.save(out, "JPEG", quality=92)
    return out.getvalue()


def fb_host_image(page_id, token, data):
    """إنستغرام يريد رابط عام للصورة: نرفعها لفيسبوك غير منشورة ونأخذ رابطها"""
    b = fb_check(requests.post(
        f"{GRAPH}/{page_id}/photos",
        data={"access_token": token, "published": "false"},
        files={"source": ("img.jpg", data)}, timeout=120))
    info = fb_check(requests.get(
        f"{GRAPH}/{b['id']}",
        params={"fields": "images", "access_token": token}, timeout=30))
    return info["images"][0]["source"]


def ig_discover(page_id, token):
    try:
        r = requests.get(f"{GRAPH}/{page_id}",
                         params={"fields": "instagram_business_account", "access_token": token}, timeout=30)
        body = r.json()
    except Exception as e:
        print(f"[IG] discover failed: {e}")
        return None
    if "error" in body:
        print(f"[IG] discover error: {str(body['error'])[:200]}")
        return None
    return (body.get("instagram_business_account") or {}).get("id")


def ig_create(ig_id, token, **fields):
    data = {"access_token": token}
    data.update(fields)
    return fb_check(requests.post(f"{GRAPH}/{ig_id}/media", data=data, timeout=60))["id"]


def ig_wait(container, token):
    for _ in range(8):
        b = fb_check(requests.get(f"{GRAPH}/{container}",
                                  params={"fields": "status_code", "access_token": token}, timeout=30))
        st = b.get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram container {st}: {b}")
        time.sleep(4)
    raise RuntimeError("Instagram container not ready in time")


def ig_post(ig_id, token, caption, image_urls):
    caption = (caption or "")[:2200]
    if len(image_urls) == 1:
        container = ig_create(ig_id, token, image_url=image_urls[0], caption=caption)
    else:
        kids = []
        for u in image_urls[:10]:
            k = ig_create(ig_id, token, image_url=u, is_carousel_item="true")
            ig_wait(k, token)
            kids.append(k)
        container = ig_create(ig_id, token, media_type="CAROUSEL", children=",".join(kids), caption=caption)
    ig_wait(container, token)
    return fb_check(requests.post(f"{GRAPH}/{ig_id}/media_publish",
                                  data={"creation_id": container, "access_token": token}, timeout=60))


def ig_retry_pending(key, st, state, ig_id, token):
    """إعادة محاولة منشورات إنستغرام اللي فشلت سابقاً (فيسبوك نُشر، إنستغرام لا)"""
    keep = []
    for item in st.get("ig_pending", []):
        if time.time() - item.get("ts", 0) > 86400 or item.get("tries", 0) >= IG_MAX_TRIES:
            print(f"[{key}] [IG GIVE UP] {item.get('id')} after {item.get('tries')} tries")
            continue
        try:
            ig_post(ig_id, token, item["caption"], item["urls"])
            print(f"[{key}] [IG OK retry] {item['id']}")
        except Exception as e:
            item["tries"] = item.get("tries", 0) + 1
            keep.append(item)
            print(f"[{key}] [IG retry failed {item['tries']}/{IG_MAX_TRIES}] {item['id']}: {str(e)[:200]}")
    st["ig_pending"] = keep
    save_state(state)


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
    st = state.setdefault(key, {"last_id": None, "hashes": []})

    ig_id = None
    if cfg.get("instagram"):
        ig_id = os.environ.get("IG_USER_ID", "").strip() or st.get("ig_user_id")
        if not ig_id and time.time() - st.get("ig_checked_at", 0) > 1800:
            st["ig_checked_at"] = time.time()
            ig_id = ig_discover(page_id, token)
            if ig_id:
                st["ig_user_id"] = ig_id
        if not ig_id:
            print(f"[{key}] [IG] disabled: no linked Instagram professional account found (or token lacks instagram_basic)")
        elif st.get("ig_pending"):
            ig_retry_pending(key, st, state, ig_id, token)

    wait = st.get("cooldown_until", 0) - time.time()
    if wait > 0:
        print(f"[{key}] Gemini cooldown, {int(wait)}s left - skipping this run")
        return

    items, last_id = await fetch_new(client, cfg["source"], st["last_id"])
    if st["last_id"] is None:
        st["last_id"] = last_id
    print(f"[{key}] {len(items)} new items")

    for grp in items:
        top = max(m.id for m in grp)
        kinds = [classify(m) for m in grp]
        raw = next((m.message for m in grp if m.message), "") or ""
        raw = clean_text(raw, cfg["source"])
        has_photo = any(k == "photo" for k in kinds)
        wp = next((m.media.webpage for m in grp if isinstance(m.media, MessageMediaWebPage)), None)
        is_preview = wp is not None and not any(isinstance(m.media, MessageMediaPhoto) for m in grp)
        if wp is not None:
            print(f"[{key}] preview info id={top} type={getattr(wp, 'type', None)} duration={getattr(wp, 'duration', None)} embed={bool(getattr(wp, 'embed_url', None))}")

        def done(why):
            print(f"[{key}] {why} {top}")
            st["last_id"] = top
            save_state(state)

        if "skip" in kinds:
            done("[SKIP media]"); continue
        if cfg.get("skip_rt") and is_rt(raw):
            done("[SKIP RT]"); continue
        if cfg.get("skip_preview_video") and is_preview and preview_is_video(wp):
            done("[SKIP preview video]"); continue
        if is_preview and letters_count(raw) < cfg.get("min_letters", 15):
            done("[SKIP empty quote]"); continue
        if not raw and not has_photo:
            done("[SKIP empty]"); continue
        if cfg.get("skip_podcast") and is_podcast_promo(raw):
            done("[SKIP podcast]"); continue
        if raw and h(raw) in st["hashes"]:
            done("[SKIP duplicate]"); continue

        text = ""
        if raw:
            try:
                text, skip_reason = ai_process(raw, cfg["mode"], gemini_key, cfg.get("min_score", 0))
            except Exception:
                st["cooldown_until"] = time.time() + COOLDOWN_SECONDS
                save_state(state)
                raise
            if text is None:
                done(f"[SKIP {skip_reason}]"); continue

        images = []
        for m, k in zip(grp, kinds):
            if k == "photo":
                data = await client.download_media(m, file=bytes)
                if data:
                    images.append(data)

        if not text and not images:
            done("[SKIP nothing to post]"); continue

        body = text                                   # النص بدون فوتر (لإنستغرام)
        text = f"{text}\n\n{FOOTER}" if text else FOOTER

        res = fb_post(page_id, token, text, images)   # يرمي خطأ لو فشل فلا تتقدم الحالة
        post_id = res.get("post_id") or res.get("id")

        # نحفظ التقدم فوراً (قبل التعليق وإنستغرام) حتى لا يتكرر النشر لو صار أي خلل بعدها
        if raw:
            st["hashes"] = (st["hashes"] + [h(raw)])[-100:]
        done("[OK]")
        git_persist_state()

        # التعليق الأول: لو فشل لا نفشل المنشور (وإلا ينشر مرة ثانية)
        try:
            fb_comment(token, post_id, FIRST_COMMENT)
        except Exception as e:
            print(f"[{key}] WARNING comment failed for {post_id}: {e}")

        # إنستغرام: يحتاج صورة دائماً. لو فشل نحفظه بقائمة الانتظار ويُعاد لاحقاً
        if ig_id:
            if not images:
                print(f"[{key}] [IG skip: no image] {top}")
            else:
                caption = f"{body}\n\n{IG_FOOTER}" if body else IG_FOOTER
                urls = None
                try:
                    urls = [fb_host_image(page_id, token, fit_for_instagram(b)) for b in images[:10]]
                    ig_post(ig_id, token, caption, urls)
                    print(f"[{key}] [IG OK] {top}")
                except Exception as e:
                    print(f"[{key}] [IG FAILED -> will retry] {top}: {str(e)[:200]}")
                    if urls:   # الصور رُفعت لكن النشر فشل: نعيد المحاولة لاحقاً بنفس الروابط
                        st.setdefault("ig_pending", []).append(
                            {"id": top, "caption": caption[:2200], "urls": urls, "tries": 1, "ts": int(time.time())})
        save_state(state)
        git_persist_state()


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
        git_persist_state()
        await client.disconnect()
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
