from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
import httpx
import os
import tempfile
import secrets
import time
from groq import Groq

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

groq_client       = Groq(api_key=os.environ.get("GROQ_API_KEY"))
YOUTUBE_API_KEY   = os.environ.get("YOUTUBE_API_KEY")
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID")
APP_URL           = "https://lyrics-teleprompter-ebon.vercel.app"
BACKEND_URL       = "https://lyrics-backend-production.up.railway.app"

# Temporary token store — { code: { token, refresh, expires_at } }
token_store = {}

def clean_store():
    now = time.time()
    expired = [k for k, v in token_store.items() if v["stored_at"] + 120 < now]
    for k in expired:
        del token_store[k]

@app.get("/")
def root():
    return {"status": "ok"}

@app.get("/callback")
async def spotify_callback(request: Request):
    code     = request.query_params.get("code")
    error    = request.query_params.get("error")
    verifier = request.query_params.get("state", "")

    if error or not code:
        return RedirectResponse(f"{APP_URL}?spotify_error=1")

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://accounts.spotify.com/api/token",
                data={
                    "grant_type":    "authorization_code",
                    "code":          code,
                    "redirect_uri":  f"{BACKEND_URL}/callback",
                    "client_id":     SPOTIFY_CLIENT_ID,
                    "code_verifier": verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10,
            )

        if r.status_code != 200:
            return RedirectResponse(f"{APP_URL}?spotify_error=1")

        data          = r.json()
        token         = data.get("access_token", "")
        expires_in    = data.get("expires_in", 3600)
        refresh_token = data.get("refresh_token", "")

        # Store token with short code
        clean_store()
        short_code = secrets.token_urlsafe(8)
        token_store[short_code] = {
            "token":      token,
            "refresh":    refresh_token,
            "expires_in": expires_in,
            "stored_at":  time.time(),
        }

        # Show HTML page with the short code to copy
        from fastapi.responses import HTMLResponse
        html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
  <title>Spotify connecté !</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #0d0f14; color: #e0d8c8; font-family: Georgia, serif;
           display: flex; flex-direction: column; align-items: center;
           justify-content: center; min-height: 100vh; padding: 32px; text-align: center; }}
    .icon {{ font-size: 64px; margin-bottom: 20px; }}
    h1 {{ font-size: 22px; font-weight: 700; color: #f0e8d0; margin-bottom: 8px; }}
    .sub {{ color: #666; font-size: 14px; line-height: 1.6; margin-bottom: 32px; }}
    .code-box {{ background: #13161d; border: 2px solid #e8c97a; border-radius: 16px;
                padding: 24px 32px; margin-bottom: 24px; cursor: pointer; }}
    .code-label {{ font-size: 11px; letter-spacing: 0.2em; color: #666;
                  text-transform: uppercase; margin-bottom: 10px; }}
    .code {{ font-size: 36px; font-weight: 700; color: #e8c97a;
            letter-spacing: 0.15em; font-family: monospace; }}
    .copy-btn {{ background: #e8c97a; color: #000; border: none; border-radius: 12px;
               padding: 14px 28px; font-size: 16px; font-weight: 700;
               cursor: pointer; margin-bottom: 16px; width: 100%; font-family: inherit; }}
    .hint {{ color: #444; font-size: 13px; line-height: 1.5; }}
    .copied {{ color: #7ec87e; font-size: 14px; margin-top: 8px; display: none; }}
  </style>
</head>
<body>
  <div class="icon">✅</div>
  <h1>Spotify connecté !</h1>
  <p class="sub">Copie ce code et retourne<br/>dans l'appli Lyrics pour le coller.</p>

  <div class="code-box" onclick="copyCode()">
    <div class="code-label">Ton code</div>
    <div class="code" id="code">{short_code}</div>
  </div>

  <button class="copy-btn" onclick="copyCode()">📋 Copier le code</button>
  <div class="copied" id="copied">✅ Copié !</div>

  <p class="hint">Retourne dans l'appli Lyrics →<br/>Section Spotify → Entre le code</p>

  <script>
    function copyCode() {{
      navigator.clipboard?.writeText("{short_code}").then(() => {{
        document.getElementById("copied").style.display = "block";
        document.querySelector(".copy-btn").textContent = "✅ Copié !";
      }}).catch(() => {{
        // Fallback: select the text
        const el = document.getElementById("code");
        const range = document.createRange();
        range.selectNode(el);
        window.getSelection().removeAllRanges();
        window.getSelection().addRange(range);
      }});
    }}
    // Auto-copy on load
    setTimeout(() => copyCode(), 500);
  </script>
</body>
</html>"""
        return HTMLResponse(content=html)

    except Exception:
        return RedirectResponse(f"{APP_URL}?spotify_error=1")

@app.get("/token/{code}")
async def get_token(code: str):
    clean_store()
    entry = token_store.get(code)
    if not entry:
        raise HTTPException(404, "Code expiré ou invalide")
    # Delete after use
    del token_store[code]
    return {
        "access_token":  entry["token"],
        "refresh_token": entry["refresh"],
        "expires_in":    entry["expires_in"],
    }

# In-memory cache for bulk imports — avoids duplicate requests
_lyrics_cache = {}

@app.get("/lyrics")
async def lyrics_only(title: str, artist: str = ""):
    """Fast endpoint — only fetches lyrics, no YouTube. Used during bulk import."""
    cache_key = f"{title.lower().strip()}|{artist.lower().strip()}"
    if cache_key in _lyrics_cache:
        return _lyrics_cache[cache_key]
    async with httpx.AsyncClient() as client:
        try:
            lrc_params = {"track_name": title, "artist_name": artist}
            r = await client.get("https://lrclib.net/api/search", params=lrc_params, timeout=4)
            if r.status_code == 200 and r.json():
                results = r.json()
                best = next((x for x in results if x.get("syncedLyrics")), results[0])
                result = {
                    "title":  best.get("trackName"),
                    "artist": best.get("artistName"),
                    "synced": best.get("syncedLyrics"),
                    "plain":  best.get("plainLyrics"),
                }
                _lyrics_cache[cache_key] = result
                # Keep cache small
                if len(_lyrics_cache) > 500:
                    oldest = next(iter(_lyrics_cache))
                    del _lyrics_cache[oldest]
                return result
        except:
            pass
    raise HTTPException(404, "Rien trouvé")

@app.get("/search")
async def search_lyrics(title: str, artist: str = ""):
    import asyncio

    async with httpx.AsyncClient() as client:

        # Run LRCLIB and YouTube in PARALLEL — 2x faster
        async def fetch_lyrics():
            try:
                lrc_params = {"track_name": title, "artist_name": artist}
                r = await client.get("https://lrclib.net/api/search", params=lrc_params, timeout=8)
                if r.status_code == 200 and r.json():
                    results = r.json()
                    best = next((x for x in results if x.get("syncedLyrics")), results[0])
                    return {
                        "title":  best.get("trackName"),
                        "artist": best.get("artistName"),
                        "synced": best.get("syncedLyrics"),
                        "plain":  best.get("plainLyrics"),
                    }
            except:
                pass
            return {}

        async def fetch_youtube():
            if not YOUTUBE_API_KEY:
                return {}
            try:
                # Priority 1: YouTube Music Topic channel (auto-generated, clean audio)
                # Priority 2: Official audio/video
                # Priority 3: Lyrics video
                queries = [
                    f"{title} {artist} topic",           # YouTube Music auto-generated
                    f"{title} {artist} official audio",   # Official audio
                    f"{title} {artist} official video",   # Official video
                ]
                for query in queries:
                    yt_params = {
                        "part": "snippet",
                        "q": query,
                        "type": "video",
                        "videoCategoryId": "10",  # Music category
                        "maxResults": 5,
                        "key": YOUTUBE_API_KEY,
                    }
                    r = await client.get("https://www.googleapis.com/youtube/v3/search", params=yt_params, timeout=8)
                    if r.status_code != 200:
                        continue
                    items = r.json().get("items", [])
                    if not items:
                        continue
                    # Score each result
                    def score(item):
                        t = item["snippet"]["title"].lower()
                        ch = item["snippet"]["channelTitle"].lower()
                        s = 0
                        if "- topic" in ch: s += 10        # YouTube Music auto-generated
                        if "official audio" in t: s += 8
                        if "official video" in t: s += 6
                        if "lyrics" in t: s += 4
                        if "official" in t: s += 3
                        if "music video" in t: s += 2
                        if "cover" in t: s -= 5            # Penalize covers
                        if "karaoke" in t: s -= 8          # Penalize karaoke
                        if "remix" in t and "official" not in t: s -= 3
                        return s
                    best_yt = max(items, key=score)
                    if best_yt:
                        return {
                            "videoId":    best_yt["id"]["videoId"],
                            "videoTitle": best_yt["snippet"]["title"],
                            "thumbnail":  best_yt["snippet"]["thumbnails"]["medium"]["url"],
                        }
            except:
                pass
            return {}

        lyrics_data, youtube_data = await asyncio.gather(fetch_lyrics(), fetch_youtube())

    if not lyrics_data and not youtube_data:
        raise HTTPException(404, "Rien trouvé")

    return {**lyrics_data, **youtube_data}

@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    audio  = await file.read()
    suffix = "." + (file.filename.split(".")[-1] if file.filename else "mp3")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio)
        tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as f:
            result = groq_client.audio.transcriptions.create(
                file=(file.filename or "audio.mp3", f),
                model="whisper-large-v3",
                response_format="verbose_json",
                timestamp_granularities=["segment"],
            )
        segments = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in result.segments]
        return {"segments": segments}
    finally:
        os.unlink(tmp_path)
