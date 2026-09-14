# Setting up your lyric database (one-time, ~5 minutes)

This creates the cloud Postgres database where your lyric corpus lives and grows.
You do steps 1–3; Claude has already written the code that uses it (schema and
all). Nothing here touches your Spotify/Genius setup.

---

## 1. Create a free Postgres database (Neon — recommended)

1. Go to **https://neon.tech** and click **Sign up** (GitHub or Google is fastest).
2. It'll prompt you to create a **project**. Give it a name like `playlist` and
   pick the **region closest to you** (lower latency). Click **Create**.
3. Neon creates a database and shows a **Connection string**. It looks like:
   ```
   postgresql://<user>:<password>@<host>.neon.tech/<db>?sslmode=require
   ```
   Click **Copy**. (If it offers "Pooled connection", copy that one.)

> Prefer Supabase? Works identically — supabase.com → New project → Project
> Settings → Database → Connection string (URI). Copy that instead.

**Treat this string like a password** — it contains your DB credentials. Don't
paste it into code or commit it. It goes in an environment variable, like your
Spotify/Genius tokens.

---

## 2. Add it to your shell (same pattern as your other secrets)

Open your shell config:
```bash
open -e ~/.zshrc
```

Add this line (paste your real connection string inside the quotes):
```bash
export DATABASE_URL='postgresql://...paste the whole thing...'
```

Save, then reload:
```bash
source ~/.zshrc
```

Verify it's set (won't print the secret):
```bash
echo ${DATABASE_URL:+set and non-empty}
```
That should print `set and non-empty`.

---

## 3. That's it — first run creates everything

The code creates the table automatically the first time it connects, so there's
nothing to run by hand. Just start the app as usual:
```bash
cd ~/Development/playlist/playlist-generator && python3 -m uvicorn app:app --port 8000
```

From then on:
- Every run **reads** from the cache first (no re-fetching songs you've seen).
- Every run **writes** newly fetched lyrics into it — so your corpus grows.
- Genius scraping trends toward zero over time.

If `DATABASE_URL` is **not** set, the app still works exactly as before — it just
runs without the cache (fetches every time). So nothing breaks if you skip this.

---

## What Claude built to use it
- A `LyricStore` that checks the DB before any Genius fetch and writes results
  back (including "no lyrics" so junk is never re-fetched).
- Genius discovery is back **on** by default — the cache makes both engines
  affordable again.
- `cache.db`-style local files and the connection string are gitignored.

## Notes
- **Free tier is plenty:** lyrics are tiny text; a free Neon/Supabase DB holds
  hundreds of thousands of songs before you'd pay anything.
- **It's yours:** standard Postgres — you can export/dump the whole corpus anytime.
- **Backed up:** Neon/Supabase snapshot it, so a dead laptop won't lose your corpus.
