import logging
import os
import random
import string
import subprocess

import config

logger = logging.getLogger(__name__)


def _probe_has_audio(path: str) -> bool:
    result = subprocess.run(
        [config.FFMPEG_BIN, "-i", path, "-hide_banner"],
        capture_output=True, text=True, timeout=30,
    )
    return "Audio:" in result.stderr


def _random_name(ext: str = "mp4") -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=12)) + f".{ext}"


def process_video(input_path: str) -> tuple:
    """
    Re-encode at CRF 18 (near-lossless) with a 1.01x speed nudge that
    breaks audio/video fingerprints without any perceptible quality change.
    Returns (output_path, file_size_bytes).
    """
    output_path = os.path.join(config.PROCESSED_DIR, _random_name())
    has_audio   = _probe_has_audio(input_path)

    vf = "setpts=PTS/1.01"

    if has_audio:
        cmd = [
            config.FFMPEG_BIN, "-y",
            "-i", input_path,
            "-vf", vf,
            "-af", "atempo=1.01",
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "aac", "-b:a", "128k",
            "-map_metadata", "-1",
            "-movflags", "+faststart",
            output_path,
        ]
    else:
        cmd = [
            config.FFMPEG_BIN, "-y",
            "-i", input_path,
            "-vf", vf,
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-an",
            "-map_metadata", "-1",
            "-movflags", "+faststart",
            output_path,
        ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
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
