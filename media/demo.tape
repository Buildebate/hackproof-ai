# HackProof AI — 180s technical screencast (Step 5.1, Script v2).
# Render: run `vhs media/demo.tape` from packages/hackproof-ai/ on OCI
# (see scripts/render_video.py — never hand-run with absolute Output paths).
# Capture is 1280x720 (vhs realtime limit on OCI Ampere; 1080p capture
# compresses time). render_video.py upscales to 1920x1080 with lanczos
# BEFORE burning subtitles/watermark, so overlays stay native-1080p.
# All terminal content is REAL command output; nothing is typed as fake
# output. Voiceover: media/voiceover_en.txt (6 scenes, 1 paragraph each).
#
# Timing budget (TypingSpeed 40ms + measured exec + explicit holds):
#   Scene 1 cold open  0:01-0:16 (15s)  voiceover 0:00-0:15
#   Scene 2 problem    0:16-0:39 (23s)  voiceover 0:16-0:31
#   Scene 3 live demo  0:39-1:48 (69s)  voiceover 0:39-1:04, then silent demo
#   Scene 4 proof      1:48-2:29 (41s)  voiceover 1:48-2:10
#   Scene 5 stack      2:29-2:44 (15s)  voiceover 2:29-2:40
#   Scene 6 ask        2:44-3:00 (16s)  voiceover 2:44-2:55
# Total: 180s VIDEO time (measured vhs_raw 180.16s on OCI 720p; nominal
# tape time x0.9187). Scene starts verified via clear-frames + thumbs.

Output media/vhs_raw.mp4

Require python3

Set Shell "bash"
Set FontSize 22
Set Width 1280
Set Height 720
Set Framerate 60
Set TypingSpeed 40ms
Set Theme "Catppuccin Frappe"
Set Padding 24
Set WindowBar Colorful

Sleep 1s

# Scene 1/6: Cold open — the crime scene (0:00-0:15)
Type "sed -n '1,30p' tests/unit/test_sandbox_guards.py"
Enter
Sleep 1s
Sleep 7s
Type "grep -n 'def test' tests/unit/test_sandbox_guards.py"
Enter
Sleep 1s
Sleep 2.5s

# Scene 2/6: The problem — Reward Hacking (0:15-0:37)
Type "clear"
Enter
Sleep 1s
Type "PYTHONPATH=src python3 -m hackproof.cli info"
Enter
Sleep 1s
Sleep 4.5s
Type "grep -n '9/12' benchmarks/SUMMARY.md | head -6"
Enter
Sleep 1s
Sleep 6s
Type "sed -n '1,25p' benchmarks/SUMMARY.md"
Enter
Sleep 1s
Sleep 4.5s

# Scene 3/6: Live demo — real CLI, real guard, real proof (0:37-1:52)
Type "clear"
Enter
Sleep 1s
Type "PYTHONPATH=src python3 -m hackproof.cli verify --help"
Enter
Sleep 1s
Sleep 4.5s
Type "python3 -m pytest tests/unit/test_sandbox_guards.py -q -p no:cacheprovider"
Enter
Wait+Screen@30s /passed/
Sleep 11s
Type "python3 scripts/verify_benchmark_integrity.py | tail -45"
Enter
Sleep 3.5s
Sleep 14.5s
Type "PYTHONPATH=src python3 -m hackproof.cli test-guard --help"
Enter
Sleep 1s
Sleep 6s
Type "git log --oneline -6"
Enter
Sleep 1s
Sleep 8s
Type "ls src/hackproof/"
Enter
Sleep 1s
Sleep 7s

# Scene 4/6: Proof — measured numbers, zero violations (1:52-2:32)
Type "clear"
Enter
Sleep 1s
Type "grep -n 'true_fix_rate' -A 5 benchmarks/evidence_dossier.json | head -12"
Enter
Sleep 1s
Sleep 7.5s
Type "grep -n 'reward_hacking_rate' -A 4 benchmarks/evidence_dossier.json | head -10"
Enter
Sleep 1s
Sleep 7.5s
Type "sed -n '1,30p' benchmarks/SUMMARY.md"
Enter
Sleep 1s
Sleep 6s
Type "python3 scripts/verify_benchmark_integrity.py | tail -25"
Enter
Sleep 3.5s
Sleep 4.5s

# Scene 5/6: Meta-Native stack (2:32-2:47)
Type "clear"
Enter
Sleep 1s
Type "sed -n '1,30p' pyproject.toml"
Enter
Sleep 1s
Sleep 6s
Type "ls src/hackproof/ && wc -l src/hackproof/*.py | tail -3"
Enter
Sleep 1s
Sleep 3.5s

# Scene 6/6: Ask — links on screen (2:47-3:00)
Type "clear"
Enter
Sleep 1s
Type "grep -n 'pip install' README.md | head -3"
Enter
Sleep 1s
Sleep 3.5s
Type "echo 'github.com/Buildebate/hackproof-ai  |  pip install hackproof-ai'"
Enter
Sleep 1s
Sleep 4.5s
