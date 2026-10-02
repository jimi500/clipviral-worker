# ClipViral Free Worker

Free prototype worker for ClipViral.

Pipeline:
YouTube/Twitch -> yt-dlp -> Whisper -> highlight scoring -> FFmpeg -> AppDeploy storage.

The GitHub Actions workflow runs every 5 minutes and can also be started manually from the Actions tab.

This is a prototype configuration: one 20-second 720x1280 clip per job. It is not intended for unlimited production traffic.
