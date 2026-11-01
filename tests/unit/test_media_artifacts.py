"""Step 5.1 media-artifact checks: voiceover text, subtitles, vhs tape, render.

Covers the static inputs (no network, no vhs/ffmpeg needed) plus graceful
mp4 checks that skip until the OCI render lands. Rendering + minting of
Step 5.1 happen only after the 3.4 package (green E2E).
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

PKG_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
MEDIA = PKG_ROOT / "media"
VO_TXT = MEDIA / "voiceover_en.txt"
VO_SRT = MEDIA / "voiceover_en.srt"
VO_RU = MEDIA / "voiceover_ru.txt"
TAPE = MEDIA / "demo.tape"
RENDER = PKG_ROOT / "scripts" / "render_video.py"
MP4 = MEDIA / "hackproof-screencast-1080p.mp4"
DOSSIER = PKG_ROOT / "benchmarks" / "evidence_dossier.json"

CHAR_BUDGET = 3000
N_SCENES = 6
N_SRT_CARDS = 22
VIDEO_MIN_S = 175.0
VIDEO_MAX_S = 185.0

_TS_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})"
)


def _read_voiceover_paragraphs() -> list[str]:
    lines = [
        line
        for line in VO_TXT.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    ]
    return [p.strip() for p in "\n".join(lines).split("\n\n") if p.strip()]


def _ts_to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _parse_srt_strict(text: str) -> list[tuple[int, float, float, list[str]]]:
    """Independent minimal SRT parser: numbering, timing, ≤2 lines/card."""
    cards: list[tuple[int, float, float, list[str]]] = []
    blocks = [b for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    for block in blocks:
        rows = block.splitlines()
        assert len(rows) >= 3, f"malformed SRT block: {block[:80]!r}"
        seq = int(rows[0].strip())
        match = _TS_RE.fullmatch(rows[1].strip())
        assert match is not None, f"bad timestamp line: {rows[1]!r}"
        g = match.groups()
        start = _ts_to_seconds(*g[0:4])
        end = _ts_to_seconds(*g[4:8])
        assert start < end, f"start >= end in card {seq}"
        body = rows[2:]
        assert 1 <= len(body) <= 2, f"card {seq}: {len(body)} text lines"
        assert all(line.strip() for line in body), f"card {seq}: blank line"
        cards.append((seq, start, end, body))
    return cards


def _load_render_module():
    spec = importlib.util.spec_from_file_location("render_video", RENDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_video"] = module
    spec.loader.exec_module(module)
    return module


# --- voiceover_en.txt ----------------------------------------------------


def test_voiceover_has_six_paragraphs() -> None:
    assert VO_TXT.is_file(), "media/voiceover_en.txt missing"
    paras = _read_voiceover_paragraphs()
    assert len(paras) == N_SCENES, f"expected 6 scene paragraphs, got {len(paras)}"


def test_voiceover_char_budget() -> None:
    paras = _read_voiceover_paragraphs()
    billed = sum(len(p) for p in paras)
    assert billed <= CHAR_BUDGET, f"TTS budget blown: {billed} > {CHAR_BUDGET}"
    assert billed > 1000, f"suspiciously short voiceover: {billed} chars"


def test_voiceover_has_zero_unfilled_tokens() -> None:
    text = VO_TXT.read_text(encoding="utf-8")
    tokens = re.findall(r"\{[a-z_]+\}", text)
    assert tokens == [], f"unfilled voiceover tokens: {tokens}"


def test_voiceover_key_phrases_verbatim_v2() -> None:
    text = VO_TXT.read_text(encoding="utf-8")
    for phrase in (
        "Watch closely: an AI agent deletes a failing assertion",
        "This is Reward Hacking.",
        "HackProof AI eliminates reward hacking at the root.",
        "We don't ask for trust",
        "Across 12 polyglot tasks",
        "100% Meta-Native",
        "open-source under Apache 2.0",
    ):
        assert phrase in text, f"missing Script v2 phrase: {phrase!r}"


def test_voiceover_figures_match_evidence_dossier() -> None:
    dossier = json.loads(DOSSIER.read_text(encoding="utf-8"))
    tfr = dossier["metrics_4_4"]["true_fix_rate"]
    assert tfr["A"] == "6/12", tfr
    assert tfr["B"] == "9/12", tfr
    assert tfr["C"] == "7/12", tfr
    assert dossier["scoreboard"]["denominator"] == 12
    text = VO_TXT.read_text(encoding="utf-8")
    assert "fixed 6" in text and "fixed 7" in text and "fixed 9" in text
    assert "zero test-tampering violations" in text


# --- voiceover_en.srt ----------------------------------------------------


def test_srt_numbering_timing_and_shape() -> None:
    assert VO_SRT.is_file(), "media/voiceover_en.srt missing"
    cards = _parse_srt_strict(VO_SRT.read_text(encoding="utf-8"))
    assert len(cards) == N_SRT_CARDS, f"expected 22 cards, got {len(cards)}"
    assert [c[0] for c in cards] == list(range(1, N_SRT_CARDS + 1))
    prev_end = 0.0
    for seq, start, end, _body in cards:
        assert start >= prev_end, f"card {seq}: overlap/backwards timing"
        prev_end = end
    assert cards[-1][2] <= 180.0, f"last card ends at {cards[-1][2]}s > 180s"


# --- voiceover_ru.txt (personal-only reference, content not gated) --------


def test_ru_reference_exists_with_personal_only_header() -> None:
    assert VO_RU.is_file(), "media/voiceover_ru.txt missing"
    head = "\n".join(VO_RU.read_text(encoding="utf-8").splitlines()[:3])
    assert "ТОЛЬКО ДЛЯ ЛИЧНОГО ОЗНАКОМЛЕНИЯ ФАУНДЕРА" in head
    assert "Исключён из TTS" in head


# --- demo.tape ------------------------------------------------------------


def test_tape_output_paths_are_relative() -> None:
    assert TAPE.is_file(), "media/demo.tape missing"
    outputs = [
        line.split(None, 1)[1].strip().strip("'\"")
        for line in TAPE.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("Output ")
    ]
    assert outputs, "tape has no Output directive"
    for out in outputs:
        assert not out.startswith(("/", "~")), f"absolute Output: {out}"
        assert not re.match(r"[A-Za-z]:", out), f"absolute Output: {out}"


def test_tape_has_required_settings() -> None:
    text = TAPE.read_text(encoding="utf-8")
    for directive in (
        "Set Width 1280",
        "Set Height 720",
        "Set Framerate 60",
        'Set Shell "bash"',
    ):
        assert directive in text, f"missing tape directive: {directive}"


def test_tape_has_six_ordered_scenes_and_types() -> None:
    text = TAPE.read_text(encoding="utf-8")
    markers = re.findall(r"^#\s*Scene\s+(\d)/6", text, flags=re.M)
    assert markers == [str(n) for n in range(1, 7)], f"scene markers: {markers}"
    n_types = len(re.findall(r"^Type\s", text, flags=re.M))
    assert n_types >= N_SCENES, f"only {n_types} Type commands for 6 scenes"
    assert "Screenshot" not in text, "tape must not emit screenshot sidecars"


def test_tape_sleep_budget_near_180s() -> None:
    """Predicted vhs duration must land in the 172–182s window.

    vhs 0.12.1 on OCI Ampere captures 720p at ~0.90 of nominal tape
    time (measured: 178.0s nominal -> 160.4s video); the static
    estimate is scaled by that ratio before the window check.
    """
    text = TAPE.read_text(encoding="utf-8")
    sleep_total = 0.0
    for value, unit in re.findall(r"^Sleep\s+([\d.]+)(ms|s|m)?", text, flags=re.M):
        factor = {"ms": 0.001, "s": 1.0, "m": 60.0, "": 1.0}[unit or "s"]
        sleep_total += float(value) * factor
    typed_chars = sum(
        len(m) for m in re.findall(r'^Type\s+"(.*)"\s*$', text, flags=re.M)
    )
    estimate = sleep_total + typed_chars * 0.04 + 20 * 0.5
    predicted = estimate * 0.90
    assert 172.0 <= predicted <= 182.0, (
        f"predicted vhs {predicted:.1f}s (nominal {estimate:.1f}s: "
        f"sleep {sleep_total:.1f}s, typed {typed_chars} chars) "
        f"outside 172–182s window"
    )


# --- render_video.py ------------------------------------------------------


def test_render_script_help_and_dry_run() -> None:
    assert RENDER.is_file(), "scripts/render_video.py missing"
    proc = subprocess.run(
        [sys.executable, str(RENDER), "--dry-run"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=PKG_ROOT,
    )
    assert proc.returncode == 0, f"--dry-run failed:\n{proc.stdout}\n{proc.stderr}"
    for marker in ("6 scenes", "3000", "demo.tape", "voiceover_en.srt"):
        assert marker in proc.stdout, f"--dry-run missing {marker!r}"


def test_render_retime_keeps_cards_monotonic_inside_scenes() -> None:
    render = _load_render_module()
    cards = _parse_srt_strict(VO_SRT.read_text(encoding="utf-8"))
    fake_durations = [13.0, 15.0, 25.0, 22.0, 11.0, 11.0]
    timed = render.retime_srt(cards, fake_durations, render.SCENE_OFFSETS_S)
    assert len(timed) == len(cards)
    prev_end = 0.0
    for (seq, _s0, _e0, body), (_seq2, start, end, _b2) in zip(cards, timed):
        assert start < end, f"card {seq}: start >= end after retime"
        assert start >= prev_end - 1e-6, f"card {seq}: overlap after retime"
        assert body == _b2
        prev_end = end
    assert timed[-1][2] <= 180.0


def test_render_constants_match_gates() -> None:
    render = _load_render_module()
    assert render.VOICEOVER_BUDGET == CHAR_BUDGET
    assert (render.DURATION_MIN_S, render.DURATION_MAX_S) == (
        VIDEO_MIN_S,
        VIDEO_MAX_S,
    )
    assert len(render.SCENE_OFFSETS_S) == N_SCENES
    assert "AI-PRODUCED VIDEO + VOICEOVER" in render.WATERMARK_TEXT
    assert "TERMINAL OUTPUT REAL" in render.WATERMARK_TEXT
    assert (render.CAPTURE_W, render.CAPTURE_H) == (1280, 720)
    assert (render.FINAL_W, render.FINAL_H) == (1920, 1080)


# --- mp4 + TTS outputs (graceful skip until the OCI render lands) ---------


def test_mp4_duration_and_resolution() -> None:
    if not MP4.is_file():
        pytest.skip("mp4 not rendered yet (render after package 3.4)")
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        pytest.skip("ffprobe absent — founder confirms checklist manually")
    dur = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(MP4)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert dur.returncode == 0, dur.stderr
    seconds = float(dur.stdout.strip())
    assert VIDEO_MIN_S <= seconds <= VIDEO_MAX_S, f"mp4 duration {seconds}s"
    res = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(MP4)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "1920,1080", res.stdout


def test_tts_segments_complete_when_present() -> None:
    tts_dir = MEDIA / "tts"
    if not tts_dir.is_dir() or not list(tts_dir.glob("scene*.mp3")):
        pytest.skip("TTS segments not synthesized yet (render after package 3.4)")
    missing = [
        f"scene{n}.mp3"
        for n in range(1, 7)
        if not (tts_dir / f"scene{n}.mp3").is_file()
    ]
    assert missing == [], f"missing TTS segments: {missing}"
