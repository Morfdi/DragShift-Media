"""Pure audio/video processing via ffmpeg (no UI here, so it's testable).

We shell out to ffmpeg instead of linking a codec library: one binary covers every format,
it can be shipped next to the app, and a codec crash can't take DragShift down with it.
Every function takes a source path and returns the output path(s) next to it."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .util import UserError, output_dir, sibling, unique_path

VIDEO_TARGETS = ["MP4", "WEBM", "MOV", "MKV", "GIF", "MP3"]
AUDIO_TARGETS = ["MP3", "M4A", "WAV", "FLAC", "OGG", "OPUS"]

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW
_EXT = {"MP4": ".mp4", "WEBM": ".webm", "MOV": ".mov", "MKV": ".mkv", "GIF": ".gif",
        "MP3": ".mp3", "M4A": ".m4a", "WAV": ".wav", "FLAC": ".flac", "OGG": ".ogg",
        "OPUS": ".opus"}


# ---------------------------------------------------------------- locating ffmpeg
def _bundled(name: str) -> Path | None:
    """Look for ffmpeg next to the app: assets/ffmpeg/, bin/, or the app folder itself."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    exe = name + (".exe" if sys.platform == "win32" else "")
    for d in (base / "assets" / "ffmpeg", base / "bin", base):
        p = d / exe
        if p.is_file():
            return p
    return None


def _tool(name: str) -> str | None:
    p = _bundled(name)
    return str(p) if p else shutil.which(name)


def available() -> bool:
    return _tool("ffmpeg") is not None and _tool("ffprobe") is not None


def _ffmpeg() -> str:
    p = _tool("ffmpeg")
    if not p:
        raise UserError("ffmpeg isn't installed. Install it (winget install ffmpeg / "
                        "brew install ffmpeg / sudo apt install ffmpeg) or drop ffmpeg "
                        "next to DragShift (assets/ffmpeg/).")
    return p


def _ffprobe() -> str:
    p = _tool("ffprobe")
    if not p:
        raise UserError("ffprobe is missing — it ships with ffmpeg.")
    return p


# ---------------------------------------------------------------- probe
@dataclass
class MediaInfo:
    duration: float = 0.0
    size: tuple[int, int] | None = None
    vcodec: str | None = None
    acodec: str | None = None
    fps: float | None = None
    bitrate: int | None = None
    channels: int | None = None
    sample_rate: int | None = None

    @property
    def has_video(self) -> bool:
        return self.vcodec is not None

    @property
    def has_audio(self) -> bool:
        return self.acodec is not None


def duration_str(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def probe(src: str | Path) -> MediaInfo:
    try:
        r = subprocess.run(
            [_ffprobe(), "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(src)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, creationflags=_NO_WINDOW)
    except OSError as e:
        raise UserError(f"Couldn't run ffprobe: {e}")
    
    if r.returncode != 0:
        raise UserError(f"Couldn't read this file: {(r.stderr or '').strip()[:200]}")
    
    if not r.stdout.strip():
        raise UserError("ffprobe returned no data. Make sure you have a valid ffprobe.exe (from BtbN or gyan.dev, not ffmpeg.org).")
        
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise UserError(f"ffprobe output is not valid JSON: {r.stdout[:100]}")

    streams = data.get("streams", [])
    
    # Ищем видеопоток, игнорируя обложки
    v = next((s for s in streams 
              if s.get("codec_type") == "video"
              and str(s.get("disposition", {}).get("attached_pic", "0")).lower() not in ("1", "true")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})
    
    try:
        dur = float(fmt.get("duration") or (v or a or {}).get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
        
    fps = None
    if v and "/" in str(v.get("avg_frame_rate", "")):
        n, d = v["avg_frame_rate"].split("/")
        if float(d or 0):
            fps = float(n) / float(d)
            
    return MediaInfo(
        duration=dur,
        size=(int(v["width"]), int(v["height"])) if v and v.get("width") else None,
        vcodec=v.get("codec_name") if v else None,
        acodec=a.get("codec_name") if a else None,
        fps=fps,
        bitrate=int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
        channels=a.get("channels") if a else None,
        sample_rate=int(a["sample_rate"]) if a and a.get("sample_rate") else None,
    )


# ---------------------------------------------------------------- running ffmpeg
def _run(args: list[str], duration: float = 0.0, on_progress=None) -> None:
    """Run ffmpeg; feed `on_progress(fraction)` from its -progress stream."""
    cmd = [_ffmpeg(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats", *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1, creationflags=_NO_WINDOW)
    for line in proc.stdout:
        if on_progress and duration > 0 and line.startswith("out_time="):
            m = re.match(r"out_time=(\d+):(\d+):(\d+\.\d+)", line.strip())
            if m:
                h, mi, s = m.groups()
                t = int(h) * 3600 + int(mi) * 60 + float(s)
                try:
                    on_progress(min(1.0, t / duration))
                except Exception:
                    pass
    err = proc.stderr.read() or ""
    if proc.wait() != 0:
        tail = [l for l in err.strip().splitlines() if l.strip()][-4:]
        raise UserError((tail[-1] if tail else "ffmpeg failed")[:300])


# ---------------------------------------------------------------- codec selection
def _crf(quality: int) -> int:
    """0–100 quality → x264 CRF (51…14)."""
    return max(14, min(51, round(51 - quality * 0.37)))


def _audio_kbps(quality: int) -> int:
    return max(64, min(320, round(quality * 3)))


def _audio_args(target: str, quality: int) -> list[str]:
    kbps = _audio_kbps(quality)
    if target == "MP3":
        return ["-vn", "-c:a", "libmp3lame", "-b:a", f"{kbps}k"]
    if target == "M4A":
        return ["-vn", "-c:a", "aac", "-b:a", f"{kbps}k"]
    if target == "OGG":
        return ["-vn", "-c:a", "libvorbis", "-q:a", f"{max(0, min(10, round(quality / 10)))}"]
    if target == "OPUS":
        return ["-vn", "-c:a", "libopus", "-b:a", f"{min(256, kbps)}k"]
    if target == "FLAC":
        return ["-vn", "-c:a", "flac", "-compression_level", "8"]
    if target == "WAV":
        return ["-vn", "-c:a", "pcm_s16le"]
    raise UserError(f"Unsupported audio target {target}")


def _codec_args(target: str, quality: int, max_side: int | None) -> list[str]:
    vf = ["-vf", f"scale='min({max_side},iw)':-2:flags=lanczos"] if max_side else []
    if target in ("MP4", "MOV"):
        return ["-c:v", "libx264", "-preset", "medium", "-crf", str(_crf(quality)),
                *vf, "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", f"{_audio_kbps(quality)}k", "-movflags", "+faststart"]
    if target == "MKV":
        return ["-c:v", "libx264", "-preset", "medium", "-crf", str(_crf(quality)),
                *vf, "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", f"{_audio_kbps(quality)}k"]
    if target == "WEBM":
        return ["-c:v", "libvpx-vp9",
                "-crf", str(max(10, min(63, round(63 - quality * 0.53)))), "-b:v", "0",
                *vf, "-c:a", "libopus", "-b:a", f"{min(160, _audio_kbps(quality))}k"]
    if target in ("MP3", "M4A", "WAV", "FLAC", "OGG", "OPUS"):
        return _audio_args(target, quality)
    raise UserError(f"Unsupported target {target}")


# ---------------------------------------------------------------- conversions
def to_gif(src: str | Path, quality: int = 72, max_side: int | None = None,
           trim: tuple[float, float] | None = None, on_progress=None) -> Path:
    src = Path(src)
    info = probe(src)
    if not info.has_video:
        raise UserError("GIF needs a video.")
    out = sibling(src, ext=".gif")
    max_side = max_side or 640
    fps = 10 if quality < 45 else 14 if quality < 75 else 20
    dur, head = info.duration, []
    if trim:
        start, end = trim
        head = ["-ss", f"{start:.3f}"]
        dur = max(0.05, end - start)
    chain = (f"[0:v]fps={fps},scale='min({max_side},iw)':-2:flags=lanczos,split[a][b];"
             f"[a]palettegen=max_colors=256:stats_mode=diff[p];"
             f"[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle")
    args = [*head, "-i", str(src)]
    if trim:
        args += ["-t", f"{dur:.3f}"]
    args += ["-filter_complex", chain, "-loop", "0", "-an", str(out)]
    _run(args, duration=dur, on_progress=on_progress)
    return out


def convert_media(src: str | Path, target: str, quality: int = 72,
                  max_side: int | None = None, trim: tuple[float, float] | None = None,
                  on_progress=None) -> Path:
    src = Path(src)
    target = target.upper()
    info = probe(src)
    if target == "GIF":
        return to_gif(src, quality, max_side, trim, on_progress)
    if target in ("MP3", "M4A", "WAV", "FLAC", "OGG", "OPUS") and not info.has_audio:
        raise UserError("This file has no audio track.")
    if target in ("MP4", "WEBM", "MOV", "MKV") and not info.has_video:
        raise UserError("This file has no video track.")
    out = sibling(src, ext=_EXT.get(target, "." + target.lower()))
    dur, args = info.duration, []
    if trim:
        start, end = trim
        args += ["-ss", f"{start:.3f}"]
        dur = max(0.05, end - start)
    args += ["-i", str(src)]
    if trim:
        args += ["-t", f"{dur:.3f}"]
    args += _codec_args(target, quality, max_side)
    args += [str(out)]
    _run(args, duration=dur, on_progress=on_progress)
    return out


def compress_media(src: str | Path, quality: int = 72, max_side: int | None = None,
                   max_bytes: int | None = None, on_progress=None) -> Path:
    src = Path(src)
    info = probe(src)
    target = "MP4" if info.has_video else "M4A"
    if max_bytes and info.duration > 0:
        total_kbps = max_bytes * 8 / info.duration / 1000
        out = sibling(src, "Compressed", _EXT[target])
        if info.has_video:
            audio_kbps = min(160, max(48, total_kbps * 0.25)) if info.has_audio else 0
            video_kbps = max(80, total_kbps - audio_kbps)
            args = ["-i", str(src)]
            if max_side:
                args += ["-vf", f"scale='min({max_side},iw)':-2:flags=lanczos"]
            args += ["-c:v", "libx264", "-preset", "medium",
                     "-b:v", f"{video_kbps:.0f}k",
                     "-maxrate", f"{video_kbps * 1.4:.0f}k", "-bufsize", f"{video_kbps * 2:.0f}k",
                     "-pix_fmt", "yuv420p",
                     "-c:a", "aac", "-b:a", f"{audio_kbps:.0f}k",
                     "-movflags", "+faststart", str(out)]
        else:
            kbps = max(32, min(320, round(total_kbps)))
            args = ["-i", str(src), "-vn", "-c:a", "aac", "-b:a", f"{kbps}k", str(out)]
        _run(args, duration=info.duration, on_progress=on_progress)
        return out
    out = sibling(src, "Compressed", _EXT[target])
    _run(["-i", str(src), *_codec_args(target, quality, max_side), str(out)],
         duration=info.duration, on_progress=on_progress)
    return out


def trim_media(src: str | Path, start: float, end: float, target: str | None = None,
               copy: bool = False, on_progress=None) -> Path:
    src = Path(src)
    info = probe(src)
    dur = max(0.05, end - start)
    if copy:  # instant, but cuts at the nearest keyframe
        out = sibling(src, "Trimmed", src.suffix.lower())
        opts = ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    else:
        target = (target or ("MP4" if info.has_video else "M4A")).upper()
        out = sibling(src, "Trimmed", _EXT.get(target, "." + target.lower()))
        opts = _codec_args(target, 72, None)
    _run(["-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}", *opts, str(out)],
         duration=dur, on_progress=on_progress)
    return out


def strip_media_metadata(src: str | Path) -> Path:
    src = Path(src)
    out = sibling(src, "Clean", src.suffix.lower())
    _run(["-i", str(src), "-map_metadata", "-1", "-map_chapters", "-1",
          "-c", "copy", str(out)])
    return out


def extract_frames(src: str | Path, count: int = 6) -> list[Path]:
    src = Path(src)
    info = probe(src)
    if not info.has_video or info.duration <= 0:
        raise UserError("This file has no video track.")
    folder = unique_path(output_dir(src) / f"{src.stem} frames")
    folder.mkdir(parents=True, exist_ok=True)
    outs: list[Path] = []
    for i in range(count):
        t = info.duration * (i + 0.5) / count
        out = folder / f"frame {i + 1:02d}.png"
        _run(["-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1", str(out)])
        outs.append(out)
    return outs


def filmstrip(src: str | Path, count: int = 12, width: int = 160) -> bytes | None:
    """A single PNG with `count` frames in a row (one ffmpeg call, fast)."""
    info = probe(src)
    if info.duration <= 0 or not info.has_video:
        return None
    step = max(0.05, info.duration / count)
    try:
        r = subprocess.run(
            [_ffmpeg(), "-hide_banner", "-nostdin", "-i", str(src),
             "-vf", f"fps=1/{step:.6f},scale={width}:-2,tile={count}x1",
             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, timeout=45, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 and r.stdout else None

def all_tags(src: str | Path) -> dict[str, str]:
    """Every tag ffprobe reports (format + per-stream)."""
    try:
        r = subprocess.run(
            [_ffprobe(), "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(src)],
            capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW)
    except OSError:
        return {}
    data = json.loads(r.stdout or "{}")
    out: dict[str, str] = {}
    for k, v in (data.get("format", {}).get("tags") or {}).items():
        out[f"format · {k}"] = str(v)
    for i, s in enumerate(data.get("streams", [])):
        for k, v in (s.get("tags") or {}).items():
            out[f"{s.get('codec_type', '?')} {i} · {k}"] = str(v)
    return out