#!/usr/bin/env python3
"""
Voyager OS 01 — Live GitHub Telemetry SVG Generator (stdlib-only).

Queries GitHub's official GraphQL API (when GITHUB_TOKEN is set) or public
REST API to build an animated 880x220 Voyager Telemetry Console SVG with zero
third-party dependencies. Falls back cleanly to a curated baseline snapshot if
run offline or rate-limited.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import http.client
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple


LANG_COLORS: Dict[str, str] = {
    "C#": "#9A86FF",
    "Python": "#FFB454",
    "JavaScript": "#F7DF1E",
    "TypeScript": "#3178C6",
    "Java": "#FF8C52",
    "C++": "#6FE3FF",
    "C": "#5B8DEF",
    "HTML": "#FF6B6B",
    "CSS": "#6C63FF",
    "ShaderLab": "#3DDC84",
    "HLSL": "#3DDC84",
    "GLSL": "#50E3C2",
    "Shell": "#7DFFB0",
    "SQL": "#4CC9F0",
    "Kotlin": "#B18CFF",
    "Go": "#00ADD8",
    "Rust": "#DEA584",
    "Dart": "#00B4AB",
    "Jupyter Notebook": "#F37626",
    "ASP.NET": "#3DDC84",
}

FALLBACK_PALETTE = ["#FFB454", "#6FE3FF", "#9A86FF", "#3DDC84", "#FF7A90"]

DEFAULT_OFFLINE_DATA = {
    "contributions": None,
    "repositories": 12,
    "stars": 8,
    "followers": 6,
    "languages": [
        ("C#", 34.0, "#9A86FF"),
        ("Python", 26.5, "#FFB454"),
        ("JavaScript", 19.5, "#F7DF1E"),
        ("Java", 11.5, "#FF8C52"),
        ("HTML / CSS", 8.5, "#6FE3FF"),
    ],
    "source": "BASELINE SNAPSHOT",
}

GRAPHQL_QUERY = """
query($login: String!) {
  user(login: $login) {
    followers { totalCount }
    repositories(first: 100, ownerAffiliations: OWNER, isFork: false, privacy: PUBLIC) {
      totalCount
      nodes {
        stargazerCount
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges {
            size
            node { name color }
          }
        }
      }
    }
    contributionsCollection {
      contributionCalendar {
        totalContributions
      }
    }
  }
}
"""


def _http_json(
    url: str,
    headers: Dict[str, str],
    payload: Optional[Dict] = None,
    timeout: int = 12,
) -> Dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_graphql(username: str, token: str) -> Optional[Dict]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Voyager-OS-01-Telemetry/1.0",
    }
    body = _http_json(
        "https://api.github.com/graphql",
        headers=headers,
        payload={"query": GRAPHQL_QUERY, "variables": {"login": username}},
    )
    if "errors" in body and not body.get("data"):
        return None
    user = (body.get("data") or {}).get("user")
    if not user:
        return None

    repos = user.get("repositories") or {}
    nodes = repos.get("nodes") or []
    stars = sum(int(n.get("stargazerCount") or 0) for n in nodes if n)
    lang_bytes: Dict[str, int] = {}
    lang_colors: Dict[str, str] = {}

    for node in nodes:
        if not node:
            continue
        edges = ((node.get("languages") or {}).get("edges")) or []
        for edge in edges:
            if not edge:
                continue
            size = int(edge.get("size") or 0)
            lang_node = edge.get("node") or {}
            name = lang_node.get("name")
            if name and size > 0:
                lang_bytes[name] = lang_bytes.get(name, 0) + size
                if lang_node.get("color"):
                    lang_colors[name] = lang_node["color"]

    languages = _rank_languages(lang_bytes, lang_colors)
    contribs = (
        ((user.get("contributionsCollection") or {}).get("contributionCalendar") or {})
        .get("totalContributions")
    )
    followers = (user.get("followers") or {}).get("totalCount", 0)

    return {
        "contributions": int(contribs) if contribs is not None else None,
        "repositories": int(repos.get("totalCount") or len(nodes)),
        "stars": int(stars),
        "followers": int(followers),
        "languages": languages,
        "source": "GITHUB GRAPHQL API",
    }


def fetch_rest(username: str, token: Optional[str] = None) -> Optional[Dict]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Voyager-OS-01-Telemetry/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    user = _http_json(f"https://api.github.com/users/{username}", headers=headers)
    repos = _http_json(
        f"https://api.github.com/users/{username}/repos?per_page=100&type=owner&sort=updated",
        headers=headers,
    )
    if not isinstance(repos, list):
        repos = []

    non_forks = [r for r in repos if isinstance(r, dict) and not r.get("fork")]
    stars = sum(int(r.get("stargazers_count") or 0) for r in non_forks)

    lang_weights: Dict[str, int] = {}
    for repo in non_forks:
        lang = repo.get("language")
        if lang:
            weight = max(1, int(repo.get("size") or 1))
            lang_weights[lang] = lang_weights.get(lang, 0) + weight

    languages = _rank_languages(lang_weights, {})

    # Estimate recent public activity from events endpoint when unauthenticated
    recent_commits: Optional[int] = None
    try:
        events = _http_json(
            f"https://api.github.com/users/{username}/events/public?per_page=100",
            headers=headers,
        )
        if isinstance(events, list):
            total = 0
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                if ev.get("type") == "PushEvent":
                    commits = (ev.get("payload") or {}).get("commits")
                    total += len(commits) if isinstance(commits, list) and commits else 1
                elif ev.get("type") in ("PullRequestEvent", "IssuesEvent", "CreateEvent"):
                    total += 1
            recent_commits = total
    except Exception:
        recent_commits = None

    return {
        "contributions": recent_commits,
        "repositories": int(user.get("public_repos") or len(non_forks)),
        "stars": int(stars),
        "followers": int(user.get("followers") or 0),
        "languages": languages,
        "source": "GITHUB REST API",
    }


def _rank_languages(
    weights: Dict[str, int], api_colors: Dict[str, str]
) -> List[Tuple[str, float, str]]:
    if not weights:
        return list(DEFAULT_OFFLINE_DATA["languages"])
    total = sum(v for v in weights.values() if v > 0)
    if total <= 0:
        return list(DEFAULT_OFFLINE_DATA["languages"])
    top = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)[:5]
    top_sum = sum(v for _, v in top)
    ranked: List[Tuple[str, float, str]] = []
    for idx, (name, val) in enumerate(top):
        pct = round((val / top_sum) * 100.0, 1)
        color = LANG_COLORS.get(name) or api_colors.get(name) or FALLBACK_PALETTE[idx % len(FALLBACK_PALETTE)]
        ranked.append((name, pct, color))
    # Normalize percentages so rounding sums cleanly to 100.0
    if ranked:
        diff = round(100.0 - sum(p for _, p, _ in ranked), 1)
        if abs(diff) <= 0.5:
            n0, p0, c0 = ranked[0]
            ranked[0] = (n0, round(max(0.1, p0 + diff), 1), c0)
    return ranked


def format_stat(value: Optional[int]) -> str:
    if value is None:
        return "SYNC"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:,}"


def truncate_label(text: str, max_len: int = 13) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def render_telemetry_svg(
    username: str,
    stats: Dict,
    synced_utc: Optional[str] = None,
) -> str:
    if synced_utc is None:
        synced_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    safe_user = html.escape(username)
    safe_source = html.escape(str(stats.get("source") or "GITHUB API"))
    safe_sync = html.escape(synced_utc)

    contrib_val = stats.get("contributions")
    contrib_sub = (
        "12-mo graph"
        if (contrib_val is not None and safe_source == "GITHUB GRAPHQL API")
        else ("recent events" if contrib_val is not None else "pending action")
    )

    tiles = [
        ("CONTRIBUTIONS", format_stat(contrib_val), contrib_sub, "#FFB454"),
        ("REPOSITORIES", format_stat(stats.get("repositories", 0)), "public builds", "#6FE3FF"),
        ("STARS EARNED", format_stat(stats.get("stars", 0)), "community signal", "#9A86FF"),
        ("FOLLOWERS", format_stat(stats.get("followers", 0)), "voyager network", "#3DDC84"),
    ]

    languages: List[Tuple[str, float, str]] = stats.get("languages") or list(
        DEFAULT_OFFLINE_DATA["languages"]
    )
    languages = languages[:5]

    # Deterministic subtle deep-space stars
    stars_coords = [
        (78, 16, 0.8, 3.4, -0.5), (192, 34, 0.6, 4.8, -1.2), (318, 14, 1.0, 3.9, -2.1),
        (456, 26, 0.7, 5.2, -0.8), (588, 18, 0.9, 4.1, -2.8), (712, 31, 0.7, 3.6, -1.7),
        (838, 17, 1.0, 4.6, -0.3), (124, 148, 0.7, 5.0, -2.2), (274, 152, 0.9, 3.7, -1.1),
        (498, 149, 0.6, 4.4, -3.0), (662, 154, 0.8, 3.8, -1.9), (796, 147, 1.0, 5.1, -0.6),
        (44, 198, 0.6, 4.2, -2.4), (848, 204, 0.8, 3.5, -1.4),
    ]
    star_svg = "\n".join(
        f'<circle cx="{sx}" cy="{sy}" r="{sr}" fill="#cfe0ff" opacity=".35">'
        f'<animate attributeName="opacity" values=".12;.75;.12" dur="{sd}s" begin="{sb}s" repeatCount="indefinite"/>'
        f"</circle>"
        for sx, sy, sr, sd, sb in stars_coords
    )

    tile_parts: List[str] = []
    tile_w = 197.5
    tile_gap = 14.0
    tile_x0 = 24.0
    for idx, (label, val_str, sub, accent) in enumerate(tiles):
        tx = tile_x0 + idx * (tile_w + tile_gap)
        delay = round(idx * 0.45, 2)
        tile_parts.append(
            f'<g transform="translate({tx:.1f},60)">'
            f'<rect width="{tile_w:.1f}" height="74" rx="10" fill="#0B1022" fill-opacity=".92" stroke="{accent}" stroke-opacity=".26" stroke-width="1"/>'
            f'<rect x="0.5" y="14" width="3" height="46" rx="1.5" fill="{accent}"/>'
            f'<circle cx="{tile_w - 14:.1f}" cy="16" r="2.6" fill="{accent}" opacity=".75">'
            f'<animate attributeName="opacity" values=".3;1;.3" dur="2.6s" begin="{delay}s" repeatCount="indefinite"/>'
            f"</circle>"
            f'<text x="15" y="21" font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,\'Liberation Mono\',monospace" font-size="9.5" letter-spacing="1.3" fill="#7D91C9">{html.escape(label)}</text>'
            f'<text x="15" y="50" font-family="\'Segoe UI\',system-ui,-apple-system,Helvetica,Arial,sans-serif" font-size="25" font-weight="700" fill="#F3F6FF">{html.escape(val_str)}</text>'
            f'<text x="15" y="65" font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,\'Liberation Mono\',monospace" font-size="9.5" fill="#5B6FA8">{html.escape(sub)}</text>'
            f"</g>"
        )

    # Top-5 languages segmented bar (guaranteed contiguous, exact fit to bar_w)
    bar_x = 24.0
    bar_y = 169.0
    bar_w = 832.0
    bar_h = 10.0

    bar_segments: List[str] = []
    legend_items: List[str] = []
    cursor_x = bar_x
    count = max(1, len(languages))
    slot_w = bar_w / count
    min_w = min(4.0, bar_w / count)
    remaining_w = max(0.0, bar_w - min_w * count)
    weights_pct = [max(0.0, float(p)) for _, p, _ in languages]
    weight_sum = sum(weights_pct) or float(count)

    for idx, (lang_name, pct, color) in enumerate(languages):
        if idx == count - 1:
            seg_w = max(0.0, (bar_x + bar_w) - cursor_x)
        else:
            w_share = (weights_pct[idx] / weight_sum) if sum(weights_pct) > 0 else (1.0 / count)
            seg_w = min_w + remaining_w * w_share
        bar_segments.append(
            f'<rect x="{cursor_x:.2f}" y="{bar_y:.1f}" width="{seg_w:.2f}" height="{bar_h:.1f}" fill="{html.escape(color)}"/>'
        )
        cursor_x += seg_w

        lx = bar_x + idx * slot_w
        short_name = html.escape(truncate_label(lang_name, 13))
        legend_items.append(
            f'<g transform="translate({lx:.1f},198)">'
            f'<circle cx="4.5" cy="-3.5" r="4" fill="{html.escape(color)}"/>'
            f'<text x="14" y="0" font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,\'Liberation Mono\',monospace" font-size="11" fill="#D7E2FF">'
            f'{short_name} <tspan fill="#7D91C9" font-size="10.5">{pct:.1f}%</tspan></text>'
            f"</g>"
        )

    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 880 220" width="880" height="220" role="img" aria-label="Voyager OS 01 live GitHub telemetry for {safe_user}">
<defs>
  <radialGradient id="tg1" cx="12%" cy="18%" r="55%">
    <stop offset="0" stop-color="#5a3cff" stop-opacity=".22"/>
    <stop offset="1" stop-color="#5a3cff" stop-opacity="0"/>
  </radialGradient>
  <radialGradient id="tg2" cx="90%" cy="88%" r="50%">
    <stop offset="0" stop-color="#00c8ff" stop-opacity=".16"/>
    <stop offset="1" stop-color="#00c8ff" stop-opacity="0"/>
  </radialGradient>
  <linearGradient id="sheen" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0" stop-color="#ffffff" stop-opacity="0"/>
    <stop offset=".5" stop-color="#ffffff" stop-opacity=".42"/>
    <stop offset="1" stop-color="#ffffff" stop-opacity="0"/>
  </linearGradient>
  <clipPath id="lang-clip">
    <rect x="{bar_x:.1f}" y="{bar_y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" rx="5"/>
  </clipPath>
  <clipPath id="lang-reveal">
    <rect x="{bar_x:.1f}" y="{bar_y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}">
      <animate attributeName="width" from="0" to="{bar_w:.1f}" dur="0.9s" fill="freeze"/>
    </rect>
  </clipPath>
</defs>

<rect width="880" height="220" rx="18" fill="#05060A"/>
<rect width="880" height="220" rx="18" fill="url(#tg1)"/>
<rect width="880" height="220" rx="18" fill="url(#tg2)"/>
<rect x="0.75" y="0.75" width="878.5" height="218.5" rx="17.25" fill="none" stroke="#243773" stroke-opacity=".55" stroke-width="1.2"/>

{star_svg}

<!-- Header & Pulsing Radar -->
<g transform="translate(34,27)">
  <circle r="10" fill="none" stroke="#6FE3FF" stroke-opacity=".35" stroke-width="1">
    <animate attributeName="r" values="4;12;12" keyTimes="0;0.75;1" dur="2.8s" repeatCount="indefinite"/>
    <animate attributeName="stroke-opacity" values=".75;0;0" keyTimes="0;0.75;1" dur="2.8s" repeatCount="indefinite"/>
  </circle>
  <circle r="6.5" fill="none" stroke="#6FE3FF" stroke-opacity=".28" stroke-width="0.9"/>
  <circle r="2.8" fill="#6FE3FF"/>
</g>
<text x="54" y="31" font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace" font-size="11.5" font-weight="700" letter-spacing="1.9" fill="#6FE3FF">VOYAGER OS 01 // TELEMETRY</text>
<text x="294" y="31" font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace" font-size="11" fill="#7D91C9">@{safe_user}</text>
<text x="856" y="31" text-anchor="end" font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace" font-size="10" fill="#5B6FA8">SYNC {safe_sync}</text>
<line x1="24" y1="46" x2="856" y2="46" stroke="#243773" stroke-opacity=".6" stroke-width="1"/>

<!-- Stat Tiles -->
{"".join(tile_parts)}

<!-- Top Languages Bar -->
<text x="24" y="160" font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace" font-size="10" letter-spacing="1.4" fill="#7D91C9">TOP LANGUAGES</text>
<text x="856" y="160" text-anchor="end" font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace" font-size="9.5" letter-spacing="1.0" fill="#5B6FA8">{safe_source}</text>

<rect x="{bar_x:.1f}" y="{bar_y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" rx="5" fill="#101730"/>
<g clip-path="url(#lang-clip)">
  <g clip-path="url(#lang-reveal)">
    {"".join(bar_segments)}
  </g>
  <rect x="-140" y="{bar_y:.1f}" width="140" height="{bar_h:.1f}" fill="url(#sheen)">
    <animate attributeName="x" values="-140;900;900" keyTimes="0;0.45;1" dur="6s" repeatCount="indefinite"/>
  </rect>
</g>

<!-- Language Legend -->
{"".join(legend_items)}
</svg>
'''


def collect_stats(username: str, offline: bool = False) -> Dict:
    if offline:
        return dict(DEFAULT_OFFLINE_DATA)

    net_errors = (
        urllib.error.URLError,
        urllib.error.HTTPError,
        http.client.HTTPException,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    )

    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        try:
            gql = fetch_graphql(username, token)
            if gql is not None:
                return gql
        except net_errors as exc:
            print(f"[telemetry] GraphQL fetch failed ({exc}); falling back to REST.", file=sys.stderr)

    try:
        rest = fetch_rest(username, token=token or None)
        if rest is not None:
            return rest
    except net_errors as exc:
        print(f"[telemetry] REST fetch failed ({exc}); using offline baseline.", file=sys.stderr)

    return dict(DEFAULT_OFFLINE_DATA)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Voyager OS 01 animated GitHub telemetry SVG.")
    parser.add_argument("--username", default="sGokulaKumaran", help="GitHub username")
    parser.add_argument("--out", required=True, help="Output SVG file path")
    parser.add_argument("--offline", action="store_true", help="Use deterministic baseline stats without network calls")
    args = parser.parse_args()

    stats = collect_stats(args.username, offline=args.offline)
    svg = render_telemetry_svg(args.username, stats)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(svg, encoding="utf-8")
    print(f"[telemetry] Wrote {out_path} ({len(svg):,} bytes, source={stats.get('source')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
