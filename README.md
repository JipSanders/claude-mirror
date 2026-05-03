# 🪞 claude-mirror

> *A reflection of how you collaborate with AI — generated entirely from your local `~/.claude` data.*

**claude-mirror** reads your Claude Code history and produces a beautiful, interactive HTML report that answers the question: **what kind of Claude Code collaborator are you?**

<img width="693" height="551" alt="Screenshot 2026-05-03 at 10 55 13" src="https://github.com/user-attachments/assets/309e9ed8-3384-491c-a1a2-1d2a1d27e2db" />

---

## What it reveals

- **Your collaborator personality type** — are you The Deep Diver, The Commander, The Architect, or The Explorer?
- **Activity heatmap** — when and how intensely you code
- **Prompt analysis** — how long and specific your prompts are, and what that says about your workflow
- **Session patterns** — marathon builder or rapid sprinter?
- **Project breakdown** — where you actually spend your time
- **Most used tools** — what Claude does most on your behalf
- **Correction rate** — how often you course-correct vs. trust the first response
- **AI insight** *(optional)* — Claude's own take on your collaboration style, generated from your stats

---

## Privacy

All analysis runs **100% locally**. Your conversation content never leaves your machine. The only optional network call is to the Anthropic API for the AI insight feature (opt-in via `ANTHROPIC_API_KEY`).

---

## Requirements

- Python 3.10+
- [Claude Code](https://github.com/anthropics/claude-code) installed and used at least once

---

## Installation

```bash
git clone https://github.com/jipsanders/claude-mirror
cd claude-mirror
pip install -r requirements.txt
```

---

## Usage

```bash
# Generate report and open in browser
python claude_mirror.py

# Save to a custom path
python claude_mirror.py -o my_report.html

# Include an AI-generated personal insight (uses Claude API)
ANTHROPIC_API_KEY=sk-ant-... python claude_mirror.py

# Generate without auto-opening browser
python claude_mirror.py --no-browser
```

---

## The four personality types

| Type | Style | Traits |
|------|-------|--------|
| 🤿 **The Deep Diver** | Long sessions, detailed prompts | Locks in for hours, trusts the process, builds ambitiously |
| ⚡ **The Commander** | Short prompts, long sessions | Decisive, iterates fast, knows exactly what they want |
| 🏗️ **The Architect** | Verbose prompts, shorter sessions | Plans before building, front-loads context, thinks in systems |
| 🗺️ **The Explorer** | Short sessions, many corrections | Iterative, curious, discovers through doing |

---

## How it works

1. Reads `~/.claude/stats-cache.json` for aggregate usage stats
2. Parses all `~/.claude/projects/**/*.jsonl` transcript files
3. Extracts user prompts, tool calls, timestamps, and correction patterns
4. Computes personality type based on prompt verbosity × session depth axes
5. Generates interactive Plotly charts embedded in a single self-contained HTML file

---

## Contributing

PRs welcome. Ideas for new metrics, personality types, or visualisations — open an issue.

---

*Built with [Claude Code](https://claude.ai/code)*
