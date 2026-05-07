"""
FFmpeg processing pipeline (runs in exact order specified):
 1. Strip all metadata
 2. Add one transparent (black) frame at start
 3. Crop 1% from every edge  (keeps exact aspect ratio)
 4. Re-encode with slightly randomised bitrate
 5. Shift audio pitch very slightly (0.2% — imperceptible)
 6. Output with randomised filename
"""

import logging
import os
import random
import re
import string
import subprocess

import config

logger = logging.getLogger(__name__)


# ── probe via ffmpeg -i (no ffprobe needed) ───────────────────────────────────

def _probe(path: str) -> dict:
    """
    Run `ffmpeg -i <path>` and parse the stderr output for stream info.
    ffmpeg always exits non-zero when no output file is given — that's expected.
    """
    cmd = [config.FFMPEG_BIN, "-i", path, "-hide_banner"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    output = result.stderr  # ffmpeg writes info to stderr

    info = {
        "width": 0,
        "height": 0,
        "r_frame_rate": "30/1",
        "has_audio": False,
        "bit_rate": 0,
        "sample_rate": 44100,
    }

    # Video stream line:
    # Stream #0:0: Video: h264, yuv420p, 1080x1920, 1965 kb/s, 30 fps, ...
    vm = re.search(r"Stream.*?Video:.*?(\d+)x(\d+).*?(\d+(?:\.\d+)?)\s+fps", output)
    if vm:
        info["width"] = int(vm.group(1))
        info["height"] = int(vm.group(2))
        fps = float(vm.group(3))
        info["r_frame_rate"] = f"{int(fps * 1000)}/1000"

    # Fallback: WxH without fps on same match
    if info["width"] == 0:
        dm = re.search(r"(\d{2,5})x(\d{2,5})", output)
        if dm:
            info["width"] = int(dm.group(1))
            info["height"] = int(dm.group(2))

    # Video bitrate: "1965 kb/s" on the Video stream line
    vb = re.search(r"Video:.*?(\d+)\s+kb/s", output)
    if vb:
        info["bit_rate"] = int(vb.group(1)) * 1000

    # Container bitrate fallback: "bitrate: 2048 kb/s"
    if info["bit_rate"] == 0:
        cb = re.search(r"bitrate:\s*(\d+)\s+kb/s", output)
        if cb:
            info["bit_rate"] = int(cb.group(1)) * 1000

    # Audio stream
    if re.search(r"Stream.*Audio:", output):
        info["has_audio"] = True
        sm = re.search(r"Audio:.*?(\d{4,6})\s+Hz", output)
        if sm:
            info["sample_rate"] = int(sm.group(1))

    if info["width"] == 0 or info["height"] == 0:
        raise RuntimeError(
            f"Could not detect video dimensions from ffmpeg output:\n{output[-600:]}"
        )

    return info


# ── helpers ───────────────────────────────────────────────────────────────────

def _random_name(ext: str = "mp4") -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choices(chars, k=12)) + f".{ext}"


# ── main pipeline ─────────────────────────────────────────────────────────────

def process_video(input_path: str) -> tuple:
    """
    Apply the full 6-step pipeline to *input_path*.
    Returns (output_path, file_size_bytes).
    """
    info = _probe(input_path)

    width = info["width"]
    height = info["height"]
    has_audio = info["has_audio"]

    # fps from "num/den" string or float
    fps_str = info["r_frame_rate"]
    try:
        num, den = fps_str.split("/")
        fps = float(num) / float(den)
    except Exception:
        fps = 30.0
    fps = max(1.0, min(fps, 120.0))
    frame_dur = 1.0 / fps

    # ── step 4: slightly randomised bitrate ───────────────────────────────────
    base_bps = info["bit_rate"] or 2_000_000
    factor = random.uniform(0.95, 1.05)
    new_bitrate_k = max(300, min(int(base_bps * factor / 1000), 10_000))

    # ── step 5: imperceptible pitch shift ─────────────────────────────────────
    sr = info["sample_rate"]
    shifted_sr = int(sr * config.PITCH_SHIFT_FACTOR)

    # ── output path (step 6: randomised filename) ─────────────────────────────
    output_path = os.path.join(config.PROCESSED_DIR, _random_name())

    # ── filter_complex ────────────────────────────────────────────────────────
    #  Input 0 = source video
    #  Input 1 = lavfi black frame (same size/fps, 1-frame duration)
    #
    #  Graph (in pipeline order):
    #   step 2: [1:v] trim to 1 frame → format → [blank]
    #           [blank][0:v] concat → [vcat]
    #   step 3: [vcat] crop 1% from each edge → [vout]
    #   step 5: [0:a] asetrate → aresample → [aout]

    if has_audio:
        fc = (
            f"[1:v]trim=end={frame_dur:.6f},setpts=PTS-STARTPTS,"
            f"format=yuv420p[blank];"
            f"[blank][0:v]concat=n=2:v=1:a=0[vcat];"
            f"[vcat]crop=iw*0.98:ih*0.98:iw*0.01:ih*0.01[vout];"
            f"[0:a]asetrate={shifted_sr},aresample={sr}[aout]"
        )
        map_args = ["-map", "[vout]", "-map", "[aout]"]
        audio_args = ["-c:a", "aac", "-b:a", "128k", "-ar", str(sr)]
    else:
        fc = (
            f"[1:v]trim=end={frame_dur:.6f},setpts=PTS-STARTPTS,"
            f"format=yuv420p[blank];"
            f"[blank][0:v]concat=n=2:v=1:a=0[vcat];"
            f"[vcat]crop=iw*0.98:ih*0.98:iw*0.01:ih*0.01[vout]"
        )
        map_args = ["-map", "[vout]"]
        audio_args = ["-an"]

    cmd = [
        config.FFMPEG_BIN, "-y",
        "-i", input_path,
        "-f", "lavfi",
        "-i", f"color=black:size={width}x{height}:rate={fps:.4f}",
        "-filter_complex", fc,
        *map_args,
        "-c:v", "libx264",
        "-b:v", f"{new_bitrate_k}k",
        "-preset", "fast",
        "-profile:v", "high",
        *audio_args,
        "-map_metadata", "-1",      # step 1: strip ALL metadata
        "-movflags", "+faststart",
        output_path,
    ]

    logger.info(
        f"FFmpeg | {width}x{height} {fps:.1f}fps | "
        f"bitrate={new_bitrate_k}k | pitch_sr={shifted_sr} | "
        f"audio={has_audio}"
    )

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(
            f"FFmpeg failed (code {result.returncode}):\n{result.stderr[-800:]}"
        )

    if not os.path.exists(output_path):
        raise RuntimeError("FFmpeg produced no output file")

    file_size = os.path.getsize(output_path)
    size_mb = file_size / (1024 * 1024)

    if size_mb > config.MAX_FILE_SIZE_MB:
        os.remove(output_path)
        raise RuntimeError(
            f"Processed file too large ({size_mb:.1f} MB > {config.MAX_FILE_SIZE_MB} MB limit)"
        )

    logger.info(f"Processed → {os.path.basename(output_path)} ({size_mb:.1f} MB)")
    return output_path, file_size
