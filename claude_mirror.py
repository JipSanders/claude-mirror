#!/usr/bin/env python3
"""
claude-mirror: A tool that reads your local ~/.claude data and reveals
what kind of Claude Code collaborator you are.
"""

import json
import os
import glob
import re
import argparse
import webbrowser
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path

try:
    import plotly.graph_objects as go
    import plotly.express as px
    from plotly.subplots import make_subplots
    import plotly.io as pio
except ImportError:
    print("Missing dependency: pip install plotly")
    raise SystemExit(1)

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

CLAUDE_DIR = Path.home() / ".claude"

CORRECTION_WORDS = {
    "no ", "nope", "wrong", "actually", "wait", "stop", "don't", "dont",
    "that's not", "thats not", "that is not", "undo", "revert", "not quite",
    "not right", "incorrect", "mistake", "redo", "try again", "not what",
    "you missed", "you forgot", "you didn't", "you don't", "fix this",
}

PERSONALITY_TYPES = {
    "deep_diver": {
        "title": "The Deep Diver",
        "emoji": "🤿",
        "description": "You lock in for marathon sessions with rich, detailed context. Claude is your co-pilot on long, ambitious builds — you trust the process and go all in.",
        "tip": "Your long sessions are a superpower, but consider leaving breadcrumb comments for your future self (and Claude) at session boundaries.",
    },
    "commander": {
        "title": "The Commander",
        "emoji": "⚡",
        "description": "Short, sharp, decisive. You know exactly what you want and say it in as few words as possible. You move fast and iterate hard.",
        "tip": "Your brevity is efficient, but adding one sentence of context per prompt can halve your back-and-forth. Try prefixing with 'Given that...' or 'The goal is...'",
    },
    "architect": {
        "title": "The Architect",
        "emoji": "🏗️",
        "description": "You plan before you build. Your prompts are thoughtful, structured, and show you've already mapped the problem space before asking.",
        "tip": "You're great at front-loading context. Try using CLAUDE.md files to persist that context across sessions so you don't repeat yourself.",
    },
    "explorer": {
        "title": "The Explorer",
        "emoji": "🗺️",
        "description": "You work iteratively, discovering as you go. Many sessions, lots of course-corrections — you're comfortable with ambiguity and figure things out in the open.",
        "tip": "Your exploratory style is creative, but consolidating your findings into a CLAUDE.md between sessions will help Claude keep up with your evolving vision.",
    },
}


# ──────────────────────────────────────────────────────────────────────────────
# PARSING
# ──────────────────────────────────────────────────────────────────────────────

def slug_to_project_name(slug: str) -> str:
    """Convert a directory slug like '-Users-jip-Desktop-FocusRogue' to 'FocusRogue'."""
    parts = slug.split("-")
    # Find the last meaningful part (skip Users, username, Desktop)
    skip = {"", "Users", "Desktop", "home"}
    meaningful = [p for p in parts if p and p not in skip]
    # The username is typically the third part after splitting on -
    # Just return the last segment if it looks like a project name
    if meaningful:
        # Try to reconstruct multi-word names with hyphens removed
        last = meaningful[-1]
        # If the last few parts form a path, take the last one
        return last
    return slug


def load_stats() -> dict:
    stats_path = CLAUDE_DIR / "stats-cache.json"
    if not stats_path.exists():
        return {}
    with open(stats_path) as f:
        return json.load(f)


def load_transcripts() -> list[dict]:
    """Load and parse all non-subagent JSONL transcript files."""
    pattern = str(CLAUDE_DIR / "projects" / "**" / "*.jsonl")
    files = [
        f for f in glob.glob(pattern, recursive=True)
        if "subagents" not in f
    ]

    sessions = []

    for filepath in files:
        # Derive project name from directory path
        rel = Path(filepath).relative_to(CLAUDE_DIR / "projects")
        project_slug = rel.parts[0]
        project_name = slug_to_project_name(project_slug)
        session_id = Path(filepath).stem

        messages = []
        try:
            with open(filepath, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        if d.get("type") in ("user", "assistant"):
                            messages.append(d)
                    except json.JSONDecodeError:
                        pass
        except (OSError, UnicodeDecodeError):
            continue

        if not messages:
            continue

        sessions.append({
            "session_id": session_id,
            "project_name": project_name,
            "project_slug": project_slug,
            "filepath": filepath,
            "messages": messages,
        })

    return sessions


# ──────────────────────────────────────────────────────────────────────────────
# ANALYSIS
# ──────────────────────────────────────────────────────────────────────────────

def extract_text(content) -> str:
    """Extract plain text from a message content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return " ".join(parts)
    return ""


def is_correction(text: str) -> bool:
    lower = text.lower().strip()
    for word in CORRECTION_WORDS:
        if lower.startswith(word) or f" {word}" in lower:
            return True
    return False


def analyze_sessions(sessions: list[dict]) -> dict:
    project_stats = defaultdict(lambda: {
        "sessions": 0,
        "user_messages": 0,
        "tool_calls": 0,
        "corrections": 0,
        "prompt_words": [],
    })

    all_prompts = []
    all_tool_names = defaultdict(int)
    timestamps = []
    session_lengths = []

    for session in sessions:
        proj = session["project_name"]
        project_stats[proj]["sessions"] += 1

        messages = session["messages"]
        user_msgs = [m for m in messages if m.get("type") == "user"]
        asst_msgs = [m for m in messages if m.get("type") == "assistant"]

        project_stats[proj]["user_messages"] += len(user_msgs)

        # Extract timestamps for session duration
        msg_timestamps = []
        for m in messages:
            ts = m.get("timestamp")
            if ts:
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    msg_timestamps.append(dt)
                    timestamps.append(dt)
                except ValueError:
                    pass

        if len(msg_timestamps) >= 2:
            duration_mins = (max(msg_timestamps) - min(msg_timestamps)).total_seconds() / 60
            session_lengths.append(duration_mins)

        # Analyze user messages
        prev_was_assistant = False
        for m in messages:
            if m.get("type") == "assistant":
                prev_was_assistant = True
                # Count tool calls
                content = m.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "tool_use":
                            tool_name = item.get("name", "unknown")
                            all_tool_names[tool_name] += 1
                            project_stats[proj]["tool_calls"] += 1

            elif m.get("type") == "user":
                content = m.get("message", {}).get("content", [])
                text = extract_text(content).strip()

                # Filter out system injections
                if text.startswith("<") or len(text) < 3:
                    prev_was_assistant = False
                    continue

                # Clean up ide_opened_file tags
                text = re.sub(r"<ide_opened_file>.*?</ide_opened_file>", "", text, flags=re.DOTALL).strip()
                if not text or len(text) < 3:
                    prev_was_assistant = False
                    continue

                word_count = len(text.split())
                all_prompts.append({"text": text, "words": word_count, "project": proj})
                project_stats[proj]["prompt_words"].append(word_count)

                if prev_was_assistant and is_correction(text):
                    project_stats[proj]["corrections"] += 1

                prev_was_assistant = False

    return {
        "project_stats": dict(project_stats),
        "all_prompts": all_prompts,
        "all_tool_names": dict(all_tool_names),
        "timestamps": timestamps,
        "session_lengths": session_lengths,
    }


def compute_personality(stats: dict, analysis: dict) -> dict:
    """Determine personality type from usage patterns."""
    prompts = analysis["all_prompts"]
    session_lengths = analysis["session_lengths"]

    avg_words = sum(p["words"] for p in prompts) / max(len(prompts), 1)
    avg_session_len = sum(session_lengths) / max(len(session_lengths), 1)

    total_corrections = sum(
        v["corrections"] for v in analysis["project_stats"].values()
    )
    total_user_msgs = sum(
        v["user_messages"] for v in analysis["project_stats"].values()
    )
    correction_rate = total_corrections / max(total_user_msgs, 1)

    # Axes: prompt verbosity (threshold: 15 words) + session depth (threshold: 60 mins)
    verbose = avg_words >= 15
    deep = avg_session_len >= 60

    if verbose and deep:
        ptype = "deep_diver"
    elif not verbose and deep:
        ptype = "commander"
    elif verbose and not deep:
        ptype = "architect"
    else:
        ptype = "explorer"

    return {
        "type": ptype,
        "avg_words_per_prompt": round(avg_words, 1),
        "avg_session_length_mins": round(avg_session_len, 1),
        "correction_rate": round(correction_rate * 100, 1),
        **PERSONALITY_TYPES[ptype],
    }


def compute_overview(stats: dict, analysis: dict) -> dict:
    total_sessions = stats.get("totalSessions", 0)
    total_messages = stats.get("totalMessages", 0)
    longest = stats.get("longestSession", {})
    longest_msgs = longest.get("messageCount", 0)
    first_date = stats.get("firstSessionDate", "")

    total_prompts = len(analysis["all_prompts"])
    total_tool_calls = sum(analysis["all_tool_names"].values())

    # Days active
    daily = stats.get("dailyActivity", [])
    days_active = len(daily)

    # Total tokens
    model_usage = stats.get("modelUsage", {})
    total_input = sum(v.get("inputTokens", 0) for v in model_usage.values())
    total_output = sum(v.get("outputTokens", 0) for v in model_usage.values())
    total_cache_read = sum(v.get("cacheReadInputTokens", 0) for v in model_usage.values())

    return {
        "total_sessions": total_sessions,
        "total_messages": total_messages,
        "total_prompts": total_prompts,
        "total_tool_calls": total_tool_calls,
        "longest_session_msgs": longest_msgs,
        "days_active": days_active,
        "first_date": first_date[:10] if first_date else "unknown",
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_cache_read_tokens": total_cache_read,
        "num_projects": len(analysis["project_stats"]),
    }


# ──────────────────────────────────────────────────────────────────────────────
# VISUALIZATIONS
# ──────────────────────────────────────────────────────────────────────────────

PLOTLY_THEME = {
    "paper_bgcolor": "rgba(0,0,0,0)",
    "plot_bgcolor": "rgba(0,0,0,0)",
    "font": {"color": "#e2e8f0", "family": "Inter, system-ui, sans-serif"},
    "margin": {"t": 40, "b": 40, "l": 40, "r": 20},
}

PURPLE = "#a78bfa"
BLUE = "#60a5fa"
PINK = "#f472b6"
TEAL = "#34d399"
ORANGE = "#fb923c"
COLORS = [PURPLE, BLUE, PINK, TEAL, ORANGE, "#facc15", "#e879f9", "#38bdf8"]


def fig_to_html(fig) -> str:
    return pio.to_html(fig, full_html=False, include_plotlyjs=False, config={"displayModeBar": False})


def make_activity_heatmap(stats: dict) -> str:
    daily = stats.get("dailyActivity", [])
    if not daily:
        return ""

    dates = [d["date"] for d in daily]
    counts = [d["messageCount"] for d in daily]

    fig = go.Figure(go.Bar(
        x=dates,
        y=counts,
        marker=dict(
            color=counts,
            colorscale=[[0, "#1e1b4b"], [0.3, "#4c1d95"], [0.6, "#7c3aed"], [1.0, "#a78bfa"]],
            showscale=False,
        ),
        hovertemplate="<b>%{x}</b><br>%{y} messages<extra></extra>",
    ))

    fig.update_layout(
        title="Message Activity Over Time",
        xaxis_title="",
        yaxis_title="Messages",
        **PLOTLY_THEME,
    )
    fig.update_xaxes(gridcolor="#1e293b", tickangle=-30)
    fig.update_yaxes(gridcolor="#1e293b")
    return fig_to_html(fig)


def make_hour_chart(stats: dict) -> str:
    hour_counts = stats.get("hourCounts", {})
    if not hour_counts:
        return ""

    hours_all = list(range(24))
    counts = [hour_counts.get(str(h), 0) for h in hours_all]
    labels = [f"{h:02d}:00" for h in hours_all]

    # Assign time-of-day colors
    bar_colors = []
    for h in hours_all:
        if 6 <= h < 12:
            bar_colors.append("#fbbf24")  # morning gold
        elif 12 <= h < 17:
            bar_colors.append(BLUE)       # afternoon blue
        elif 17 <= h < 21:
            bar_colors.append(PURPLE)     # evening purple
        else:
            bar_colors.append(PINK)       # night pink

    fig = go.Figure(go.Bar(
        x=labels,
        y=counts,
        marker_color=bar_colors,
        hovertemplate="<b>%{x}</b><br>%{y} sessions<extra></extra>",
    ))

    fig.update_layout(
        title="When Do You Code?",
        xaxis_title="Hour of Day",
        yaxis_title="Sessions",
        **PLOTLY_THEME,
    )
    fig.update_xaxes(gridcolor="#1e293b", tickangle=-45)
    fig.update_yaxes(gridcolor="#1e293b")
    return fig_to_html(fig)


def make_project_chart(project_stats: dict) -> str:
    if not project_stats:
        return ""

    # Sort by user messages descending
    sorted_projects = sorted(project_stats.items(), key=lambda x: x[1]["user_messages"], reverse=True)
    names = [p[0] for p in sorted_projects]
    msgs = [p[1]["user_messages"] for p in sorted_projects]
    tools = [p[1]["tool_calls"] for p in sorted_projects]
    sessions = [p[1]["sessions"] for p in sorted_projects]

    fig = go.Figure()

    fig.add_trace(go.Bar(
        name="Your Messages",
        x=msgs,
        y=names,
        orientation="h",
        marker_color=PURPLE,
        hovertemplate="<b>%{y}</b><br>%{x} messages<extra></extra>",
    ))

    fig.add_trace(go.Bar(
        name="Tool Calls",
        x=tools,
        y=names,
        orientation="h",
        marker_color=TEAL,
        hovertemplate="<b>%{y}</b><br>%{x} tool calls<extra></extra>",
    ))

    fig.update_layout(
        title="Activity by Project",
        barmode="group",
        xaxis_title="Count",
        height=max(300, len(names) * 55 + 100),
        **PLOTLY_THEME,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    fig.update_xaxes(gridcolor="#1e293b")
    fig.update_yaxes(gridcolor="#1e293b")
    return fig_to_html(fig)


def make_prompt_length_chart(prompts: list[dict]) -> str:
    if not prompts:
        return ""

    words = [p["words"] for p in prompts]

    fig = go.Figure(go.Histogram(
        x=words,
        nbinsx=30,
        marker=dict(
            color=words,
            colorscale=[[0, "#312e81"], [0.5, "#7c3aed"], [1.0, "#c4b5fd"]],
            line=dict(color="#1e1b4b", width=0.5),
        ),
        hovertemplate="<b>%{x} words</b><br>%{y} prompts<extra></extra>",
    ))

    avg = sum(words) / len(words)
    fig.add_vline(
        x=avg, line_dash="dash", line_color=ORANGE,
        annotation_text=f"avg: {avg:.0f}w", annotation_font_color=ORANGE,
    )

    fig.update_layout(
        title="Prompt Length Distribution",
        xaxis_title="Words per prompt",
        yaxis_title="Count",
        **PLOTLY_THEME,
    )
    fig.update_xaxes(gridcolor="#1e293b")
    fig.update_yaxes(gridcolor="#1e293b")
    return fig_to_html(fig)


def make_tool_chart(tool_names: dict) -> str:
    if not tool_names:
        return ""

    sorted_tools = sorted(tool_names.items(), key=lambda x: x[1], reverse=True)[:12]
    names = [t[0] for t in sorted_tools]
    counts = [t[1] for t in sorted_tools]

    fig = go.Figure(go.Bar(
        x=names,
        y=counts,
        marker=dict(
            color=counts,
            colorscale=[[0, "#164e63"], [0.5, "#0891b2"], [1.0, "#67e8f9"]],
            showscale=False,
        ),
        hovertemplate="<b>%{x}</b><br>%{y} calls<extra></extra>",
    ))

    fig.update_layout(
        title="Most Used Tools",
        xaxis_title="",
        yaxis_title="Total Calls",
        **PLOTLY_THEME,
    )
    fig.update_xaxes(gridcolor="#1e293b", tickangle=-30)
    fig.update_yaxes(gridcolor="#1e293b")
    return fig_to_html(fig)


def make_sessions_scatter(stats: dict, sessions: list[dict]) -> str:
    daily = stats.get("dailyActivity", [])
    if not daily:
        return ""

    dates = [d["date"] for d in daily]
    msgs = [d["messageCount"] for d in daily]
    tool_counts = [d.get("toolCallCount", 0) for d in daily]
    session_counts = [d.get("sessionCount", 1) for d in daily]

    # Intensity = msgs per session
    intensity = [m / max(s, 1) for m, s in zip(msgs, session_counts)]

    fig = go.Figure(go.Scatter(
        x=dates,
        y=msgs,
        mode="markers+lines",
        marker=dict(
            size=[min(6 + i / 20, 30) for i in intensity],
            color=tool_counts,
            colorscale=[[0, "#1e3a5f"], [0.5, "#2563eb"], [1.0, "#93c5fd"]],
            showscale=True,
            colorbar=dict(title="Tool Calls", tickfont=dict(color="#94a3b8")),
            line=dict(color="#1e293b", width=1),
        ),
        line=dict(color="#334155", width=1),
        hovertemplate="<b>%{x}</b><br>%{y} messages<br>%{marker.color} tool calls<extra></extra>",
    ))

    fig.update_layout(
        title="Session Intensity (bubble size = messages per session)",
        xaxis_title="",
        yaxis_title="Total Messages",
        **PLOTLY_THEME,
    )
    fig.update_xaxes(gridcolor="#1e293b", tickangle=-30)
    fig.update_yaxes(gridcolor="#1e293b")
    return fig_to_html(fig)


# ──────────────────────────────────────────────────────────────────────────────
# CLAUDE API INSIGHT (optional)
# ──────────────────────────────────────────────────────────────────────────────

def get_ai_insight(personality: dict, overview: dict, analysis: dict) -> str:
    if not HAS_ANTHROPIC:
        return ""

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return ""

    top_tools = sorted(analysis["all_tool_names"].items(), key=lambda x: x[1], reverse=True)[:5]
    top_projects = sorted(
        analysis["project_stats"].items(),
        key=lambda x: x[1]["user_messages"],
        reverse=True,
    )[:3]

    summary = f"""
Claude Code Usage Summary:
- Personality type: {personality['title']}
- Total sessions: {overview['total_sessions']}
- Total prompts written: {overview['total_prompts']}
- Average words per prompt: {personality['avg_words_per_prompt']}
- Average session length: {personality['avg_session_length_mins']} minutes
- Correction rate: {personality['correction_rate']}%
- Most used tools: {', '.join(f"{t[0]} ({t[1]}x)" for t in top_tools)}
- Most active projects: {', '.join(p[0] for p in top_projects)}
- Days active since {overview['first_date']}: {overview['days_active']}
"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": f"""You are analysing someone's Claude Code usage patterns. Based on these stats, write a 3-sentence personal insight that:
1. Acknowledges one specific strength in how they collaborate
2. Identifies one concrete area for improvement
3. Ends with an encouraging observation about their unique style

Be specific, warm, and a little playful. Use the data. Don't use bullet points — flowing prose only.

{summary}""",
            }],
        )
        return response.content[0].text
    except Exception:
        return ""


# ──────────────────────────────────────────────────────────────────────────────
# HTML REPORT
# ──────────────────────────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>claude-mirror — Your Claude Code Report</title>
<script src="https://cdn.plot.ly/plotly-2.26.0.min.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg: #080b14;
    --surface: #0f1729;
    --surface2: #162033;
    --border: #1e293b;
    --purple: #a78bfa;
    --purple-dim: #4c1d95;
    --blue: #60a5fa;
    --pink: #f472b6;
    --teal: #34d399;
    --orange: #fb923c;
    --text: #e2e8f0;
    --text-muted: #64748b;
    --text-dim: #94a3b8;
  }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', system-ui, sans-serif;
    font-size: 15px;
    line-height: 1.6;
    min-height: 100vh;
  }

  /* ── HEADER ── */
  .header {
    background: linear-gradient(135deg, #0f0c29 0%, #302b63 50%, #24243e 100%);
    border-bottom: 1px solid #2d1b69;
    padding: 60px 40px 50px;
    text-align: center;
    position: relative;
    overflow: hidden;
  }
  .header::before {
    content: '';
    position: absolute;
    inset: 0;
    background: radial-gradient(ellipse 80% 60% at 50% 0%, rgba(167,139,250,0.15) 0%, transparent 70%);
    pointer-events: none;
  }
  .header-logo {
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px;
    font-weight: 500;
    color: var(--purple);
    letter-spacing: 0.3em;
    text-transform: uppercase;
    margin-bottom: 16px;
    opacity: 0.8;
  }
  .header h1 {
    font-size: clamp(32px, 5vw, 52px);
    font-weight: 800;
    background: linear-gradient(135deg, #c4b5fd 0%, #818cf8 40%, #60a5fa 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    letter-spacing: -0.02em;
    margin-bottom: 12px;
  }
  .header .subtitle {
    font-size: 16px;
    color: var(--text-dim);
    font-weight: 400;
  }
  .generated-at {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    color: var(--text-muted);
    margin-top: 20px;
    letter-spacing: 0.05em;
  }

  /* ── LAYOUT ── */
  .container {
    max-width: 1200px;
    margin: 0 auto;
    padding: 40px 24px 80px;
  }

  .section-label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    font-weight: 500;
    color: var(--purple);
    letter-spacing: 0.2em;
    text-transform: uppercase;
    margin-bottom: 20px;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .section-label::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--border);
  }

  /* ── STAT CARDS ── */
  .stat-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 16px;
    margin-bottom: 48px;
  }
  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
    transition: border-color 0.2s, transform 0.2s;
  }
  .stat-card:hover {
    border-color: var(--purple-dim);
    transform: translateY(-2px);
  }
  .stat-card .label {
    font-size: 11px;
    font-weight: 500;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin-bottom: 8px;
  }
  .stat-card .value {
    font-size: 28px;
    font-weight: 700;
    color: var(--text);
    font-family: 'JetBrains Mono', monospace;
    letter-spacing: -0.02em;
  }
  .stat-card .value.purple { color: var(--purple); }
  .stat-card .value.blue   { color: var(--blue); }
  .stat-card .value.teal   { color: var(--teal); }
  .stat-card .value.pink   { color: var(--pink); }
  .stat-card .value.orange { color: var(--orange); }
  .stat-card .sub {
    font-size: 11px;
    color: var(--text-muted);
    margin-top: 4px;
  }

  /* ── PERSONALITY CARD ── */
  .personality-card {
    background: linear-gradient(135deg, #1a1040 0%, #0f1729 60%, #0a1628 100%);
    border: 1px solid #3b0764;
    border-radius: 20px;
    padding: 40px;
    margin-bottom: 48px;
    position: relative;
    overflow: hidden;
  }
  .personality-card::before {
    content: '';
    position: absolute;
    top: -40px;
    right: -40px;
    width: 200px;
    height: 200px;
    background: radial-gradient(circle, rgba(167,139,250,0.15) 0%, transparent 70%);
    pointer-events: none;
  }
  .personality-emoji {
    font-size: 48px;
    margin-bottom: 12px;
    display: block;
  }
  .personality-type-label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    color: var(--purple);
    letter-spacing: 0.2em;
    text-transform: uppercase;
    margin-bottom: 8px;
  }
  .personality-title {
    font-size: 36px;
    font-weight: 800;
    background: linear-gradient(135deg, #e9d5ff 0%, #a78bfa 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    margin-bottom: 16px;
    letter-spacing: -0.02em;
  }
  .personality-description {
    font-size: 16px;
    color: var(--text-dim);
    max-width: 680px;
    line-height: 1.7;
    margin-bottom: 24px;
  }
  .personality-tip {
    background: rgba(167,139,250,0.08);
    border: 1px solid rgba(167,139,250,0.2);
    border-radius: 10px;
    padding: 16px 20px;
    font-size: 14px;
    color: var(--text-dim);
    display: flex;
    gap: 12px;
    align-items: flex-start;
    max-width: 680px;
  }
  .personality-tip::before {
    content: '💡';
    flex-shrink: 0;
    font-size: 16px;
  }

  .personality-stats {
    display: flex;
    gap: 24px;
    margin-top: 24px;
    flex-wrap: wrap;
  }
  .p-stat {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .p-stat-label {
    font-size: 11px;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    font-weight: 500;
  }
  .p-stat-value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 20px;
    font-weight: 600;
    color: var(--purple);
  }

  /* ── AI INSIGHT ── */
  .ai-insight {
    background: linear-gradient(135deg, #0c1a1a 0%, #0f2020 100%);
    border: 1px solid #134e4a;
    border-radius: 16px;
    padding: 28px 32px;
    margin-bottom: 48px;
    position: relative;
  }
  .ai-insight-label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    color: var(--teal);
    letter-spacing: 0.2em;
    text-transform: uppercase;
    margin-bottom: 12px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .ai-insight-text {
    font-size: 15px;
    color: #a7f3d0;
    line-height: 1.8;
  }

  /* ── CHART CARDS ── */
  .chart-grid {
    display: grid;
    gap: 20px;
    margin-bottom: 48px;
  }
  .chart-grid.two { grid-template-columns: repeat(auto-fit, minmax(460px, 1fr)); }
  .chart-grid.one { grid-template-columns: 1fr; }

  .chart-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 24px;
    transition: border-color 0.2s;
  }
  .chart-card:hover { border-color: #334155; }

  /* ── FOOTER ── */
  footer {
    text-align: center;
    padding: 40px 24px;
    border-top: 1px solid var(--border);
    color: var(--text-muted);
    font-size: 13px;
  }
  footer a {
    color: var(--purple);
    text-decoration: none;
  }
  footer a:hover { text-decoration: underline; }
  .footer-logo {
    font-family: 'JetBrains Mono', monospace;
    font-size: 16px;
    font-weight: 600;
    color: var(--text-dim);
    margin-bottom: 8px;
  }
</style>
</head>
<body>

<div class="header">
  <div class="header-logo">claude-mirror</div>
  <h1>Your Claude Code Report</h1>
  <p class="subtitle">A reflection of how you collaborate with AI — from your local data</p>
  <p class="generated-at">Generated %%generated_at%% · All data stays on your machine</p>
</div>

<div class="container">

  <!-- OVERVIEW STATS -->
  <div class="section-label">Overview</div>
  <div class="stat-grid">
    <div class="stat-card">
      <div class="label">Sessions</div>
      <div class="value purple">%%total_sessions%%</div>
      <div class="sub">since %%first_date%%</div>
    </div>
    <div class="stat-card">
      <div class="label">Messages</div>
      <div class="value blue">%%total_messages%%</div>
      <div class="sub">total in history</div>
    </div>
    <div class="stat-card">
      <div class="label">Your Prompts</div>
      <div class="value teal">%%total_prompts%%</div>
      <div class="sub">things you typed</div>
    </div>
    <div class="stat-card">
      <div class="label">Tool Calls</div>
      <div class="value pink">%%total_tool_calls%%</div>
      <div class="sub">actions taken</div>
    </div>
    <div class="stat-card">
      <div class="label">Projects</div>
      <div class="value orange">%%num_projects%%</div>
      <div class="sub">directories worked in</div>
    </div>
    <div class="stat-card">
      <div class="label">Days Active</div>
      <div class="value purple">%%days_active%%</div>
      <div class="sub">coding days logged</div>
    </div>
    <div class="stat-card">
      <div class="label">Longest Session</div>
      <div class="value blue">%%longest_session_msgs%%</div>
      <div class="sub">messages in one go</div>
    </div>
    <div class="stat-card">
      <div class="label">Cache Tokens Read</div>
      <div class="value teal">%%cache_tokens_m%%M</div>
      <div class="sub">tokens saved by cache</div>
    </div>
  </div>

  <!-- PERSONALITY -->
  <div class="section-label">Your Collaborator Type</div>
  <div class="personality-card">
    <span class="personality-emoji">%%personality_emoji%%</span>
    <div class="personality-type-label">Personality Type</div>
    <div class="personality-title">%%personality_title%%</div>
    <p class="personality-description">%%personality_description%%</p>
    <div class="personality-stats">
      <div class="p-stat">
        <span class="p-stat-label">Avg Prompt Length</span>
        <span class="p-stat-value">%%avg_words%%w</span>
      </div>
      <div class="p-stat">
        <span class="p-stat-label">Avg Session</span>
        <span class="p-stat-value">%%avg_session%%m</span>
      </div>
      <div class="p-stat">
        <span class="p-stat-label">Correction Rate</span>
        <span class="p-stat-value">%%correction_rate%%%</span>
      </div>
    </div>
    <div class="personality-tip">%%personality_tip%%</div>
  </div>

  %%ai_insight_block%%

  <!-- ACTIVITY -->
  <div class="section-label">Activity</div>
  <div class="chart-grid one">
    <div class="chart-card">%%chart_activity%%</div>
  </div>

  <div class="chart-grid two">
    <div class="chart-card">%%chart_sessions%%</div>
    <div class="chart-card">%%chart_hours%%</div>
  </div>

  <!-- PROMPTS & TOOLS -->
  <div class="section-label">How You Write</div>
  <div class="chart-grid two">
    <div class="chart-card">%%chart_prompt_length%%</div>
    <div class="chart-card">%%chart_tools%%</div>
  </div>

  <!-- PROJECTS -->
  <div class="section-label">Projects</div>
  <div class="chart-grid one">
    <div class="chart-card">%%chart_projects%%</div>
  </div>

</div>

<footer>
  <div class="footer-logo">claude-mirror</div>
  <p>Your data never left your machine ·
     Built with <a href="https://github.com/anthropics/claude-code">Claude Code</a> ·
     <a href="https://github.com/jipsanders/claude-mirror">View on GitHub</a>
  </p>
</footer>

</body>
</html>
"""

AI_INSIGHT_BLOCK = """
  <div class="section-label">AI Insight</div>
  <div class="ai-insight">
    <div class="ai-insight-label">&#10022; Claude's read on you</div>
    <p class="ai-insight-text">%%insight%%</p>
  </div>
"""


def build_report(
    stats: dict,
    sessions: list[dict],
    analysis: dict,
    overview: dict,
    personality: dict,
    ai_insight: str,
    output_path: str,
) -> None:
    charts = {
        "chart_activity": make_activity_heatmap(stats),
        "chart_hours": make_hour_chart(stats),
        "chart_projects": make_project_chart(analysis["project_stats"]),
        "chart_prompt_length": make_prompt_length_chart(analysis["all_prompts"]),
        "chart_tools": make_tool_chart(analysis["all_tool_names"]),
        "chart_sessions": make_sessions_scatter(stats, sessions),
    }

    ai_block = ""
    if ai_insight:
        ai_block = AI_INSIGHT_BLOCK.replace("%%insight%%", ai_insight)

    substitutions = {
        "%%generated_at%%": datetime.now().strftime("%B %d, %Y at %H:%M"),
        "%%total_sessions%%": str(overview["total_sessions"]),
        "%%first_date%%": str(overview["first_date"]),
        "%%total_messages%%": f"{overview['total_messages']:,}",
        "%%total_prompts%%": f"{overview['total_prompts']:,}",
        "%%total_tool_calls%%": f"{overview['total_tool_calls']:,}",
        "%%num_projects%%": str(overview["num_projects"]),
        "%%days_active%%": str(overview["days_active"]),
        "%%longest_session_msgs%%": f"{overview['longest_session_msgs']:,}",
        "%%cache_tokens_m%%": f"{overview['total_cache_read_tokens'] / 1_000_000:.1f}",
        "%%personality_emoji%%": personality["emoji"],
        "%%personality_title%%": personality["title"],
        "%%personality_description%%": personality["description"],
        "%%personality_tip%%": personality["tip"],
        "%%avg_words%%": str(personality["avg_words_per_prompt"]),
        "%%avg_session%%": str(personality["avg_session_length_mins"]),
        "%%correction_rate%%": str(personality["correction_rate"]),
        "%%ai_insight_block%%": ai_block,
        "%%chart_activity%%": charts["chart_activity"],
        "%%chart_hours%%": charts["chart_hours"],
        "%%chart_projects%%": charts["chart_projects"],
        "%%chart_prompt_length%%": charts["chart_prompt_length"],
        "%%chart_tools%%": charts["chart_tools"],
        "%%chart_sessions%%": charts["chart_sessions"],
    }
    html = HTML_TEMPLATE
    for key, value in substitutions.items():
        html = html.replace(key, value)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)


# ──────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="claude-mirror: See yourself through your Claude Code usage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python claude_mirror.py                    # open report in browser
  python claude_mirror.py -o my_report.html  # save to custom path
  ANTHROPIC_API_KEY=sk-... python claude_mirror.py  # include AI insight
        """,
    )
    parser.add_argument(
        "-o", "--output",
        default="claude_mirror_report.html",
        help="Output HTML file path (default: claude_mirror_report.html)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't open the report in a browser automatically",
    )
    args = parser.parse_args()

    print("🪞  claude-mirror")
    print("─" * 40)

    print("  📂  Loading stats...")
    stats = load_stats()
    if not stats:
        print("  ⚠️   No stats-cache.json found in ~/.claude — have you used Claude Code yet?")

    print("  📜  Loading transcripts...")
    sessions = load_transcripts()
    print(f"       Found {len(sessions)} session(s) across {len(set(s['project_name'] for s in sessions))} project(s)")

    print("  🔬  Analysing...")
    analysis = analyze_sessions(sessions)
    overview = compute_overview(stats, analysis)
    personality = compute_personality(stats, analysis)

    print(f"  🧠  You are: {personality['emoji']} {personality['title']}")

    ai_insight = ""
    if HAS_ANTHROPIC and os.environ.get("ANTHROPIC_API_KEY"):
        print("  ✦   Generating AI insight...")
        ai_insight = get_ai_insight(personality, overview, analysis)
    else:
        print("  ℹ️   Set ANTHROPIC_API_KEY for a personalised AI insight")

    print("  🎨  Building report...")
    build_report(stats, sessions, analysis, overview, personality, ai_insight, args.output)

    print(f"\n  ✅  Report saved to: {args.output}")

    if not args.no_browser:
        webbrowser.open(f"file://{os.path.abspath(args.output)}")
        print("  🌐  Opening in browser...")

    print()


if __name__ == "__main__":
    main()
