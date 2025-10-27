"""Centralized logging configuration for the app.

Call `configure_logging()` early in process startup (e.g. in `app.main`).
"""
import logging
from typing import Optional


def configure_logging(level: int = logging.INFO, fmt: Optional[str] = None) -> None:
    """Configure the root logger for the application.

    Keeps configuration in one place so libraries can just use logging.getLogger(__name__).
    """
    if fmt is None:
        fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"

    logging.basicConfig(level=level, format=fmt)

    # Optionally adjust boto3/urllib3 verbosity to reduce noisy logs at INFO level
    logging.getLogger('boto3').setLevel(logging.WARNING)
    logging.getLogger('botocore').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
