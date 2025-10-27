import logging
import shutil
import sys

from app.core.config import settings
from app.core.logging_config import configure_logging
from app.services.transcoder import VideoTranscoder, TranscodingError

logger = logging.getLogger(__name__)


def _check_ffmpeg_available() -> None:
    """Warn if ffmpeg is not available on PATH. Transcoding requires ffmpeg to be installed."""
    if shutil.which("ffmpeg") is None:
        logger.warning(
            "ffmpeg was not found on PATH. Transcoding will fail unless ffmpeg is installed in the image or host.")


def main() -> None:
    # Centralized logging configuration
    configure_logging()
    logger.info("iTube-transcoder starting")

    # To avoid logging secrets, I'll just log which key pieces are present
    try:
        logger.debug("Settings: REGION=%s, S3_BUCKET=%s", settings.REGION_NAME, getattr(settings, 'S3_BUCKET', None))
    except Exception as e:
        logger.debug("Settings are not fully available yet (missing environment variables)")
        logger.debug("Settings error: %s", e)

    _check_ffmpeg_available()

    try:
        transcoder = VideoTranscoder()
    except Exception as e:
        logger.exception("Failed to initialize VideoTranscoder (likely missing/invalid settings or AWS client error)")
        logger.debug("Initialization error: %s", e)
        sys.exit(2)

    try:
        transcoder.process_video()
        logger.info("Video processing completed successfully")
    except TranscodingError as e:
        # Transcoding errors are expected to be due to ffmpeg or input issues
        # I'm loggging key fields for easier diagnostics but avoid printing enormous outputs
        stderr_snip = (e.stderr or "")[:2000]
        stdout_snip = (e.stdout or "")[:1000]
        logger.error("Transcoding failed (returncode=%s). stderr_snip=%s", e.returncode, stderr_snip)
        if stdout_snip:
            logger.debug("Transcoding stdout (truncated): %s", stdout_snip)
        sys.exit(3)
    except Exception as e:
        # Unexpected errors (S3, filesystem, etc.)
        logger.exception("Video processing failed due to an unexpected error")
        logger.debug("Unexpected error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
