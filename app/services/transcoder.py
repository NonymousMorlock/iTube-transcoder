import logging
import os
import subprocess
from pathlib import Path

import boto3
from botocore.client import BaseClient

from app.core.config import settings

logger = logging.getLogger(__name__)


class TranscodingError(Exception):
    """Raised when ffmpeg returns a non-zero exit code or transcoding fails.

    Carries optional process output to aid debugging.
    """

    def __init__(self, message: str, returncode: int | None = None, stdout: str | None = None,
                 stderr: str | None = None):
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __str__(self) -> str:
        base = super().__str__()
        if self.returncode is None:
            return base
        parts = [f"{base} (returncode={self.returncode})"]
        if self.stdout:
            parts.append(f"stdout={self.stdout}")
        if self.stderr:
            parts.append(f"stderr={self.stderr}")
        return "\n".join(parts)


class VideoTranscoder:
    def __init__(self):
        logger.debug("Initializing VideoTranscoder with S3 bucket: %s", settings.S3_BUCKET)
        self.s3_client: BaseClient = boto3.client(
            's3',
            region_name=settings.REGION_NAME,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        )

    @staticmethod
    def _get_content_type(file_path: str) -> str | None:
        if file_path.endswith('.m3u8'):
            return 'application/vnd.apple.mpegurl'
        elif file_path.endswith('.ts'):
            return 'video/MP2T'
        return None

    def download_video(self, local_path: Path):
        logger.info("Downloading s3://%s/%s to %s", settings.S3_BUCKET, settings.S3_KEY, local_path)
        # boto3 expects a filename string for download_file
        self.s3_client.download_file(settings.S3_BUCKET, settings.S3_KEY, str(local_path))
        logger.info("Download complete: %s", local_path)

    def transcode_video(self, input_path: str, output_dir: str):
        logger.info("Starting transcoding: %s -> %s", input_path, output_dir)
        # subprocess.run(self._command_builder(input_path, output_dir, is_hls=False), check=True)
        process = subprocess.run(
            self._command_builder(input_path, output_dir),
            capture_output=True,
            text=True,
        )

        if process.returncode != 0:
            logger.error(
                "FFmpeg process failed (returncode=%s). stdout=%s stderr=%s",
                process.returncode,
                (process.stdout or ""),
                (process.stderr or ""),
            )
            # Raise a domain-specific exception so callers can distinguish and inspect outputs
            raise TranscodingError(
                f"FFmpeg failed with return code {process.returncode}",
                returncode=process.returncode,
                stdout=(process.stdout or ""),
                stderr=(process.stderr or ""),
            )

        logger.info("Transcoding finished successfully")

    def upload_files(self, prefix: str, local_directory: str):
        logger.info("Uploading files from %s to bucket %s with prefix %s", local_directory,
                    settings.S3_PROCESSED_VIDEOS_BUCKET, prefix)
        for root, _, files in os.walk(local_directory):
            for file in files:
                local_path = os.path.join(root, file)
                s3_key = f'{prefix}/{os.path.relpath(local_path, local_directory)}'
                logger.debug("Uploading %s -> s3://%s/%s", local_path, settings.S3_PROCESSED_VIDEOS_BUCKET, s3_key)
                self.s3_client.upload_file(
                    local_path,
                    settings.S3_PROCESSED_VIDEOS_BUCKET,
                    s3_key,
                    ExtraArgs={'ACL': 'public-read', 'ContentType': self._get_content_type(local_path)}
                )
        logger.info("Upload complete")

    def process_video(self):
        logger.info("Beginning video processing workflow")
        work_dir = Path('/tmp/workspace')
        work_dir.mkdir(exist_ok=True)

        input_path = work_dir / 'input.mp4'
        output_path = work_dir / 'output'

        output_path.mkdir(exist_ok=True)
        try:
            self.download_video(local_path=input_path)
            self.transcode_video(input_path=str(input_path), output_dir=str(output_path))
            self.upload_files(prefix=settings.S3_KEY, local_directory=str(output_path))
        except Exception as e:
            # Log a concise error (no traceback) and re-raise so the caller (main) decides termination behavior
            logger.error("Error during video processing: %s", e)
            raise
        finally:
            if input_path.exists():
                input_path.unlink()
            if output_path.exists():
                import shutil

                shutil.rmtree(output_path)

    @staticmethod
    def _command_builder(input_path: str, output_dir: str, is_hls: bool = False):
        # Fargate doesn't have GPU support yet, so we use CPU-based transcoding
        # If you have GPU support, consider using h264_nvenc for faster encoding and adding '-hwaccel cuda' flag
        if is_hls:
            return [
                "ffmpeg",
                "-i",
                input_path,
                "-filter_complex",
                "[0:v]split=3[v1][v2][v3];"
                "[v1]scale=640:360:flags=fast_bilinear[360p];"
                "[v2]scale=1280:720:flags=fast_bilinear[720p];"
                "[v3]scale=1920:1080:flags=fast_bilinear[1080p]",
                "-map",
                "[360p]",
                "-map",
                "[720p]",
                "-map",
                "[1080p]",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-profile:v",
                "high",
                "-level:v",
                "4.1",
                "-g",
                "48",
                "-keyint_min",
                "48",
                "-sc_threshold",
                "0",
                "-b:v:0",
                "1000k",
                "-b:v:1",
                "4000k",
                "-b:v:2",
                "8000k",
                "-f",
                "hls",
                "-hls_time",
                "6",
                "-hls_playlist_type",
                "vod",
                "-hls_flags",
                "independent_segments",
                "-hls_segment_type",
                "mpegts",
                "-hls_list_size",
                "0",
                "-master_pl_name",
                "master.m3u8",
                "-var_stream_map",
                "v:0 v:1 v:2",
                "-hls_segment_filename",
                f"{output_dir}/%v/segment_%03d.ts",
                f"{output_dir}/%v/playlist.m3u8",
            ]

        return [
            "ffmpeg",
            "-i",
            input_path,
            "-filter_complex",
            "[0:v]split=3[v1][v2][v3];"
            "[v1]scale=640:360:flags=fast_bilinear[360p];"
            "[v2]scale=1280:720:flags=fast_bilinear[720p];"
            "[v3]scale=1920:1080:flags=fast_bilinear[1080p]",
            # 360p video stream
            "-map",
            "[360p]",
            "-c:v:0",
            "libx264",
            "-b:v:0",
            "1000k",
            "-preset",
            "veryfast",
            "-profile:v",
            "high",
            "-level:v",
            "4.1",
            "-g",
            "48",
            "-keyint_min",
            "48",
            # 720p video stream
            "-map",
            "[720p]",
            "-c:v:1",
            "libx264",
            "-b:v:1",
            "4000k",
            "-preset",
            "veryfast",
            "-profile:v",
            "high",
            "-level:v",
            "4.1",
            "-g",
            "48",
            "-keyint_min",
            "48",
            # 1080p video stream
            "-map",
            "[1080p]",
            "-c:v:2",
            "libx264",
            "-b:v:2",
            "8000k",
            "-preset",
            "veryfast",
            "-profile:v",
            "high",
            "-level:v",
            "4.1",
            "-g",
            "48",
            "-keyint_min",
            "48",
            # Audio stream
            "-map",
            "0:a",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            # DASH specific settings
            "-use_timeline",
            "1",
            "-use_template",
            "1",
            "-window_size",
            "5",
            "-adaptation_sets",
            "id=0,streams=v id=1,streams=a",
            "-f",
            "dash",
            f"{output_dir}/manifest.mpd",
        ]
