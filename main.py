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

        # Redirect to PWA with just the short code
        return RedirectResponse(f"{APP_URL}?sp={short_code}")

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

@app.get("/search")
async def search_lyrics(title: str, artist: str = ""):
    async with httpx.AsyncClient() as client:
        lrc_params  = {"track_name": title, "artist_name": artist}
        lrc_r       = await client.get("https://lrclib.net/api/search", params=lrc_params, timeout=10)
        lyrics_data = {}
        if lrc_r.status_code == 200 and lrc_r.json():
            results = lrc_r.json()
            best    = next((x for x in results if x.get("syncedLyrics")), results[0])
            lyrics_data = {
                "title":  best.get("trackName"),
                "artist": best.get("artistName"),
                "synced": best.get("syncedLyrics"),
                "plain":  best.get("plainLyrics"),
            }

        youtube_data = {}
        if YOUTUBE_API_KEY:
            query     = f"{title} {artist} official audio"
            yt_params = {"part": "snippet", "q": query, "type": "video", "maxResults": 5, "key": YOUTUBE_API_KEY}
            yt_r      = await client.get("https://www.googleapis.com/youtube/v3/search", params=yt_params, timeout=10)
            if yt_r.status_code == 200:
                items   = yt_r.json().get("items", [])
                best_yt = None
                for item in items:
                    t = item["snippet"]["title"].lower()
                    if any(k in t for k in ["official audio", "lyrics", "official video"]):
                        best_yt = item
                        break
                if not best_yt and items:
                    best_yt = items[0]
                if best_yt:
                    youtube_data = {
                        "videoId":    best_yt["id"]["videoId"],
                        "videoTitle": best_yt["snippet"]["title"],
                        "thumbnail":  best_yt["snippet"]["thumbnails"]["medium"]["url"],
                    }

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
