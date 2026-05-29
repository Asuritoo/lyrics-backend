from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx
import os
import tempfile
from groq import Groq

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")

@app.get("/")
def root():
    return {"status": "ok"}

@app.get("/search")
async def search_lyrics(title: str, artist: str = ""):
    async with httpx.AsyncClient() as client:
        # Paroles LRCLIB
        lrc_params = {"track_name": title, "artist_name": artist}
        lrc_r = await client.get("https://lrclib.net/api/search", params=lrc_params, timeout=10)
        lyrics_data = {}
        if lrc_r.status_code == 200 and lrc_r.json():
            results = lrc_r.json()
            best = next((x for x in results if x.get("syncedLyrics")), results[0])
            lyrics_data = {
                "title": best.get("trackName"),
                "artist": best.get("artistName"),
                "synced": best.get("syncedLyrics"),
                "plain": best.get("plainLyrics"),
            }

        # Recherche YouTube
        youtube_data = {}
        if YOUTUBE_API_KEY:
            query = f"{title} {artist} official audio"
            yt_params = {
                "part": "snippet",
                "q": query,
                "type": "video",
                "maxResults": 5,
                "key": YOUTUBE_API_KEY,
            }
            yt_r = await client.get("https://www.googleapis.com/youtube/v3/search", params=yt_params, timeout=10)
            if yt_r.status_code == 200:
                items = yt_r.json().get("items", [])
                # Préférer "official audio" ou "lyrics" dans le titre
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
                        "videoId": best_yt["id"]["videoId"],
                        "videoTitle": best_yt["snippet"]["title"],
                        "thumbnail": best_yt["snippet"]["thumbnails"]["medium"]["url"],
                    }

    if not lyrics_data and not youtube_data:
        raise HTTPException(404, "Rien trouvé")

    return { **lyrics_data, **youtube_data }

@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    audio = await file.read()
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
        segments = [
            {"start": s.start, "end": s.end, "text": s.text.strip()}
            for s in result.segments
        ]
        return {"segments": segments}
    finally:
        os.unlink(tmp_path)
