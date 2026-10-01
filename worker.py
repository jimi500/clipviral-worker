import os
import subprocess
import tempfile
import time
from pathlib import Path

import requests
from faster_whisper import WhisperModel

API_BASE_URL = os.environ["CLIPVIRAL_API_URL"].rstrip("/")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "5"))
MODEL_NAME = os.getenv("WHISPER_MODEL", "small")
MAX_CLIPS = int(os.getenv("MAX_CLIPS", "3"))

SESSION = requests.Session()
MODEL = None


def callback(job_id, payload):
    response = SESSION.post(
        f"{API_BASE_URL}/api/jobs/{job_id}/callback",
        json=payload,
        timeout=60,
    )
    response.raise_for_status()


def run(command):
    return subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def download_source(source, workdir):
    if not source.startswith(("http://", "https://")):
        raise RuntimeError(
            "Direct uploaded-file processing is not enabled yet. "
            "Use a YouTube or Twitch URL."
        )

    output = str(Path(workdir) / "source.%(ext)s")

    run([
        "yt-dlp",
        "--no-playlist",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", output,
        source,
    ])

    files = list(Path(workdir).glob("source.*"))
    if not files:
        raise RuntimeError("yt-dlp completed but no media file was produced.")

    return str(files[0])


def transcribe(media):
    global MODEL

    if MODEL is None:
        MODEL = WhisperModel(
            MODEL_NAME,
            device="cpu",
            compute_type="int8",
        )

    segments, _ = MODEL.transcribe(
        media,
        vad_filter=True,
        word_timestamps=True,
    )

    return list(segments)


def score_segments(segments):
    hook_words = {
        "secret",
        "mistake",
        "why",
        "how",
        "truth",
        "never",
        "always",
        "best",
        "worst",
        "problem",
        "learn",
        "important",
        "shocking",
        "actually",
        "because",
        "instead",
        "stop",
        "start",
    }

    candidates = []

    for index, segment in enumerate(segments):
        text = segment.text.strip()

        if not text:
            continue

        words = text.split()
        lower = text.lower()

        score = 45
        score += min(25, sum(5 for word in hook_words if word in lower))
        score += min(20, max(0, len(words) - 8))

        if "?" in text:
            score += 8

        candidates.append(
            (
                min(99, score),
                index,
                segment,
            )
        )

    candidates.sort(
        reverse=True,
        key=lambda item: item[0],
    )

    return candidates[:MAX_CLIPS]


def render_clip(media, start, end, output):
    duration = max(8.0, min(60.0, end - start))

    video_filter = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        "setsar=1"
    )

    run([
        "ffmpeg",
        "-y",
        "-ss",
        str(start),
        "-i",
        media,
        "-t",
        str(duration),
        "-vf",
        video_filter,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        output,
    ])


def process(job):
    job_id = job["jobId"]
    source = job["source"]

    with tempfile.TemporaryDirectory(
        prefix=f"clipviral-{job_id}-"
    ) as workdir:

        callback(
            job_id,
            {
                "status": "processing",
                "progress": 15,
                "step": 1,
                "message": "Downloading source video...",
            },
        )

        media = download_source(source, workdir)

        callback(
            job_id,
            {
                "status": "processing",
                "progress": 35,
                "step": 2,
                "message": "Transcribing with Whisper...",
            },
        )

        segments = transcribe(media)

        if not segments:
            raise RuntimeError(
                "No speech was detected in the source video."
            )

        callback(
            job_id,
            {
                "status": "processing",
                "progress": 55,
                "step": 2,
                "message": "Finding high-retention moments...",
            },
        )

        picks = score_segments(segments)

        clips = []

        for clip_number, (score, _, segment) in enumerate(
            picks,
            start=1,
        ):
            start = max(
                0.0,
                float(segment.start) - 3.0,
            )

            end = min(
                float(segments[-1].end),
                start + 45.0,
            )

            output = str(
                Path(workdir) / f"clip-{clip_number}.mp4"
            )

            callback(
                job_id,
                {
                    "status": "processing",
                    "progress": 60 + clip_number * 10,
                    "step": 3,
                    "message": (
                        f"Rendering clip {clip_number} "
                        f"of {len(picks)}..."
                    ),
                },
            )

            render_clip(
                media,
                start,
                end,
                output,
            )

            clips.append(
                {
                    "id": clip_number,
                    "title": segment.text.strip()[:90],
                    "viralScore": int(score),
                    "duration": (
                        f"{int(end - start) // 60:02d}:"
                        f"{int(end - start) % 60:02d}"
                    ),
                    "reason": (
                        "Transcript hook and "
                        "conversational density detected."
                    ),
                    "subtitle": (
                        segment.text.strip()[:120].upper()
                    ),
                    "tone": "Hook",
                    "status": "rendered",
                    "localOutput": output,
                }
            )

        # IMPORTANT:
        # Render's local filesystem is temporary. We deliberately do not
        # expose localOutput as a public download URL.
        # Persistent S3/R2-compatible storage must be configured next.
        raise RuntimeError(
            "MP4 rendering succeeded, but persistent storage is not "
            "configured. Configure S3-compatible storage before publishing "
            "download URLs."
        )


def main():
    while True:
        job_data = None

        try:
            response = SESSION.post(
                f"{API_BASE_URL}/api/worker/next",
                timeout=30,
            )

            if response.status_code == 204:
                time.sleep(POLL_SECONDS)
                continue

            response.raise_for_status()
            job_data = response.json()

            if not job_data.get("claimed"):
                time.sleep(POLL_SECONDS)
                continue

            process(job_data)

        except Exception as exc:
            print(
                f"worker error: {exc}",
                flush=True,
            )

            job_id = (
                job_data.get("jobId")
                if job_data
                else None
            )

            if job_id:
                try:
                    callback(
                        job_id,
                        {
                            "status": "failed",
                            "progress": 100,
                            "step": 4,
                            "message": "Media processing failed.",
                            "error": str(exc),
                        },
                    )
                except Exception as callback_error:
                    print(
                        f"callback error: {callback_error}",
                        flush=True,
                    )

            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
