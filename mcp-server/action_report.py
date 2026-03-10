#!/usr/bin/env python3
"""
Generate a markdown diagnosis report for GitHub Actions job summary / PR comments.

Usage:
  python3 action_report.py --min-confidence 50 --repo owner/repo
"""

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone

ENGINE_URL = "http://127.0.0.1:8080"


def engine_get(path: str) -> dict | list:
    url = f"{ENGINE_URL}/api/{path}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-confidence", type=int, default=50)
    parser.add_argument("--repo", default="")
    args = parser.parse_args()

    min_conf = args.min_confidence / 100.0

    # Get health stats
    try:
        health = engine_get("health")
    except Exception:
        print("## ⚠️ Causinator 9000 — Engine Unavailable")
        print("The C9K engine could not be reached.")
        return

    mutations = health.get("active_mutations", 0)
    signals = health.get("active_signals", 0)

    if signals == 0:
        print("## ✅ Causinator 9000 — No Failures Detected")
        print(f"No CI failures found for `{args.repo}` in the lookback window.")
        return

    # Get alert groups
    groups = engine_get("alert-groups")
    groups = [g for g in groups if g.get("confidence", 0) >= min_conf]

    # Get all diagnoses
    diagnoses = engine_get("diagnosis/all")
    high = [d for d in diagnoses if d.get("confidence", 0) >= min_conf]

    print(f"## 🔍 Causinator 9000 — CI Failure Analysis")
    print()
    print(f"**{len(high)} failures** diagnosed above {args.min_confidence}% confidence "
          f"| {mutations} mutations | {signals} signals")
    print()

    if groups:
        print("### Alert Groups")
        print()
        print("| Root Cause | Confidence | Affected Jobs | Type |")
        print("|---|---|---|---|")
        for g in sorted(groups, key=lambda x: x.get("confidence", 0), reverse=True):
            rc = g.get("root_cause", "?")
            conf = g.get("confidence", 0)
            members = len(g.get("members", []))

            # Determine type from root cause string
            if "latent://runner-env" in rc:
                cause_type = "🖥️ Runner Environment"
            elif "latent://flaky" in rc:
                cause_type = "🎲 Flaky Test"
            elif "latent://github-scorecard" in rc:
                cause_type = "🔒 Security Scan"
            elif "latent://github-automerge" in rc:
                cause_type = "🔄 Automerge"
            elif "latent://" in rc:
                cause_type = "🏗️ Infrastructure"
            elif "commit://" in rc:
                cause_type = "💻 Code Change"
            else:
                cause_type = "❓ Unknown"

            # Clean up root cause display
            rc_display = rc
            if "commit://" in rc:
                parts = rc.split("/")
                sha = parts[-1].split()[0] if parts else "?"
                mutation = rc.split("(")[-1].rstrip(")") if "(" in rc else ""
                rc_display = f"`{sha}` {mutation}"
            elif "latent://" in rc:
                label = rc.split("//")[1].split()[0] if "//" in rc else rc
                rc_display = label

            print(f"| {rc_display} | {conf:.0%} | {members} | {cause_type} |")

        print()

    if high:
        print("<details>")
        print("<summary>📋 Detailed Diagnoses</summary>")
        print()
        print("| Confidence | Failed Job | Root Cause | Competing Causes |")
        print("|---|---|---|---|")
        for d in sorted(high, key=lambda x: x.get("confidence", 0), reverse=True)[:20]:
            target = d.get("target_node", "?")
            rc = d.get("root_cause", "?")
            conf = d.get("confidence", 0)
            competing = d.get("competing_causes", [])

            # Shorten target
            target_short = target
            if "job://" in target:
                parts = target.split("/")
                target_short = "/".join(parts[-2:]) if len(parts) > 2 else target

            # Shorten root cause
            rc_short = rc
            if "commit://" in rc:
                sha = rc.split("/")[-1].split()[0]
                rc_short = f"`{sha}`"
            elif "latent://" in rc:
                rc_short = rc.split("//")[1].split()[0]

            comp_str = ""
            if competing:
                comp_items = []
                for c, p in competing[:2]:
                    if "commit://" in c:
                        c_short = f"`{c.split('/')[-1].split()[0]}`"
                    elif "latent://" in c:
                        c_short = c.split("//")[1].split()[0]
                    else:
                        c_short = c[:20]
                    comp_items.append(f"{c_short} ({p:.0%})")
                comp_str = ", ".join(comp_items)

            print(f"| {conf:.0%} | {target_short} | {rc_short} | {comp_str} |")

        print()
        print("</details>")
        print()

    print("---")
    print(f"*Generated by [Causinator 9000](https://github.com/sylvainsf/causinator9000) "
          f"at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*")


if __name__ == "__main__":
    main()
