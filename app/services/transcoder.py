import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

import boto3
import requests
from botocore.client import BaseClient
from requests_aws4auth import AWS4Auth

from app.core.config import settings

logger = logging.getLogger(__name__)


class TranscodingError(Exception):
    """Raised when ffmpeg returns a non-zero exit code or transcoding fails.

    Carries optional process output to aid debugging.
    """

    def __init__(
            self,
            message: str,
            returncode: int | None = None,
            stdout: str | None = None,
            stderr: str | None = None
    ):
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
        elif file_path.endswith('.mpd'):
            return 'application/dash+xml'
        elif file_path.endswith('m4s'):
            return 'video/mp4'
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
            self.update_video_processing_status(video_s3_key=settings.S3_KEY, status='COMPLETED')
        except Exception as e:
            # Log the error and attempt to update status to FAILED
            logger.error("Error during video processing: %s", e)
            try:
                self.update_video_processing_status(video_s3_key=settings.S3_KEY, status='FAILED')
                logger.info("Successfully marked video as FAILED")
            except Exception as status_error:
                logger.error("Failed to update video status to FAILED: %s", status_error)
            # Re-raise the original exception
            raise
        finally:
            if input_path.exists():
                input_path.unlink()
            if output_path.exists():
                import shutil

                shutil.rmtree(output_path)

    @staticmethod
    def _get_aws4_auth() -> AWS4Auth:
        """Get AWS4Auth object using ECS task role credentials.

        This uses boto3's default credential chain which will automatically
        use the ECS task role when running in ECS.
        """
        # Get credentials from boto3 session (uses ECS task role)
        session = boto3.Session()
        credentials = session.get_credentials()

        # Create AWS4Auth for signing requests
        return AWS4Auth(
            credentials.access_key,
            credentials.secret_key,
            settings.REGION_NAME,
            'execute-api',  # Service name for API Gateway, or use generic service name
            session_token=credentials.token,
        )

    @staticmethod
    def _lookup_video_id(video_s3_key: str, auth: AWS4Auth) -> Optional[str]:
        """Lookup video ID by S3 key using IAM-authenticated request.

        Args:
            video_s3_key: The S3 key of the video
            auth: AWS4Auth object for signing the request

        Returns:
            The video ID if found, None otherwise

        Raises:
            requests.RequestException: If the lookup request fails
        """
        logger.info("Looking up video ID for S3 key: %s", video_s3_key)

        try:
            response = requests.get(
                url=f"{settings.BACKEND_URL}videos/by-key/{video_s3_key}",
                auth=auth,
            )
            response.raise_for_status()
            data = response.json()
            video_id = data.get('video_id')
            logger.info("Found video ID %s for S3 key %s", video_id, video_s3_key)
            return video_id
        except requests.RequestException as e:
            logger.error("Failed to lookup video ID for S3 key %s: %s", video_s3_key, e)
            raise

    @staticmethod
    def update_video_processing_status(video_s3_key: str, status: str) -> None:
        """Update video processing status using IAM-authenticated requests.

        This method:
        1. Looks up the video ID by S3 key
        2. Updates the processing status with the video ID
        Both requests are signed with AWS SigV4 using the ECS task role.

        Args:
            video_s3_key: The S3 key of the video
            status: The new processing status (COMPLETED or FAILED)

        Raises:
            requests.RequestException: If any request fails
        """
        logger.info("Updating video with S3 key %s to status %s", video_s3_key, status)

        try:
            # Get AWS credentials and create auth object
            auth = VideoTranscoder._get_aws4_auth()

            # First, lookup the video ID
            video_id = VideoTranscoder._lookup_video_id(video_s3_key, auth)

            if not video_id:
                raise ValueError(f"Video not found for S3 key: {video_s3_key}")

            # Now update the status using the video ID
            logger.info("Updating video ID %s to status %s", video_id, status)
            response = requests.patch(
                url=f"{settings.BACKEND_URL}/videos/{video_id}/status",
                params={"status": status},
                auth=auth,
            )
            response.raise_for_status()
            logger.info("Successfully updated video status for ID %s", video_id)
        except requests.RequestException as e:
            logger.error("Failed to update video status for S3 key %s: %s", video_s3_key, e)
            raise
        except ValueError as e:
            logger.error("Video lookup failed: %s", e)
            raise

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
