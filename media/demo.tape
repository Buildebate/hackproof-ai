# HackProof AI — 180s technical screencast (Step 5.1, Script v2).
# Render: run `vhs media/demo.tape` from packages/hackproof-ai/ on OCI
# (see scripts/render_video.py — never hand-run with absolute Output paths).
# All terminal content is REAL command output; nothing is typed as fake
# output. Voiceover: media/voiceover_en.txt (6 scenes, 1 paragraph each).
#
# Timing budget (TypingSpeed 40ms + measured exec + explicit holds):
#   Scene 1 cold open  0:00-0:15 (15s)  voiceover 0:01-0:15
#   Scene 2 problem    0:15-0:37 (22s)  voiceover 0:15-0:35
#   Scene 3 live demo  0:37-1:52 (75s)  voiceover 0:37-1:13, then silent demo
#   Scene 4 proof      1:52-2:32 (40s)  voiceover 1:52-2:22
#   Scene 5 stack      2:32-2:47 (15s)  voiceover 2:32-2:44
#   Scene 6 ask        2:47-3:00 (13s)  voiceover 2:47-2:59
# Total: 180s. First OCI render measures the real duration; if outside
# 175-185s, adjust one Sleep and re-run vhs only (TTS is cached).

Output media/vhs_raw.mp4

Require python3

Set Shell "bash"
Set FontSize 22
Set Width 1920
Set Height 1080
Set Framerate 60
Set TypingSpeed 40ms
Set Theme "Catppuccin Frappe"
Set Padding 24
Set WindowBar Colorful

Sleep 1s

# Scene 1/6: Cold open — the crime scene (0:00-0:15)
Type "sed -n '1,45p' tests/unit/test_sandbox_guards.py"
Enter
Sleep 1s
Sleep 6s
Type "grep -n 'def test' tests/unit/test_sandbox_guards.py"
Enter
Sleep 1s
Sleep 2s

# Scene 2/6: The problem — Reward Hacking (0:15-0:37)
Type "PYTHONPATH=src python3 -m hackproof.cli info"
Enter
Sleep 1s
Sleep 4s
Type "grep -n '9/12' benchmarks/SUMMARY.md | head -6"
Enter
Sleep 1s
Sleep 5s
Type "sed -n '1,25p' benchmarks/SUMMARY.md"
Enter
Sleep 1s
Sleep 4s

# Scene 3/6: Live demo — real CLI, real guard, real proof (0:37-1:52)
Type "clear"
Enter
Sleep 1s
Type "PYTHONPATH=src python3 -m hackproof.cli verify --help"
Enter
Sleep 1s
Sleep 4s
Type "python3 -m pytest tests/unit/test_sandbox_guards.py -q -p no:cacheprovider"
Enter
Wait+Screen@30s /passed/
Sleep 10s
Type "python3 scripts/verify_benchmark_integrity.py"
Enter
Sleep 3s
Sleep 13s
Type "PYTHONPATH=src python3 -m hackproof.cli test-guard --help"
Enter
Sleep 1s
Sleep 5s
Type "git log --oneline -6"
Enter
Sleep 1s
Sleep 7s
Type "ls src/hackproof/"
Enter
Sleep 1s
Sleep 6s

# Scene 4/6: Proof — measured numbers, zero violations (1:52-2:32)
Type "clear"
Enter
Sleep 1s
Type "grep -n 'true_fix_rate' -A 5 benchmarks/evidence_dossier.json | head -12"
Enter
Sleep 1s
Sleep 6.5s
Type "grep -n 'reward_hacking_rate' -A 4 benchmarks/evidence_dossier.json | head -10"
Enter
Sleep 1s
Sleep 6.5s
Type "sed -n '1,30p' benchmarks/SUMMARY.md"
Enter
Sleep 1s
Sleep 5s
Type "python3 scripts/verify_benchmark_integrity.py | tail -25"
Enter
Sleep 3s
Sleep 4s

# Scene 5/6: Meta-Native stack (2:32-2:47)
Type "clear"
Enter
Sleep 1s
Type "sed -n '1,30p' pyproject.toml"
Enter
Sleep 1s
Sleep 5s
Type "ls src/hackproof/ && wc -l src/hackproof/*.py | tail -3"
Enter
Sleep 1s
Sleep 3s

# Scene 6/6: Ask — links on screen (2:47-3:00)
Type "clear"
Enter
Sleep 1s
Type "grep -n 'pip install' README.md | head -3"
Enter
Sleep 1s
Sleep 3s
Type "echo 'github.com/Buildebate/hackproof-ai  |  pip install hackproof-ai'"
Enter
Sleep 1s
Sleep 4s
