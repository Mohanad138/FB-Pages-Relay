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
from datetime import datetime, timezone
from difflib import SequenceMatcher
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
    # المفتاح الداخلي "offside" جديد عمداً: يبدأ التتبع من الصفر للمصدر الجديد
    "offside": {
        "source": "Offsideahdaff",             # يوزرنيم القناة العامة بدون @
        "page_id_env": "FB_PAGE_ID_1",
        "token_env": "FB_PAGE_TOKEN_1",
        "mode": "engage_ar",                   # engage_ar | translate_ar | rephrase_ar | copy
        "skip_podcast": False,
        "skip_rt": True,               # يتخطى الريتويت (RT @...)
        "skip_preview_video": True,    # يتخطى معاينات الفيديو
        "min_letters": 15,             # يتخطى منشورات المعاينة اللي نصها فاضي تقريباً
        "max_age_minutes": 180,        # يتخطى الأقدم من 3 ساعات. 0 = بدون حد
        "min_score": 7,                # ينشر فقط اللي يجيب تفاعل: درجة >= هذا الرقم (0 = ينشر كل شي)
        "require_photo_and_text": True,   # ينشر فقط منشور فيه صورة + نص (لا نص وحده ولا صورة وحدها)
        # تغطية المباريات الحية مرفوضة (قبل Gemini لتوفير الحصة). التشكيلة ونهاية المباراة المهمة تمر لـ Gemini
        "live_patterns": [r"نهاية\s+الشوط", r"بين\s+الشوطين", r"إحماء|احماء", r"خلف\s+الأهداف|خلف\s+الاهداف",
                          r"\bhalf[\s-]?time\b", r"\bwarm[\s-]?up\b"],
        # نصوص قصيرة جداً فيها هدف/أسيست/تبديل/بطاقة = غالباً خبر حي أثناء المباراة
        "live_short_patterns": [r"هدف|أسيست|اسيست|تبديل|بطاقة"],
        "live_short_max_len": 70,
        "instagram": True,             # ينشر نفس المنشور على إنستغرام (لو الحساب Professional ومربوط بالصفحة)
    },
}

VERSION = "2026-10-16-v9"
STATE_FILE = "state.json"
FIRST_RUN_COUNT = 5
COOLDOWN_SECONDS = 600   # بعد فشل Gemini: استراحة 10 دقائق قبل المحاولة التالية
FETCH_LIMIT = 40
GRAPH = "https://graph.facebook.com/v21.0"
# الأرخص أولاً (الترجمة بسيطة). لو انتهت حصته أو ما توفر ينتقل للي بعده (لكل موديل حصة مستقلة)
GEMINI_MODELS = ["gemini-flash-lite-latest", "gemini-flash-latest"]   # لو خلصت حصتهم يكتشف البوت موديلات ثانية متاحة بمفتاحك تلقائياً

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
    "engage_ar": """أنت محرر صفحة كرة قدم عربية هدفها جذب التفاعل وزيادة متابعي قناة تلكرام.
اقرأ المنشور (بأي لغة) وقرر:

1) إذا كان إعلاناً أو ترويجاً أو دعوة اشتراك أو بيع أو مراهنات أو كود خصم: أجب SKIP فقط.

2) أخبار الكرة العربية مرفوضة: الدوريات والأندية والمنتخبات واللاعبون والمدربون العرب (مصر، السعودية، المغرب، الجزائر، تونس، العراق، الإمارات، قطر، الأردن، الكويت...) وكل ما يخص كرة القدم المحلية العربية.
الاستثناء الوحيد: خبر عن لاعب أو مدرب مشهور عالمياً حتى لو يلعب بدوري عربي (مثل كريستيانو رونالدو، بنزيما، نيمار، ماني، ميسي، صلاح، محرز...). في حالة الرفض اكتب: SCORE: 0

3) غير ذلك أعطِ درجة من 1 إلى 10 لاحتمال تفاعل الجمهور العربي معه. أي نوع منشور مقبول (خبر، رقم قياسي، إحصائية، مقارنة، لقطة، صورة، موقف طريف) طالما سيثير التفاعل:
- 9-10: نجوم عالميون (ميسي، رونالدو، مبابي، هالاند، صلاح...) أو أندية كبرى (ريال مدريد، برشلونة، سيتي، يونايتد، ليفربول، أرسنال، تشلسي، باريس، بايرن...)، صفقات مؤكدة، أرقام قياسية، مواقف مثيرة للجدل أو الطرافة.
- 6-8: أخبار مهمة أو مثيرة للنقاش عن دوريات كبرى.
- 3-5: أخبار عادية أو أندية متوسطة.
- 1-2: هامشية أو غير مثيرة.

4) منشورات المباريات:
- مرفوض دائماً (SCORE: 0): أي تغطية حية أو أثناء المباراة: بداية أو نهاية شوط، تسجيل هدف، أسيست، تبديل، بطاقة، إحماء اللاعبين، لقطات أو إحصائيات أثناء اللعب.
- مقبول: التشكيلة الرسمية للمباراة (درجة 8 إذا لنادٍ كبير أو نجم أو مباراة مهمة، وإلا 6).
- نتيجة نهاية المباراة: مقبولة فقط إذا كانت مباراة مهمة (نهائي، دوري أبطال، ديربي، منتخبات كبرى) أو لفرق كبيرة (درجة 8-10). غير ذلك SCORE: 2.

الصيغة: السطر الأول فقط SCORE: الرقم، ثم اكتب المنشور بالعربية بأسلوب طبيعي وجذاب (إن كان بالعربية فأعد صياغته بأسلوب أفضل، وإن كان بلغة أخرى فترجمه كأن كاتباً عربياً كتبه).
- حافظ على الأسماء والأرقام والحقائق ولا تضف معلومات من عندك.
- حافظ على تقسيم الأسطر.
- بدون هاشتاغات، وبدون روابط أو إشارات لحسابات أو قنوات.
- أجب بالصيغة المطلوبة فقط بدون شرح.

المنشور:
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


def norm_text(t):
    return re.sub(r"\s+", " ", (t or "")).strip()[:400]


def _words(t):
    return set(re.findall(r"\w{3,}", t.lower()))


def is_near_duplicate(raw, recent, threshold=0.82):
    """نفس الخبر انكتب مرتين بصياغة قريبة. نتطلب تشابه النص + تطابق الكلمات (يفرّق بين خبرين بنفس القالب وأسماء مختلفة)"""
    a = norm_text(raw)
    if not a:
        return False
    wa = _words(a)
    for old in recent:
        sm = SequenceMatcher(None, a.lower(), old.lower())
        if not (sm.real_quick_ratio() >= threshold and sm.quick_ratio() >= threshold and sm.ratio() >= threshold):
            continue
        wo = _words(old)
        union = wa | wo
        if not union or len(wa & wo) / len(union) >= 0.8:
            return True
    return False


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
def discover_models(gemini_key):
    """يجلب الموديلات المتاحة بمفتاحك (لكل موديل حصة يومية مستقلة)"""
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         params={"key": gemini_key, "pageSize": 1000}, timeout=30)
        if r.status_code != 200:
            print(f"[gemini] ListModels failed {r.status_code}: {r.text[:150]}")
            return []
        names = []
        for mm in r.json().get("models", []):
            name = mm.get("name", "").replace("models/", "")
            low = name.lower()
            if "generateContent" not in mm.get("supportedGenerationMethods", []):
                continue
            if "gemini" not in low:
                continue
            if any(x in low for x in ("pro", "image", "tts", "embedding", "audio", "live",
                                      "robotics", "computer", "thinking", "vision", "exp")):
                continue
            names.append(name)
        names.sort(key=lambda n: (0 if "lite" in n else 1, 1 if "latest" in n else 0, n))
        print(f"[gemini] discovered models: {names[:8]}")
        return names[:8]
    except Exception as e:
        print(f"[gemini] ListModels error: {e}")
        return []


def gemini_generate(prompt, gemini_key):
    last = "no model tried"
    queue = list(GEMINI_MODELS)
    tried = set()
    discovered = False
    while queue:
        model = queue.pop(0)
        if model in tried:
            continue
        tried.add(model)
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
        if not queue and not discovered:
            discovered = True
            queue.extend(discover_models(gemini_key))
    raise RuntimeError(f"Gemini failed: {last}")


def parse_score(out):
    m = re.match(r"\s*SCORE\s*[:：]\s*(\d{1,2})[^\n]*\n?", out, re.I)
    if not m:
        return None, out
    return min(int(m.group(1)), 10), out[m.end():].strip()


SCORED_MODES = ("translate_ar", "engage_ar")


def ai_process(raw, mode, gemini_key, min_score=0):
    """يرجع (النص, سبب_التخطي). لو النص None فالسبب: ad أو low score N"""
    if mode == "copy":
        return raw, None
    out = strip_hashtags(gemini_generate(PROMPTS[mode] + raw, gemini_key))
    if out.upper().startswith("SKIP"):
        return None, "ad"
    if mode in SCORED_MODES:
        score, out = parse_score(out)
        if out.upper().startswith("SKIP"):
            return None, "ad"
        if mode == "translate_ar" and "here we go" in raw.lower():
            score = 10          # Here we go تُنشر دائماً
        if score is None and mode == "engage_ar":
            return None, "no score"   # بدون درجة ما نقدر نتأكد أنه مو كرة عربية: نتخطاه
        if score is not None:
            print(f"[score] {score} (min {min_score})")
            if score < min_score:
                return None, f"low score {score}"
        if not out.strip():
            return None, "empty"
        if mode == "translate_ar":
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


# ============ سجل النشر في Saved Messages (يمنع التكرار حتى لو فشل state.json) ============
LEDGER_LIMIT = 500


async def ledger_load(client):
    """يقرأ علامات FBRELAY| من رسائلك المحفوظة. يرجع None لو فشلت القراءة"""
    try:
        out = set()
        async for m in client.iter_messages("me", limit=LEDGER_LIMIT):
            t = (m.message or "").strip()
            if t.startswith("FBRELAY|"):
                out.add(t)
        return out
    except Exception as e:
        print(f"[ledger] read failed: {e}")
        return None


async def ledger_add(client, marker):
    await client.send_message("me", marker)


RUN_TAG = f"{os.environ.get('GITHUB_RUN_ID', 'local')}-{os.getpid()}"
CLAIM_WAIT = 2.0     # ثواني ننتظر فيها لو تشغيلة ثانية تحجز نفس الخبر بنفس اللحظة
CLAIM_TTL = 600      # حجز أقدم من 10 دقائق بدون نشر يُعتبر ميت (تشغيلة انهارت)


async def ledger_scan(client, limit=150):
    """آخر العلامات بسجل Saved Messages: [(id, text, date)] أو None لو فشلت القراءة"""
    try:
        out = []
        async for m in client.iter_messages("me", limit=limit):
            t = (m.message or "").strip()
            if t.startswith("FBRELAY|"):
                out.append((m.id, t, getattr(m, "date", None)))
        return out
    except Exception as e:
        print(f"[ledger] scan failed: {e}")
        return None


async def claim_item(client, key, top):
    """
    قفل بين التشغيلات المتزامنة: كل تشغيلة تكتب «حجز» للخبر بـ Saved Messages ثم تقرأ.
    الفائز هو صاحب أصغر رقم رسالة (ترتيب تيليجرام ثابت للكل). يرجع (نجح, رقم_رسالة_الحجز)
    """
    prefix = f"FBRELAY|{key}|{top}|"
    snap = await ledger_scan(client)
    if snap is None:
        return True, None            # ما قدرنا نفحص: نكمل (الحماية الباقية تشتغل)
    if any(t == prefix + "FB" for _, t, _ in snap):
        return False, None           # منشور مسبقاً
    try:
        sent = await client.send_message("me", prefix + "CLAIM|" + RUN_TAG)
    except Exception as e:
        print(f"[ledger] claim write failed: {e}")
        return True, None
    await asyncio.sleep(CLAIM_WAIT)
    snap = await ledger_scan(client)
    if snap is None:
        return True, sent.id
    if any(t == prefix + "FB" for _, t, _ in snap):
        await release_claim(client, sent.id)
        return False, None
    now = datetime.now(timezone.utc)
    claims = []
    for mid, t, dt in snap:
        if t.startswith(prefix + "CLAIM|"):
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if (now - dt).total_seconds() > CLAIM_TTL and mid != sent.id:
                    continue         # حجز ميت
            claims.append(mid)
    if not claims or min(claims) == sent.id:
        return True, sent.id
    await release_claim(client, sent.id)
    return False, None


async def release_claim(client, claim_id):
    if not claim_id:
        return
    try:
        await client.delete_messages("me", [claim_id])
    except Exception as e:
        print(f"[ledger] release failed: {e}")


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
        else:
            print(f"[{key}] [IG] enabled (account {ig_id})")
            if st.get("ig_pending"):
                ig_retry_pending(key, st, state, ig_id, token)

    wait = st.get("cooldown_until", 0) - time.time()
    if wait > 0:
        print(f"[{key}] Gemini cooldown, {int(wait)}s left - skipping this run")
        return

    items, last_id = await fetch_new(client, cfg["source"], st["last_id"])
    if st["last_id"] is None:
        st["last_id"] = last_id
    print(f"[{key}] {len(items)} new items")

    ledger, ledger_ok = set(), False
    if items:
        loaded = await ledger_load(client)
        if loaded is not None:
            ledger, ledger_ok = loaded, True
        else:
            print(f"[{key}] WARNING: could not read the Saved Messages ledger; relying on state.json only")

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

        fb_marker = f"FBRELAY|{key}|{top}|FB"
        ig_marker = f"FBRELAY|{key}|{top}|IG"
        if fb_marker in ledger:
            done("[SKIP already posted (ledger)]"); continue

        age_limit = cfg.get("max_age_minutes", 0)
        if age_limit:
            dts = [m.date for m in grp if getattr(m, "date", None)]
            if dts:
                newest = max(dts)
                if newest.tzinfo is None:
                    newest = newest.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - newest).total_seconds() > age_limit * 60:
                    done("[SKIP too old]"); continue
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
        if cfg.get("require_photo_and_text") and not (raw and has_photo):
            done("[SKIP needs photo+text]"); continue
        if raw and cfg.get("live_patterns") and re.search("|".join(cfg["live_patterns"]), raw, re.I):
            done("[SKIP live match coverage]"); continue
        if (raw and cfg.get("live_short_patterns") and len(raw) <= cfg.get("live_short_max_len", 70)
                and re.search("|".join(cfg["live_short_patterns"]), raw, re.I)):
            done("[SKIP live match flash]"); continue
        if raw and h(raw) in st["hashes"]:
            done("[SKIP duplicate]"); continue
        if raw and is_near_duplicate(raw, st.get("recent_raw", [])):
            done("[SKIP near-duplicate]"); continue

        # حجز الخبر: لو تشغيلة ثانية تشتغل بنفس اللحظة ما ننشر نفس الخبر مرتين
        ok_claim, claim_id = await claim_item(client, key, top)
        if not ok_claim:
            print(f"[{key}] [SKIP: already posted or claimed by another run] {top}")
            continue

        text = ""
        if raw:
            try:
                text, skip_reason = ai_process(raw, cfg["mode"], gemini_key, cfg.get("min_score", 0))
            except Exception:
                st["cooldown_until"] = time.time() + COOLDOWN_SECONDS
                save_state(state)
                await release_claim(client, claim_id)
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

        try:
            res = fb_post(page_id, token, text, images)   # يرمي خطأ لو فشل فلا تتقدم الحالة
        except Exception:
            await release_claim(client, claim_id)         # ما اننشر: نحرر الحجز ليُعاد لاحقاً
            raise
        post_id = res.get("post_id") or res.get("id")

        # نسجّل النشر فوراً بسجل تلكرام (أقوى حماية من التكرار)
        try:
            await ledger_add(client, fb_marker)
            ledger.add(fb_marker)
        except Exception as e:
            print(f"[{key}] WARNING ledger write failed: {e}")

        # نحفظ التقدم فوراً (قبل التعليق وإنستغرام) حتى لا يتكرر النشر لو صار أي خلل بعدها
        if raw:
            st["hashes"] = (st["hashes"] + [h(raw)])[-100:]
            st["recent_raw"] = (st.get("recent_raw", []) + [norm_text(raw)])[-40:]
        done("[OK]")
        git_persist_state()

        # التعليق الأول: لو فشل لا نفشل المنشور (وإلا ينشر مرة ثانية)
        try:
            fb_comment(token, post_id, FIRST_COMMENT)
        except Exception as e:
            print(f"[{key}] WARNING comment failed for {post_id}: {e}")

        # إنستغرام: يحتاج صورة دائماً. لو فشل نحفظه بقائمة الانتظار ويُعاد لاحقاً
        if ig_id:
            if ig_marker in ledger:
                print(f"[{key}] [IG skip: already posted] {top}")
            elif not images:
                print(f"[{key}] [IG skip: no image] {top}")
            else:
                caption = f"{body}\n\n{IG_FOOTER}" if body else IG_FOOTER
                urls = None
                try:
                    urls = [fb_host_image(page_id, token, fit_for_instagram(b)) for b in images[:10]]
                    ig_post(ig_id, token, caption, urls)
                    print(f"[{key}] [IG OK] {top}")
                    try:
                        await ledger_add(client, ig_marker)
                        ledger.add(ig_marker)
                    except Exception as e:
                        print(f"[{key}] WARNING ledger write failed (IG): {e}")
                except Exception as e:
                    print(f"[{key}] [IG FAILED -> will retry] {top}: {str(e)[:200]}")
                    if urls:   # الصور رُفعت لكن النشر فشل: نعيد المحاولة لاحقاً بنفس الروابط
                        st.setdefault("ig_pending", []).append(
                            {"id": top, "caption": caption[:2200], "urls": urls, "tries": 1, "ts": int(time.time())})
        save_state(state)
        git_persist_state()


async def diagnostics(client, gemini_key):
    """تقرير فحص يطبع بالـ log عند التشغيل اليدوي (Run workflow)"""
    print("=========== [check] تقرير الفحص ===========")
    print(f"[check] version {VERSION}")
    for key, cfg in PAGES.items():
        pid = os.environ.get(cfg["page_id_env"], "").strip()
        tok = os.environ.get(cfg["token_env"], "").strip()
        print(f"[check] {key}: page id {'set' if pid else 'MISSING'} | token {'set' if tok else 'MISSING'} (len {len(tok)})")
        if pid and tok:
            try:
                b = requests.get(f"{GRAPH}/{pid}", params={
                    "fields": "name,instagram_business_account{id,username}", "access_token": tok}, timeout=30).json()
                if "error" in b:
                    e = b["error"]
                    print(f"[check] Facebook/Instagram ERROR code={e.get('code')}: {str(e.get('message'))[:200]}")
                else:
                    print(f"[check] Facebook page: {b.get('name')}")
                    ig = b.get("instagram_business_account")
                    if ig:
                        print(f"[check] Instagram linked: @{ig.get('username')} (id {ig.get('id')})")
                        b2 = requests.get(f"{GRAPH}/{ig['id']}/content_publishing_limit", params={
                            "fields": "quota_usage,config", "access_token": tok}, timeout=30).json()
                        if "error" in b2:
                            print(f"[check] Instagram publishing PERMISSION PROBLEM: {str(b2['error'].get('message'))[:200]}")
                            print("[check] -> ولّد توكن جديد بإذني instagram_basic و instagram_content_publish وحدّث FB_PAGE_TOKEN_1")
                        else:
                            print(f"[check] Instagram publishing permission OK: {b2.get('data')}")
                    else:
                        print("[check] Instagram: NOT linked to this page (حوّل الحساب Professional واربطه بالصفحة) أو التوكن ينقصه instagram_basic")
            except Exception as e:
                print(f"[check] Facebook check failed: {e}")
        try:
            msgs = await client.get_messages(cfg["source"], limit=1)
            if msgs:
                print(f"[check] Telegram source @{cfg['source']}: latest id {msgs[0].id}")
            else:
                print(f"[check] Telegram source @{cfg['source']}: no messages")
        except Exception as e:
            print(f"[check] Telegram source error: {e}")
    snap = await ledger_scan(client, 300)
    print(f"[check] Saved Messages ledger markers: {None if snap is None else len(snap)}")
    try:
        out = gemini_generate("Reply with the single word: ok", gemini_key)
        print(f"[check] Gemini OK: {out.strip()[:20]}")
    except Exception as e:
        print(f"[check] Gemini FAILED: {str(e)[:250]}")
    print("========================================")


async def main():
    print(f"[bot] version {VERSION}")
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
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        await diagnostics(client, gemini_key)
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
