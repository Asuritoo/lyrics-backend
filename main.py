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

@app.get("/")
def root():
    return {"status": "ok"}

@app.get("/search")
async def search_lyrics(title: str, artist: str = ""):
    url = "https://lrclib.net/api/search"
    params = {"track_name": title, "artist_name": artist}
    async with httpx.AsyncClient() as client:
        r = await client.get(url, params=params, timeout=10)
    if r.status_code != 200:
        raise HTTPException(502, "LRCLIB error")
    results = r.json()
    if not results:
        raise HTTPException(404, "No lyrics found")
    best = next((x for x in results if x.get("syncedLyrics")), results[0])
    return {
        "title": best.get("trackName"),
        "artist": best.get("artistName"),
        "synced": best.get("syncedLyrics"),
        "plain": best.get("plainLyrics"),
    }

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
