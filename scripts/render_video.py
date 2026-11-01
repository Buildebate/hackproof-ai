#!/usr/bin/env python3
"""Step 5.1 video renderer: vhs tape + 6x ElevenLabs TTS + subtitles/watermark.

Pipeline (run on OCI from ``packages/hackproof-ai/``)::

    1. vhs media/demo.tape              -> media/vhs_raw.mp4 (~180s, 1280x720)
    2. ElevenLabs TTS x 6 paragraphs    -> media/tts/scene{N}.mp3 (cached)
    3. retime DRAFT srt by real TTS len -> media/voiceover_en.timed.srt
    4. ffmpeg: upscale 720p->1080p, burn srt + watermark at 1080p,
       delayed/mixed voiceover          -> main_av.mp4
    5. ffmpeg: 3s end-card + concat     -> hackproof-screencast-1080p.mp4
    6. ffprobe asserts: 175-185s, 1920x1080, audio+video streams.

Capture is 720p by hardware necessity: vhs 0.12.1 (Chromium software
rasterizer) sustains realtime 25fps capture at 720p on OCI Ampere but
only ~8fps at 1080p, which compresses a 178s tape into ~96s of video
(measured 95.72s, 25fps output, Set Framerate ignored). Subtitles and
the watermark render AFTER the lanczos upscale, so overlay text stays
native-1080p crisp; terminal pixels are upscaled (disclosed).

Stdlib-only (urllib for ElevenLabs; no new deps). Render + minting of Step 5.1
happen only after the 3.4 package (green E2E).

Environment:
    ELEVENLABS_API_KEY   required for TTS (never printed; --dry-run skips it)
    ELEVENLABS_VOICE_ID  default Alice Xb7hH8MSUJpSbSDYk0k2 (premade; Rachel is library-gated on free tier)
    ELEVENLABS_MODEL_ID  default eleven_multilingual_v2
    VHS_BIN              default /home/ubuntu/.local/bin/vhs, then PATH
    FFMPEG_BIN / FFPROBE_BIN, VIDEO_FONTFILE (drawtext font override)

Iteration loop: the first OCI render measures the real vhs duration. If the
final mp4 falls outside 175-185s, adjust one Sleep in demo.tape and re-run
with --skip-tts (TTS mp3s are cached, so retakes cost zero free-tier chars).

Exit codes: 0 ok, 2 usage/env error, 1 pipeline failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
MEDIA_DIR = PKG_ROOT / "media"
TTS_DIR = MEDIA_DIR / "tts"
TAPE = MEDIA_DIR / "demo.tape"
VO_TXT = MEDIA_DIR / "voiceover_en.txt"
VO_SRT = MEDIA_DIR / "voiceover_en.srt"

VOICEOVER_BUDGET = 3000
N_SCENES = 6
DURATION_MIN_S = 175.0
DURATION_MAX_S = 185.0
TAPE_TARGET_S = 180.0
ENDCARD_S = 3.0
# vhs capture raster (720p, realtime on OCI) and final raster (1080p).
CAPTURE_W, CAPTURE_H = 1280, 720
FINAL_W, FINAL_H = 1920, 1080

SCENE_NAMES = ["cold-open", "problem", "live-demo", "proof", "meta-stack", "ask"]
# Audio placement = MEASURED vhs scene starts (s) + ~0.5s settle; retime
# keeps these anchors. Measured on OCI 720p (vhs_raw 180.16s): computed
# nominal x 0.9187, cross-checked against clear-frame events (±1s sc1-4)
# and boundary thumbnails (sc5/sc6 confirmed visually).
SCENE_OFFSETS_S = [0.5, 16.0, 39.5, 108.5, 149.0, 164.5]
# 1-based inclusive DRAFT-card ranges per scene (22 cards total).
SCENE_CARDS = [(1, 3), (4, 6), (7, 12), (13, 17), (18, 19), (20, 22)]

WATERMARK_TEXT = "AI-PRODUCED VIDEO + VOICEOVER \u00b7 TERMINAL OUTPUT REAL"
ENDCARD_LINES = [
    "HackProof AI \u2014 Meta Global AI Hackathon 2026",
    "AI-PRODUCED VIDEO + VOICEOVER \u00b7 TERMINAL OUTPUT REAL",
    "Voice: ElevenLabs AI \u00b7 Subtitles: burned-in EN + sidecar SRT",
    "github.com/Buildebate/hackproof-ai \u00b7 pip install hackproof-ai \u00b7 Apache-2.0",
]

DEFAULT_VHS = "/home/ubuntu/.local/bin/vhs"
DEFAULT_VOICE_ID = "Xb7hH8MSUJpSbSDYk0k2"  # Alice (Clear Educator); premade, free-tier API OK (Rachel is library-gated for free users)
DEFAULT_MODEL_ID = "eleven_multilingual_v2"
ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
ELEVENLABS_FORMAT = "mp3_44100_128"

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]
FONT_NAME = "DejaVu Sans"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PIPELINE = 1

_TS_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})"
)


class RenderError(RuntimeError):
    """Fatal pipeline failure (exit 1)."""


def eprint(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def run(cmd: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"timeout after {timeout}s: {cmd[0]}") from exc
    except OSError as exc:
        raise RenderError(f"cannot run {cmd[0]}: {exc}") from exc


def read_paragraphs(path: Path) -> list[str]:
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    ]
    return [p.strip() for p in "\n".join(lines).split("\n\n") if p.strip()]


def check_voiceover(path: Path = VO_TXT) -> list[str]:
    if not path.is_file():
        raise RenderError(f"missing voiceover text: {path}")
    paras = read_paragraphs(path)
    if len(paras) != N_SCENES:
        raise RenderError(f"expected {N_SCENES} scene paragraphs, got {len(paras)}")
    billed = sum(len(p) for p in paras)
    if billed > VOICEOVER_BUDGET:
        raise RenderError(f"TTS budget blown: {billed} > {VOICEOVER_BUDGET}")
    tokens = re.findall(r"\{[a-z_]+\}", path.read_text(encoding="utf-8"))
    if tokens:
        raise RenderError(f"unfilled voiceover tokens: {tokens}")
    return paras


def parse_srt(path: Path) -> list[tuple[int, float, float, list[str]]]:
    def ts(h: str, m: str, s: str, ms: str) -> float:
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

    cards: list[tuple[int, float, float, list[str]]] = []
    blocks = [b for b in re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip()) if b.strip()]
    for block in blocks:
        rows = block.splitlines()
        if len(rows) < 3:
            raise RenderError(f"malformed SRT block: {block[:80]!r}")
        match = _TS_RE.fullmatch(rows[1].strip())
        if match is None:
            raise RenderError(f"bad SRT timestamp line: {rows[1]!r}")
        g = match.groups()
        cards.append((int(rows[0].strip()), ts(*g[0:4]), ts(*g[4:8]), rows[2:]))
    seqs = [c[0] for c in cards]
    if seqs != list(range(1, len(cards) + 1)):
        raise RenderError(f"SRT numbering not sequential: {seqs[:5]}...")
    prev_end = 0.0
    for seq, start, end, body in cards:
        if not (prev_end <= start < end):
            raise RenderError(f"SRT card {seq}: non-monotonic timing")
        if not (1 <= len(body) <= 2):
            raise RenderError(f"SRT card {seq}: {len(body)} text lines")
        prev_end = end
    return cards


def format_ts(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def retime_srt(
    cards: list[tuple[int, float, float, list[str]]],
    durations: list[float],
    offsets: list[float] = SCENE_OFFSETS_S,
) -> list[tuple[int, float, float, list[str]]]:
    """Redistribute each scene's cards across its real TTS duration.

    Card starts anchor at the scene offset; widths scale with card char
    count so speech and burned-in captions stay in sync.
    """
    if len(durations) != N_SCENES or len(offsets) != N_SCENES:
        raise RenderError("retime needs 6 durations + 6 offsets")
    by_seq = {c[0]: c for c in cards}
    timed: list[tuple[int, float, float, list[str]]] = []
    for scene, ((first, last), offset, duration) in enumerate(
        zip(SCENE_CARDS, offsets, durations)
    ):
        group = [by_seq[n] for n in range(first, last + 1)]
        weights = [max(sum(len(line) for line in c[3]), 1) for c in group]
        total = sum(weights)
        cursor = offset
        for card, weight in zip(group, weights):
            width = duration * weight / total
            start, end = round(cursor, 3), round(cursor + width, 3)
            if not end > start:
                raise RenderError(f"retime collapse on card {card[0]}")
            timed.append((card[0], start, end, card[3]))
            cursor += width
        nxt = offsets[scene + 1] if scene + 1 < N_SCENES else TAPE_TARGET_S
        if cursor > nxt + 1e-6:
            eprint(f"WARNING: scene {scene + 1} TTS overflows by {cursor - nxt:.1f}s")
    return timed


def write_srt(cards: list[tuple[int, float, float, list[str]]], path: Path) -> None:
    out = []
    for seq, start, end, body in cards:
        out.append(f"{seq}\n{format_ts(start)} --> {format_ts(end)}\n" + "\n".join(body))
    path.write_text("\n\n".join(out) + "\n", encoding="utf-8")


def check_tape(path: Path = TAPE) -> float:
    """Validate tape directives; return static duration estimate (s)."""
    if not path.is_file():
        raise RenderError(f"missing tape: {path}")
    text = path.read_text(encoding="utf-8")
    for out in [
        line.split(None, 1)[1].strip().strip("'\"")
        for line in text.splitlines()
        if line.strip().startswith("Output ")
    ]:
        if out.startswith(("/", "~")) or re.match(r"[A-Za-z]:", out):
            raise RenderError(f"tape Output must be relative: {out}")
    for directive in (
        f"Set Width {CAPTURE_W}",
        f"Set Height {CAPTURE_H}",
        "Set Framerate 60",
    ):
        if directive not in text:
            raise RenderError(f"tape missing directive: {directive}")
    markers = re.findall(r"^#\s*Scene\s+(\d)/6", text, flags=re.M)
    if markers != [str(n) for n in range(1, 7)]:
        raise RenderError(f"tape scene markers broken: {markers}")
    sleep_total = 0.0
    for value, unit in re.findall(r"^Sleep\s+([\d.]+)(ms|s|m)?", text, flags=re.M):
        sleep_total += float(value) * {"ms": 0.001, "s": 1.0, "m": 60.0, "": 1.0}[unit or "s"]
    typed = sum(len(m) for m in re.findall(r'^Type\s+"(.*)"\s*$', text, flags=re.M))
    return sleep_total + typed * 0.04 + 10.0


def which_vhs(explicit: str | None) -> str:
    for candidate in (explicit, os.environ.get("VHS_BIN"), DEFAULT_VHS):
        if candidate and (Path(candidate).is_file() or shutil.which(candidate)):
            return candidate
    found = shutil.which("vhs")
    if found:
        return found
    raise RenderError("vhs not found (VHS_BIN or PATH)")


def which_bin(explicit: str | None, env: str, name: str) -> str:
    for candidate in (explicit, os.environ.get(env)):
        if candidate:
            if Path(candidate).is_file() or shutil.which(candidate):
                return candidate
            raise RenderError(f"{name} not found: {candidate}")
    found = shutil.which(name)
    if not found:
        raise RenderError(f"{name} not found in PATH")
    return found


def ffprobe_duration(ffprobe: str, path: Path) -> float:
    proc = run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path.name],
        cwd=path.parent,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RenderError(f"ffprobe duration failed: {proc.stderr.strip()}")
    return float(proc.stdout.strip())


def ffprobe_video_size(ffprobe: str, path: Path) -> tuple[int, int]:
    proc = run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", path.name],
        cwd=path.parent,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RenderError(f"ffprobe size failed: {proc.stderr.strip()}")
    width, height = proc.stdout.strip().split(",")
    return int(width), int(height)


def ffprobe_has_audio(ffprobe: str, path: Path) -> bool:
    proc = run(
        [ffprobe, "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", path.name],
        cwd=path.parent,
        timeout=120,
    )
    return proc.returncode == 0 and "audio" in proc.stdout


def tts_synthesize(api_key: str, voice_id: str, model_id: str, text: str) -> bytes:
    body = json.dumps(
        {
            "text": text,
            "model_id": model_id,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }
    ).encode()
    url = ELEVENLABS_URL.format(voice_id=voice_id) + f"?output_format={ELEVENLABS_FORMAT}"
    req = urllib.request.Request(
        url, data=body,
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        hint = {
            401: "invalid ELEVENLABS_API_KEY",
            402: "ElevenLabs quota/payment exceeded",
            429: "ElevenLabs rate-limited — retry later",
        }.get(exc.code, f"ElevenLabs HTTP {exc.code}")
        raise RenderError(f"{hint}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RenderError(f"ElevenLabs request failed: {exc}") from exc


def drawtext_font_args() -> str:
    override = os.environ.get("VIDEO_FONTFILE")
    if override:
        if not Path(override).is_file():
            raise RenderError(f"VIDEO_FONTFILE missing: {override}")
        return f"fontfile={override.replace(':', chr(92) + ':')}"
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return f"fontfile={candidate}"
    return f"font='{FONT_NAME}'"


def cmd_dry_run() -> int:
    print("render_video.py --dry-run: validating Step 5.1 inputs (no binaries, no network)")
    paras = check_voiceover()
    billed = sum(len(p) for p in paras)
    print(f"voiceover: {len(paras)} scenes / {billed} chars of {VOICEOVER_BUDGET} budget")
    for i, (name, off, para) in enumerate(zip(SCENE_NAMES, SCENE_OFFSETS_S, paras), 1):
        print(f"  scene{i} {name:10s} tts@{off:6.1f}s  {len(para):4d} chars")
    cards = parse_srt(VO_SRT)
    print(f"subtitles: {VO_SRT.name}: {len(cards)} cards, last end {cards[-1][2]:.1f}s")
    estimate = check_tape()
    print(f"tape: {TAPE.name}: 6 scenes, static estimate {estimate:.1f}s (target 180s)")
    print(f"watermark: {WATERMARK_TEXT}")
    print(f"end-card: {ENDCARD_S:.0f}s + final assert [{DURATION_MIN_S:.0f},{DURATION_MAX_S:.0f}]s")
    for label, probe in (("vhs", shutil.which("vhs") or DEFAULT_VHS),
                         ("ffmpeg", shutil.which("ffmpeg")),
                         ("ffprobe", shutil.which("ffprobe"))):
        print(f"tool {label}: {probe or 'MISSING (needed only for real render)'}")
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip().strip("'\"")
    print(f"elevenlabs key: {'set (hidden)' if key else 'MISSING (needed only for TTS)'}")
    print("dry-run OK")
    return EXIT_OK


def cmd_render(args: argparse.Namespace) -> int:
    stages: dict[str, float] = {}

    def stage(name: str, started: float) -> None:
        stages[name] = round(time.time() - started, 1)

    total_start = time.time()
    ffmpeg = which_bin(args.ffmpeg_bin, "FFMPEG_BIN", "ffmpeg")
    ffprobe = which_bin(args.ffprobe_bin, "FFPROBE_BIN", "ffprobe")
    vhs_raw = MEDIA_DIR / "vhs_raw.mp4"
    main_av = MEDIA_DIR / "main_av.mp4"
    endcard = MEDIA_DIR / "endcard.mp4"
    final = MEDIA_DIR / "hackproof-screencast-1080p.mp4"
    timed_srt = MEDIA_DIR / "voiceover_en.timed.srt"

    paras = check_voiceover()
    cards = parse_srt(VO_SRT)
    estimate = check_tape()
    print(f"inputs OK: 6 scenes, {sum(len(p) for p in paras)} TTS chars, "
          f"{len(cards)} srt cards, tape estimate {estimate:.1f}s")

    if args.skip_vhs:
        if not vhs_raw.is_file():
            raise RenderError("--skip-vhs but media/vhs_raw.mp4 missing")
        print("--skip-vhs: reusing media/vhs_raw.mp4")
    else:
        vhs = which_vhs(args.vhs_bin)
        started = time.time()
        proc = run([vhs, "media/demo.tape"], cwd=PKG_ROOT, timeout=1800)
        stage("vhs", started)
        if proc.returncode != 0:
            raise RenderError(f"vhs failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
        print(f"vhs done in {stages['vhs']}s")
    vhs_dur = ffprobe_duration(ffprobe, vhs_raw)
    print(f"vhs_raw duration: {vhs_dur:.1f}s")
    if not (DURATION_MIN_S - ENDCARD_S <= vhs_dur <= DURATION_MAX_S - ENDCARD_S):
        eprint(f"WARNING: vhs {vhs_dur:.1f}s + {ENDCARD_S:.0f}s end-card may miss "
               f"[{DURATION_MIN_S:.0f},{DURATION_MAX_S:.0f}]s — adjust a Sleep, re-run --skip-tts")

    TTS_DIR.mkdir(exist_ok=True)
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip().strip("'\"")
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", DEFAULT_VOICE_ID)
    model_id = os.environ.get("ELEVENLABS_MODEL_ID", DEFAULT_MODEL_ID)
    seg_paths = [TTS_DIR / f"scene{n}.mp3" for n in range(1, 7)]
    billed_now = 0
    if args.skip_tts:
        missing = [str(p) for p in seg_paths if not p.is_file()]
        if missing:
            raise RenderError(f"--skip-tts but missing segments: {missing}")
        print("--skip-tts: reusing media/tts/scene{1..6}.mp3")
    else:
        if not api_key:
            eprint("ELEVENLABS_API_KEY is not set")
            return EXIT_USAGE
        for n, (para, seg) in enumerate(zip(paras, seg_paths), 1):
            if seg.is_file() and seg.stat().st_size > 0 and not args.force_tts:
                print(f"tts scene{n}: cached ({seg.stat().st_size} bytes)")
                continue
            started = time.time()
            audio = tts_synthesize(api_key, voice_id, model_id, para)
            seg.write_bytes(audio)
            stage(f"tts_{n}", started)
            billed_now += len(para)
            print(f"tts scene{n}: {len(audio)} bytes in {stages[f'tts_{n}']}s")
    durations = [ffprobe_duration(ffprobe, p) for p in seg_paths]
    for n, (dur, off) in enumerate(zip(durations, SCENE_OFFSETS_S), 1):
        window = (SCENE_OFFSETS_S[n] if n < N_SCENES else TAPE_TARGET_S) - off
        flag = "" if dur <= window else "  <-- OVERFLOWS scene window"
        print(f"tts scene{n}: {dur:.1f}s @ {off:.1f}s (window {window:.1f}s){flag}")
    print(f"TTS chars billed this run: {billed_now} (voice {voice_id}, model {model_id})")

    started = time.time()
    if args.no_retime:
        burn_srt = VO_SRT.name
        print("--no-retime: burning DRAFT voiceover_en.srt verbatim")
    else:
        write_srt(retime_srt(cards, durations), timed_srt)
        burn_srt = timed_srt.name
        print(f"retimed {len(cards)} cards by real TTS durations -> {burn_srt}")
    stage("retime", started)

    font_args = drawtext_font_args()
    audio_chain = "".join(
        f"[{n}:a]adelay={int(off * 1000)}|{int(off * 1000)},aresample=44100[a{n}];"
        for n, off in enumerate(SCENE_OFFSETS_S, 1)
    )
    filt = (
        f"[0:v]scale={FINAL_W}:{FINAL_H}:flags=lanczos[hd];"
        f"[hd]subtitles={burn_srt}:force_style='FontName={FONT_NAME},FontSize=20,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BorderStyle=1,Outline=2,"
        "Shadow=0,MarginV=28'[sub];"
        f"[sub]drawtext=text='{WATERMARK_TEXT}':{font_args}:fontsize=22:"
        "fontcolor=white@0.85:borderw=1:bordercolor=black@0.6:"
        "x=w-text_w-24:y=24[v];"
        f"{audio_chain}"
        "[a1][a2][a3][a4][a5][a6]amix=inputs=6:normalize=0:duration=longest[mix];"
        "[mix]apad[a]"
    )
    started = time.time()
    proc = run(
        [ffmpeg, "-y", "-i", "vhs_raw.mp4",
         *[a for p in seg_paths for a in ("-i", f"tts/{p.name}")],
         "-filter_complex", filt, "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-pix_fmt", "yuv420p", "-r", "60",
         "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
         "-movflags", "+faststart", "-shortest", "main_av.mp4"],
        cwd=MEDIA_DIR,
        timeout=1200,
    )
    stage("mux_main", started)
    if proc.returncode != 0:
        raise RenderError(f"ffmpeg mux failed:\n{proc.stderr[-3000:]}")
    print(f"mux+burn+watermark done in {stages['mux_main']}s")

    (MEDIA_DIR / "endcard.txt").write_text("\n".join(ENDCARD_LINES) + "\n", encoding="utf-8")
    started = time.time()
    proc = run(
        [ffmpeg, "-y",
         "-f", "lavfi", "-i", f"color=c=0x0b0e14:s={FINAL_W}x{FINAL_H}:r=60:d={ENDCARD_S:.0f}",
         "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={ENDCARD_S:.0f}",
         "-vf", f"drawtext=textfile=endcard.txt:{font_args}:fontsize=44:"
                "fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2:line_spacing=18",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "60",
         "-c:a", "aac", "-shortest", "endcard.mp4"],
        cwd=MEDIA_DIR,
        timeout=600,
    )
    stage("endcard", started)
    if proc.returncode != 0:
        raise RenderError(f"ffmpeg end-card failed:\n{proc.stderr[-3000:]}")
    print(f"end-card ({ENDCARD_S:.0f}s) done in {stages['endcard']}s")

    started = time.time()
    proc = run(
        [ffmpeg, "-y", "-i", "main_av.mp4", "-i", "endcard.mp4",
         "-filter_complex",
         "[0:v][1:v]concat=n=2:v=1:a=0[v];[0:a][1:a]concat=n=2:v=0:a=1[a]",
         "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-pix_fmt", "yuv420p", "-r", "60",
         "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
         "-movflags", "+faststart", "hackproof-screencast-1080p.mp4"],
        cwd=MEDIA_DIR,
        timeout=1200,
    )
    stage("concat", started)
    if proc.returncode != 0:
        raise RenderError(f"ffmpeg concat failed:\n{proc.stderr[-3000:]}")

    started = time.time()
    final_dur = ffprobe_duration(ffprobe, final)
    width, height = ffprobe_video_size(ffprobe, final)
    has_audio = ffprobe_has_audio(ffprobe, final)
    stage("probe", started)
    print(f"final: {final_dur:.1f}s, {width}x{height}, audio={'yes' if has_audio else 'NO'}")
    if not (DURATION_MIN_S <= final_dur <= DURATION_MAX_S):
        raise RenderError(f"duration {final_dur:.1f}s outside [{DURATION_MIN_S:.0f},{DURATION_MAX_S:.0f}]s")
    if (width, height) != (FINAL_W, FINAL_H):
        raise RenderError(f"resolution {width}x{height} != {FINAL_W}x{FINAL_H}")
    if not has_audio:
        raise RenderError("final mp4 has no audio stream")

    stages["TOTAL"] = round(time.time() - total_start, 1)
    print("\nstage timing (s, dossier-ready):")
    for name, secs in stages.items():
        print(f"  {name:10s} {secs:8.1f}")
    print(f"RENDER OK: {final.name}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render the Step 5.1 screencast (vhs + ElevenLabs + ffmpeg)."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="validate inputs and print the plan only")
    parser.add_argument("--skip-vhs", action="store_true",
                        help="reuse media/vhs_raw.mp4")
    parser.add_argument("--skip-tts", action="store_true",
                        help="reuse media/tts/scene{1..6}.mp3")
    parser.add_argument("--force-tts", action="store_true",
                        help="re-synthesize even cached segments (spends free-tier chars)")
    parser.add_argument("--no-retime", action="store_true",
                        help="burn DRAFT srt verbatim instead of TTS-timed srt")
    parser.add_argument("--vhs-bin", default=None, help="vhs binary override")
    parser.add_argument("--ffmpeg-bin", default=None, help="ffmpeg binary override")
    parser.add_argument("--ffprobe-bin", default=None, help="ffprobe binary override")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.dry_run:
            return cmd_dry_run()
        return cmd_render(args)
    except RenderError as exc:
        eprint(f"render_video: {exc}")
        return EXIT_PIPELINE


if __name__ == "__main__":
    sys.exit(main())
