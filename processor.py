import logging
import os
import random
import string
import subprocess

import config

logger = logging.getLogger(__name__)


def _random_name(ext: str = "mp4") -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=12)) + f".{ext}"


def process_video(input_path: str) -> tuple:
    """
    Strip metadata from the video. Streams are copied without re-encoding
    so there is zero quality loss and no file size inflation.
    Returns (output_path, file_size_bytes).
    """
    output_path = os.path.join(config.PROCESSED_DIR, _random_name())

    cmd = [
        config.FFMPEG_BIN, "-y",
        "-i", input_path,
        "-c", "copy",
        "-map_metadata", "-1",
        "-movflags", "+faststart",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed:\n{result.stderr[-500:]}")

    if not os.path.exists(output_path):
        raise RuntimeError("FFmpeg produced no output file")

    file_size = os.path.getsize(output_path)
    size_mb   = file_size / (1024 * 1024)

    if size_mb > config.MAX_FILE_SIZE_MB:
        os.remove(output_path)
        raise RuntimeError(f"File too large ({size_mb:.1f} MB > {config.MAX_FILE_SIZE_MB} MB)")

    logger.info(f"Processed {size_mb:.1f} MB → {os.path.basename(output_path)}")
    return output_path, file_size
