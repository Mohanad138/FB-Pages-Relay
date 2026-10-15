import os
import re
import sys
import json
import asyncio
import hashlib
import requests
from telethon import TelegramClient
from telethon.sessions import StringSession

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
    },
}

STATE_FILE = "state.json"
FIRST_RUN_COUNT = 5
FETCH_LIMIT = 40
GRAPH = "https://graph.facebook.com/v21.0"
GEMINI_MODEL = "gemini-flash-latest"

PROMPTS = {
    "translate_ar": """أنت محرر أخبار كرة قدم عربي. اكتب الخبر التالي بالعربية كأن شخصاً عربياً كتبه أصلاً، وليس ترجمة حرفية كلمة بكلمة.
- حافظ على نفس تقسيم الأسطر والفراغات الموجودة في النص الأصلي.
- لا تترجم الهاشتاغات. احذفها، وأضف في آخر المنشور 5 هاشتاغات عربية مناسبة للخبر.
- لا تضع أي روابط أو إشارات لحسابات أو قنوات.
- إذا كان المنشور إعلاناً أو ترويجاً (سعر، خصم، اشتراك، رعاية، كود خصم، رابط شراء) أجب بكلمة SKIP فقط. الخبر العادي ليس إعلاناً، وعند الشك انشره.
- أجب بنص المنشور النهائي فقط بدون أي شرح.

النص الأصلي:
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
def clean_text(text, source):
    t = text or ""
    t = re.sub(r"https?://t\.co/\S+", "", t)
    t = re.sub(r"https?://(www\.)?(t\.me|telegram\.me)/\S+", "", t)
    t = re.sub(r"@Sabereen_Social", "", t, flags=re.I)
    t = re.sub(r"@" + re.escape(source) + r"\b", "", t, flags=re.I)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


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
def ai_process(raw, mode, gemini_key):
    if mode == "copy":
        return raw
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        params={"key": gemini_key},
        json={"contents": [{"parts": [{"text": PROMPTS[mode] + raw}]}]},
        timeout=90,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Gemini error {r.status_code}: {r.text[:300]}")
    out = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
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

        def done(why):
            print(f"[{key}] {why} {top}")
            st["last_id"] = top
            save_state(state)

        if "skip" in kinds:
            done("[SKIP media]"); continue
        if not raw and not has_photo:
            done("[SKIP empty]"); continue
        if cfg.get("skip_podcast") and is_podcast_promo(raw):
            done("[SKIP podcast]"); continue
        if raw and h(raw) in st["hashes"]:
            done("[SKIP duplicate]"); continue

        text = ""
        if raw:
            text = ai_process(raw, cfg["mode"], gemini_key)
            if text is None:
                done("[SKIP ad]"); continue

        images = []
        for m, k in zip(grp, kinds):
            if k == "photo":
                data = await client.download_media(m, file=bytes)
                if data:
                    images.append(data)

        if not text and not images:
            done("[SKIP nothing to post]"); continue

        fb_post(page_id, token, text, images)   # يرمي خطأ لو فشل فلا تتقدم الحالة
        if raw:
            st["hashes"] = (st["hashes"] + [h(raw)])[-100:]
        done("[OK]")


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
