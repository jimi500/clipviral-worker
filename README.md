# ClipViral Media Worker

This is the real media execution worker for ClipViral.

## Current pipeline

1. Polls the ClipViral API for queued jobs.
2. Downloads YouTube/Twitch videos with yt-dlp.
3. Transcribes speech using faster-whisper.
4. Finds candidate high-retention moments.
5. Renders real 9:16 MP4 clips with FFmpeg.
6. Reports progress back to ClipViral.

## Environment

Set:

`CLIPVIRAL_API_URL=https://clipforge-ai-dvdwp2.v2.appdeploy.ai`

Optional:

`WHISPER_MODEL=small`
`POLL_SECONDS=5`
`MAX_CLIPS=3`

## Important

The worker currently renders real MP4 files, but Render's local filesystem is ephemeral.

Before ClipViral can provide durable download URLs, configure persistent S3-compatible storage such as Cloudflare R2 or Amazon S3 and add the upload step.

The worker intentionally fails after rendering rather than pretending a temporary local file is a permanent public URL.
