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

# ==================== إعدادات ====================
PAGES = {
    "offside": {
        "source": "Offsideahdaff",
        "page_id_env": "FB_PAGE_ID_1",
        "token_env": "FB_PAGE_TOKEN_1",
        "ig_id_env": "IG_USER_ID_1",
        "mode": "translate_ar",
        "skip_podcast": True,
        "skip_rt": True,
        "skip_preview_video": True,
        "min_letters": 15,
        "min_score": 8,
    },
}

STATE_FILE = "state.json"
FIRST_RUN_COUNT = 5
FETCH_LIMIT = 40
COOLDOWN_SECONDS = 300
GRAPH = "https://graph.facebook.com/v21.0"
GEMINI_MODELS = ["gemini-flash-lite-latest", "gemini-flash-latest", "gemma-3-27b-it"]
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "FB-Pages-Relay/2.0"})

FB_FOOTER = (
    "----------------------\n"
    "يمكنكم متابعة الأخبار لحظة بلحظة عبر قناتنا على التلكرام\n"
    "رابط القناة في أول تعليق"
)
IG_FOOTER = (
    "----------------------\n"
    "يمكنكم متابعة الأخبار لحظة بلحظة عبر قناتنا على التلكرام\n"
    "رابط القناة في البايو"
)
FIRST_COMMENT = "تابعنا على التلكرام 👇\nhttps://t.me/FabriAr1"

PROMPTS = {
    "translate_ar": """أنت محرر صفحة كرة قدم عربية هدفها جذب تفاعل حقيقي وجلب متابعين لقناة تلغرام.
انشر فقط الأخبار المهمة جداً التي تستحق اهتمام الجمهور، وقيّمها من 1 إلى 10:
9-10: صفقة رسمية أو Here We Go، انتقال نجم عالمي، خبر عاجل كبير، إقالة/تعيين مدرب كبير، خبر مهم لريال مدريد أو برشلونة أو الأندية الكبرى.
8: خبر قوي جداً عن نجم معروف، مفاوضات متقدمة، تجديد مهم أو إصابة مؤثرة.
1-7: لا تنشرها؛ أجب SKIP فقط.

مهم جداً: لا تنشر تغطية المباريات أو التحديثات المباشرة إطلاقاً: الأهداف أثناء اللعب، النتيجة، الدقائق، نهاية الشوط، صافرة النهاية، التشكيلات، مجريات المباراة أو ملخصاتها. أجب SKIP فقط لهذه المنشورات حتى لو كان الفريق كبيراً.
لا تنشر أخباراً عادية أو شائعات ضعيفة أو أخباراً هامشية أو إعلانات وترويجاً.
إذا اجتاز الخبر الشروط، أخرج أول سطر SCORE: رقم، ثم صياغة عربية طبيعية وجذابة دون مبالغة أو اختلاق.
حافظ على الأرقام والتفاصيل. لا هاشتاغات ولا روابط ولا إشارات حسابات.
أجب بالصيغة المطلوبة فقط دون شرح.

النص:
""",
    "rephrase_ar": """أعد صياغة الخبر بالعربية بأسلوب صحفي طبيعي، مع الحفاظ على التفاصيل وعدم اختلاق معلومات.
لا تنشر تغطية المباريات أو النتائج المباشرة أو الأهداف أو التشكيلات أو مجريات المباراة؛ أجب SKIP فقط. إذا كان الخبر هامشياً أو إعلاناً أجب SKIP فقط.
أخرج النص النهائي فقط.

النص الأصلي:
""",
}


def need(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Secret missing or empty: {name}")
    return value


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        # لا نصفر الحالة تلقائياً حتى لا نعيد نشر القديم.
        raise RuntimeError(f"Cannot read {STATE_FILE}; backup/fix it first: {exc}")


def save_state(state):
    temp = STATE_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, STATE_FILE)


def h(text):
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()


def strip_hashtags(text):
    text = re.sub(r"(?<!\S)#[^\s#]+", "", text or "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_text(text, source):
    text = text or ""
    text = re.sub(r"[—–-]\s*Fabrizio Romano\s*\(@FabrizioRomano\)\s*(?:[A-Z][a-z]{2,8}\.? \d{1,2}, \d{4})?", "", text)
    text = re.sub(r"https?://t\.co/\S+", "", text)
    text = re.sub(r"https?://(www\.)?(t\.me|telegram\.me)/\S+", "", text)
    text = re.sub(r"@Sabereen_Social", "", text, flags=re.I)
    text = re.sub(r"@" + re.escape(source) + r"\b", "", text, flags=re.I)
    return strip_hashtags(text)


def is_rt(text):
    return bool(re.match(r"\s*RT\s+@", text or "", re.I))


def letters_count(text):
    text = re.sub(r"@\w+", "", text or "")
    text = re.sub(r"https?://\S+", "", text)
    return len(re.findall(r"[^\W\d_]", text))


def preview_is_video(webpage):
    if webpage is None:
        return False
    kind = (getattr(webpage, "type", "") or "").lower()
    return kind in ("video", "gif") or bool(getattr(webpage, "duration", None)) or bool(getattr(webpage, "embed_url", None))


def is_match_coverage(text):
    """استبعاد تغطية المباريات والنتائج والتحديثات الحية قبل إرسالها للذكاء الاصطناعي."""
    t = (text or "").lower()
    patterns = [
        r"\b(live|live update|matchday|kick[- ]?off|half[- ]?time|full[- ]?time|line[- ]?ups?|starting xi|ft|ht)\b",
        r"\bgoal!?\s*(for|to)\b",
        r"(?:انطلاق|بداية) المباراة",
        r"(?:مباشر|عاجل من المباراة|تغطية مباشرة|متابعة مباشرة)",
        r"(?:تشكيلة|التشكيل الرسمي|التشكيلات الرسمية)",
        r"(?:نهاية الشوط الأول|نهاية الشوط الثاني|صافرة النهاية|انتهت المباراة|انتهاء المباراة)",
        r"(?:هدف!|جوووول|يسجل هدف|يحرز هدف|هدف لصالح)",
        r"(?:الدقيقة\s*\d{1,3}|\d{1,3}\s*['’′]\s*(?:دقيقة|د))",
        r"(?:نتيجة المباراة|نتيجة اللقاء|النتيجة الآن|يتقدم .*? بهدف|التعادل .*? هدف)",
        r"(?:ملخص المباراة|أبرز لقطات المباراة|مجريات المباراة)",
    ]
    return any(re.search(pattern, t, re.I) for pattern in patterns)


def is_podcast_promo(text):
    text = (text or "").lower()
    return bool(re.match(r"\s*new pod", text)) and "here we go" not in text


def enforce_here_we_go(raw, output):
    has = "here we go" in raw.lower()
    output = re.sub(r"^\s*هير\s*وي\s*(كو|غو)\s*", "", output)
    return ("هير وي كو\n" + output.lstrip()) if has else output


# ==================== Gemini ====================
def gemini_generate(prompt, key):
    last_error = "no model tried"
    for model in GEMINI_MODELS:
        for attempt in range(3):
            try:
                response = HTTP.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    params={"key": key},
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 700},
                    },
                    timeout=90,
                )
            except requests.RequestException as exc:
                last_error = f"{model}: network error: {exc}"
                if attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                break

            if response.status_code == 200:
                data = response.json()
                try:
                    return data["candidates"][0]["content"]["parts"][0]["text"]
                except (KeyError, IndexError, TypeError):
                    raise RuntimeError(f"Gemini returned no text ({model}): {str(data)[:300]}")

            last_error = f"{model} -> {response.status_code}: {response.text[:250]}"
            print("[gemini]", last_error)
            transient = response.status_code in (500, 503) or (
                response.status_code == 429 and "PerDay" not in response.text
            )
            if transient and attempt < 2:
                time.sleep(8 * (attempt + 1))
                continue
            break

        if "-> 429" not in last_error and "-> 404" not in last_error and "-> 500" not in last_error and "-> 503" not in last_error:
            break
    raise RuntimeError(f"Gemini failed: {last_error}")


def parse_score(output):
    match = re.match(r"\s*SCORE\s*[:：]\s*(\d{1,2})[^\n]*\n?", output, re.I)
    if not match:
        return None, output
    return min(int(match.group(1)), 10), output[match.end():].strip()


def ai_process(raw, mode, gemini_key, min_score=0):
    if mode == "copy":
        return raw, None
    output = strip_hashtags(gemini_generate(PROMPTS[mode] + raw, gemini_key))
    if output.upper().startswith("SKIP"):
        return None, "ad"
    if mode == "translate_ar":
        score, output = parse_score(output)
        if output.upper().startswith("SKIP"):
            return None, "ad"
        if "here we go" in raw.lower():
            score = 10
        if score is None:
            # لا ننشر خبراً لم يستطع النموذج تقييمه، حتى لا تتجاوز الأخبار الهامشية الفلتر.
            print("[score] Missing score; skipping safely")
            return None, "missing score"
        print(f"[score] {score} (minimum {min_score})")
        if score < min_score:
            return None, f"low score {score}"
        output = enforce_here_we_go(raw, output)
    return output, None


# ==================== Facebook ====================
def graph_check(response, platform="Meta"):
    try:
        body = response.json()
    except ValueError:
        raise RuntimeError(f"{platform} non-JSON response {response.status_code}: {response.text[:300]}")
    if response.status_code not in (200, 201) or "error" in body:
        error = body.get("error", {})
        if error.get("code") == 190:
            raise RuntimeError(f"{platform}: access token invalid/expired (code 190)")
        raise RuntimeError(f"{platform} API error {response.status_code}: {body}")
    return body


def fb_post(page_id, token, text, images):
    base = f"{GRAPH}/{page_id}"
    if not images:
        return graph_check(HTTP.post(
            f"{base}/feed",
            data={"message": text, "access_token": token},
            timeout=60,
        ), "Facebook")

    if len(images) == 1:
        return graph_check(HTTP.post(
            f"{base}/photos",
            data={"access_token": token, "message": text},
            files={"source": ("image.jpg", images[0], "image/jpeg")},
            timeout=120,
        ), "Facebook")

    photo_ids = []
    for image_data in images[:10]:
        result = graph_check(HTTP.post(
            f"{base}/photos",
            data={"access_token": token, "published": "false"},
            files={"source": ("image.jpg", image_data, "image/jpeg")},
            timeout=120,
        ), "Facebook")
        photo_ids.append(result["id"])

    data = {"access_token": token, "message": text}
    for index, photo_id in enumerate(photo_ids):
        data[f"attached_media[{index}]"] = json.dumps({"media_fbid": photo_id})
    return graph_check(HTTP.post(f"{base}/feed", data=data, timeout=60), "Facebook")


def fb_comment(token, post_id, message):
    return graph_check(HTTP.post(
        f"{GRAPH}/{post_id}/comments",
        data={"message": message, "access_token": token},
        timeout=60,
    ), "Facebook comment")


def fb_photo_public_url(photo_id, token):
    """يستخرج رابط صورة فيسبوك العام لاستخدامه كمصدر لصورة إنستغرام."""
    result = graph_check(HTTP.get(
        f"{GRAPH}/{photo_id}",
        params={"fields": "images", "access_token": token},
        timeout=30,
    ), "Facebook photo lookup")
    images = result.get("images") or []
    if not images:
        raise RuntimeError("Facebook photo lookup returned no public image URL")
    # غالباً أول عنصر أكبر صورة؛ نجربه أولاً.
    return images[0].get("source")


# ==================== Instagram Graph API ====================
def instagram_publish(ig_user_id, token, image_url, caption):
    if not image_url or not image_url.startswith("https://"):
        raise RuntimeError("Instagram requires a publicly reachable HTTPS image URL")

    created = graph_check(HTTP.post(
        f"{GRAPH}/{ig_user_id}/media",
        data={
            "image_url": image_url,
            "caption": caption[:2200],
            "access_token": token,
        },
        timeout=60,
    ), "Instagram create media")
    creation_id = created.get("id")
    if not creation_id:
        raise RuntimeError(f"Instagram did not return a container ID: {created}")

    # انتظر معالجة الحاوية قبل النشر.
    for attempt in range(8):
        status = graph_check(HTTP.get(
            f"{GRAPH}/{creation_id}",
            params={"fields": "status_code,status", "access_token": token},
            timeout=30,
        ), "Instagram container status")
        code = status.get("status_code")
        if code == "FINISHED":
            break
        if code == "ERROR" or code == "EXPIRED":
            raise RuntimeError(f"Instagram media processing failed: {status}")
        time.sleep(3 + attempt)
    else:
        raise RuntimeError("Instagram media container did not finish processing in time")

    return graph_check(HTTP.post(
        f"{GRAPH}/{ig_user_id}/media_publish",
        data={"creation_id": creation_id, "access_token": token},
        timeout=60,
    ), "Instagram publish")


def retry_pending_instagram(key, cfg, state):
    """إعادة محاولة منشورات إنستغرام المعلقة فقط، دون إعادة نشر فيسبوك."""
    ig_env = cfg.get("ig_id_env")
    pending = state.setdefault(key, {}).setdefault("ig_pending", [])
    if not pending:
        return
    if not ig_env or not os.environ.get(ig_env, "").strip():
        print(f"[{key}] Instagram ID missing; {len(pending)} pending item(s) retained")
        return

    ig_id = os.environ[ig_env].strip()
    token = need(cfg["token_env"])
    remaining = []
    for item in pending:
        try:
            result = instagram_publish(ig_id, token, item["image_url"], item["caption"])
            print(f"[{key}] Instagram published pending item: {result.get('id')}")
        except Exception as exc:
            item["attempts"] = int(item.get("attempts", 0)) + 1
            item["last_error"] = str(exc)[:500]
            remaining.append(item)
            print(f"[{key}] Instagram retry failed (attempt {item['attempts']}): {exc}")
    state[key]["ig_pending"] = remaining
    save_state(state)


# ==================== Telegram ====================
def classify(message):
    if message.video or message.gif or message.document or message.audio or message.voice or message.video_note or message.sticker:
        return "skip"
    if message.photo:
        return "photo"
    if message.media is not None and message.web_preview is None:
        return "skip"
    return "text"


def group_messages(messages):
    items, seen = [], {}
    for message in messages:
        if message.grouped_id:
            if message.grouped_id in seen:
                seen[message.grouped_id].append(message)
            else:
                seen[message.grouped_id] = [message]
                items.append(seen[message.grouped_id])
        else:
            items.append([message])
    return items


async def fetch_new(client, source, last_id):
    if last_id is None:
        latest = [message async for message in client.iter_messages(source, limit=FIRST_RUN_COUNT)]
        if not latest:
            return [], 0
        last_id = min(message.id for message in latest) - 1
    messages = [
        message async for message in client.iter_messages(
            source, min_id=last_id, reverse=True, limit=FETCH_LIMIT
        )
    ]
    return group_messages(messages), last_id


async def run_page(client, key, cfg, state, gemini_key):
    page_id = need(cfg["page_id_env"])
    token = need(cfg["token_env"])
    st = state.setdefault(key, {"last_id": None, "hashes": [], "ig_pending": []})

    retry_pending_instagram(key, cfg, state)

    wait = float(st.get("cooldown_until", 0)) - time.time()
    if wait > 0:
        print(f"[{key}] Gemini cooldown, {int(wait)}s left")
        return

    items, initial_last_id = await fetch_new(client, cfg["source"], st.get("last_id"))
    if st.get("last_id") is None:
        st["last_id"] = initial_last_id
        save_state(state)
    print(f"[{key}] {len(items)} new Telegram item(s)")

    for group in items:
        top_id = max(message.id for message in group)
        kinds = [classify(message) for message in group]
        raw = clean_text(next((message.message for message in group if message.message), ""), cfg["source"])
        has_photo = any(kind == "photo" for kind in kinds)
        webpage = next(
            (message.media.webpage for message in group if isinstance(message.media, MessageMediaWebPage)),
            None,
        )
        is_preview = webpage is not None and not any(
            isinstance(message.media, MessageMediaPhoto) for message in group
        )

        def mark_done(reason):
            print(f"[{key}] {reason} {top_id}")
            st["last_id"] = top_id
            save_state(state)

        if "skip" in kinds:
            mark_done("[SKIP unsupported media]")
            continue
        if cfg.get("skip_rt") and is_rt(raw):
            mark_done("[SKIP retweet]")
            continue
        if cfg.get("skip_preview_video") and is_preview and preview_is_video(webpage):
            mark_done("[SKIP preview video]")
            continue
        if is_preview and letters_count(raw) < cfg.get("min_letters", 15):
            mark_done("[SKIP short preview]")
            continue
        if not raw and not has_photo:
            mark_done("[SKIP empty]")
            continue
        if is_match_coverage(raw):
            mark_done("[SKIP match coverage]")
            continue
        if cfg.get("skip_podcast") and is_podcast_promo(raw):
            mark_done("[SKIP podcast]")
            continue
        if raw and h(raw) in st.setdefault("hashes", []):
            mark_done("[SKIP duplicate text]")
            continue

        # منع إعادة نشر المجموعة نفسها بعد نجاح Facebook وتوقف التشغيل قبل تحديث last_id.
        post_key = str(top_id)
        posts = st.setdefault("posts", {})
        previous = posts.get(post_key, {})
        if previous.get("facebook_status") == "published":
            print(f"[{key}] Facebook already published for source {top_id}; not reposting")
            st["last_id"] = top_id
            save_state(state)
            continue
        if previous.get("facebook_status") == "inflight":
            # نتيجة طلب سابق غير مؤكدة؛ لا نكرر النشر تلقائياً لتجنب التكرار.
            print(f"[{key}] Facebook status uncertain for source {top_id}; manual check required")
            st["last_id"] = top_id
            save_state(state)
            continue

        text = ""
        if raw:
            try:
                text, skip_reason = ai_process(raw, cfg["mode"], gemini_key, cfg.get("min_score", 0))
            except Exception:
                st["cooldown_until"] = time.time() + COOLDOWN_SECONDS
                save_state(state)
                raise
            if text is None:
                mark_done(f"[SKIP {skip_reason}]")
                continue

        image_bytes = []
        for message, kind in zip(group, kinds):
            if kind == "photo":
                data = await client.download_media(message, file=bytes)
                if data:
                    image_bytes.append(data)

        if not text and not image_bytes:
            mark_done("[SKIP nothing to post]")
            continue

        # لكل منصة خاتمة مختلفة: رابط أول تعليق لفيسبوك، والبايو لإنستغرام.
        fb_caption = f"{text}\n\n{FB_FOOTER}" if text else FB_FOOTER
        ig_caption = f"{text}\n\n{IG_FOOTER}" if text else IG_FOOTER

        # احفظ حالة inflight قبل الطلب: إذا انقطع الاتصال بعد قبول Meta للطلب،
        # لن نعيد النشر تلقائياً وننشئ نسخة مكررة.
        posts[post_key] = {
            "source_id": top_id,
            "source_hash": h(raw) if raw else "",
            "facebook_status": "inflight",
            "instagram_status": "not_started",
            "created_at": int(time.time()),
        }
        save_state(state)

        try:
            fb_result = fb_post(page_id, token, fb_caption, image_bytes)
        except Exception as exc:
            posts[post_key]["facebook_error"] = str(exc)[:500]
            # قد تكون نتيجة الطلب غير مؤكدة؛ لا نعيد الطلب تلقائياً.
            save_state(state)
            raise RuntimeError(
                f"Facebook post failed or outcome uncertain for Telegram {top_id}. "
                f"Check Page before retrying to avoid duplicate: {exc}"
            )

        fb_post_id = fb_result.get("post_id") or fb_result.get("id")
        posts[post_key]["facebook_status"] = "published"
        posts[post_key]["facebook_post_id"] = fb_post_id
        save_state(state)
        print(f"[{key}] Facebook published: {fb_post_id}")

        try:
            fb_comment(token, fb_post_id, FIRST_COMMENT)
            posts[post_key]["comment_status"] = "published"
        except Exception as exc:
            posts[post_key]["comment_status"] = "failed"
            posts[post_key]["comment_error"] = str(exc)[:300]
            print(f"[{key}] WARNING: first comment failed: {exc}")

        # Instagram Graph API يحتاج صورة ذات رابط HTTPS عام. ننشر الصور فقط.
        ig_env = cfg.get("ig_id_env")
        if ig_env and os.environ.get(ig_env, "").strip():
            if image_bytes:
                try:
                    # صورة واحدة: استخرج رابطها من Facebook بعد رفعها.
                    # مع الألبوم نستخدم أول صورة منشورة/مرفوعة كمصدر إنستغرام.
                    photo_id = fb_result.get("id") if len(image_bytes) == 1 else None
                    if not photo_id and fb_post_id:
                        # المنشور متعدد الصور لا يعيد دائماً معرف الصورة؛ جرّب قراءة المرفقات.
                        attachments = graph_check(HTTP.get(
                            f"{GRAPH}/{fb_post_id}",
                            params={
                                "fields": "attachments{media}",
                                "access_token": token,
                            },
                            timeout=30,
                        ), "Facebook attachments")
                        data = attachments.get("attachments", {}).get("data", [])
                        for attachment in data:
                            media = attachment.get("media", {})
                            if media.get("id"):
                                photo_id = media["id"]
                                break
                    if not photo_id:
                        raise RuntimeError(
                            "Could not get Facebook photo ID for Instagram. "
                            "Facebook post succeeded; Instagram will need a retry/manual check."
                        )
                    image_url = fb_photo_public_url(photo_id, token)
                    if not image_url:
                        raise RuntimeError("Facebook did not return an image URL")
                    ig_result = instagram_publish(
                        os.environ[ig_env].strip(), token, image_url, ig_caption
                    )
                    posts[post_key]["instagram_status"] = "published"
                    posts[post_key]["instagram_post_id"] = ig_result.get("id")
                    print(f"[{key}] Instagram published: {ig_result.get('id')}")
                except Exception as exc:
                    posts[post_key]["instagram_status"] = "pending"
                    posts[post_key]["instagram_error"] = str(exc)[:500]
                    # نخزن الرابط إن توفر؛ إذا لم يتوفر نحتفظ بالمعلومة لتشخيصها ولا نعيد Facebook.
                    if "image_url" in locals() and image_url:
                        st.setdefault("ig_pending", []).append({
                            "source_id": top_id,
                            "image_url": image_url,
                            "caption": ig_caption,
                            "attempts": 0,
                        })
                    print(f"[{key}] Instagram failed; Facebook will NOT be reposted: {exc}")
            else:
                posts[post_key]["instagram_status"] = "skipped_no_image"
                print(f"[{key}] Instagram skipped: Graph API image posts require an image")
        else:
            posts[post_key]["instagram_status"] = "disabled_no_ig_id"

        if raw:
            st["hashes"] = (st["hashes"] + [h(raw)])[-300:]
        st["last_id"] = top_id
        save_state(state)


async def main():
    api_id = int(need("TG_API_ID"))
    api_hash = need("TG_API_HASH")
    session = need("TG_SESSION")
    gemini_key = need("GEMINI_API_KEY")

    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("TG_SESSION is not authorized; generate a new session string")

    state = load_state()
    failed = False
    try:
        for key, config in PAGES.items():
            try:
                await run_page(client, key, config, state, gemini_key)
            except Exception as exc:
                failed = True
                print(f"[{key}] FAILED: {exc}")
    finally:
        save_state(state)
        await client.disconnect()

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
