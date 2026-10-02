import argparse
import base64
import os
import subprocess
import tempfile
import time
from pathlib import Path

import requests
from faster_whisper import WhisperModel

API_BASE_URL = os.environ["CLIPVIRAL_API_URL"].rstrip("/")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "5"))
MODEL_NAME = os.getenv("WHISPER_MODEL", "base")
MAX_CLIPS = int(os.getenv("MAX_CLIPS", "1"))
CLIP_SECONDS = int(os.getenv("CLIP_SECONDS", "20"))
OUTPUT_WIDTH = int(os.getenv("OUTPUT_WIDTH", "720"))
OUTPUT_HEIGHT = int(os.getenv("OUTPUT_HEIGHT", "1280"))

SESSION = requests.Session()
MODEL = None


def callback(job_id, payload):
    r = SESSION.post(f"{API_BASE_URL}/api/jobs/{job_id}/callback",
                     json=payload, timeout=60)
    r.raise_for_status()


def upload_clip(job_id, clip_id, output):
    encoded = base64.b64encode(Path(output).read_bytes()).decode("ascii")
    r = SESSION.post(
        f"{API_BASE_URL}/api/jobs/{job_id}/upload-clip",
        json={
            "clipId": clip_id,
            "fileName": f"clip-{clip_id}.mp4",
            "contentBase64": encoded,
            "contentType": "video/mp4",
        },
        timeout=180,
    )
    r.raise_for_status()
    return r.json()["downloadUrl"]


def run(command):
    return subprocess.run(
        command, check=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True
    )


def download_source(source, workdir):
    if not source.startswith(("http://", "https://")):
        raise RuntimeError("Use a YouTube or Twitch URL for the free prototype.")
    output = str(Path(workdir) / "source.%(ext)s")
    run([
        "yt-dlp", "--no-playlist", "-f", "bv*+ba/b",
        "--merge-output-format", "mp4", "-o", output, source
    ])
    files = list(Path(workdir).glob("source.*"))
    if not files:
        raise RuntimeError("No media file was produced.")
    return str(files[0])


def transcribe(media):
    global MODEL
    if MODEL is None:
        MODEL = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
    segments, _ = MODEL.transcribe(media, vad_filter=True, word_timestamps=True)
    return list(segments)


def score_segments(segments):
    hooks = {
        "secret", "mistake", "why", "how", "truth", "never", "always",
        "best", "worst", "problem", "learn", "important", "shocking",
        "actually", "because", "instead", "stop", "start"
    }
    candidates = []
    for index, segment in enumerate(segments):
        text = segment.text.strip()
        if not text:
            continue
        words = text.split()
        lower = text.lower()
        score = 45
        score += min(25, sum(5 for w in hooks if w in lower))
        score += min(20, max(0, len(words) - 8))
        if "?" in text:
            score += 8
        candidates.append((min(99, score), index, segment))
    candidates.sort(reverse=True, key=lambda x: x[0])
    return candidates[:MAX_CLIPS]


def render_clip(media, start, end, output):
    duration = max(8.0, min(float(CLIP_SECONDS), end - start))
    vf = (
        f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:"
        "force_original_aspect_ratio=increase,"
        f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},setsar=1"
    )
    run([
        "ffmpeg", "-y", "-ss", str(start), "-i", media,
        "-t", str(duration), "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", output
    ])


def process(job):
    job_id = job["jobId"]
    with tempfile.TemporaryDirectory(prefix=f"clipviral-{job_id}-") as workdir:
        callback(job_id, {
            "status": "processing", "progress": 15, "step": 1,
            "message": "Downloading source video..."
        })
        media = download_source(job["source"], workdir)

        callback(job_id, {
            "status": "processing", "progress": 35, "step": 2,
            "message": f"Transcribing with Whisper ({MODEL_NAME})..."
        })
        segments = transcribe(media)
        if not segments:
            raise RuntimeError("No speech was detected.")

        callback(job_id, {
            "status": "processing", "progress": 55, "step": 2,
            "message": "Finding a high-retention moment..."
        })
        picks = score_segments(segments)
        if not picks:
            raise RuntimeError("No usable clip candidate was detected.")

        clips = []
        for number, (score, _, segment) in enumerate(picks, start=1):
            start = max(0.0, float(segment.start) - 3.0)
            end = min(float(segments[-1].end), start + CLIP_SECONDS)
            output = str(Path(workdir) / f"clip-{number}.mp4")

            callback(job_id, {
                "status": "processing", "progress": 60, "step": 3,
                "message": f"Rendering clip {number}..."
            })
            render_clip(media, start, end, output)

            callback(job_id, {
                "status": "processing", "progress": 85, "step": 4,
                "message": "Saving rendered clip..."
            })
            url = upload_clip(job_id, number, output)

            clips.append({
                "id": number,
                "title": segment.text.strip()[:90],
                "viralScore": int(score),
                "duration": f"{int(end-start)//60:02d}:{int(end-start)%60:02d}",
                "reason": "Transcript hook and conversational density detected.",
                "subtitle": segment.text.strip()[:120].upper(),
                "tone": "Hook",
                "status": "rendered",
                "downloadUrl": url,
            })

        callback(job_id, {
            "status": "completed", "progress": 100, "step": 4,
            "message": "Your clip is ready.", "clips": clips
        })


def claim_job():
    r = SESSION.post(f"{API_BASE_URL}/api/worker/next", timeout=30)
    r.raise_for_status()
    data = r.json()
    return data if data.get("claimed") else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    while True:
        job = None
        try:
            job = claim_job()
            if not job:
                if args.once:
                    return
                time.sleep(POLL_SECONDS)
                continue
            process(job)
            if args.once:
                return
        except Exception as exc:
            print(f"worker error: {exc}", flush=True)
            if job:
                try:
                    callback(job["jobId"], {
                        "status": "failed", "progress": 100, "step": 4,
                        "message": "Media processing failed.",
                        "error": str(exc)
                    })
                except Exception as callback_error:
                    print(f"callback error: {callback_error}", flush=True)
            if args.once:
                raise
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
