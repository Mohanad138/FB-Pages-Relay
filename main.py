ظز     data = await client.download_media(m, file=bytes)
                if data:
                    images.append(data)

        if not text and not images:
            done("[SKIP nothing to post]"); continue

        text = f"{text}\n\n{FOOTER}" if text else FOOTER

        res = fb_post(page_id, token, text, images)   # يرمي خطأ لو فشل فلا تتقدم الحالة
        post_id = res.get("post_id") or res.get("id")

        # التعليق الأول: لو فشل لا نفشل المنشور (وإلا ينشر مرة ثانية)
        try:
            fb_comment(token, post_id, FIRST_COMMENT)
        except Exception as e:
            print(f"[{key}] WARNING comment failed for {post_id}: {e}")

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
