#!/usr/bin/env python3
"""Verify C9K diagnoses against actual GitHub Actions failure logs."""

import json
import os
import re
import subprocess
import urllib.request

ENGINE_URL = "http://127.0.0.1:8080"
REPO = os.environ.get("C9K_VERIFY_REPO", "prometheus/prometheus")

ENV = {**os.environ, "GH_PAGER": "cat"}

ERROR_RE = re.compile(
    r'--- FAIL:|FAIL\s+\S+|panic:|(?<!\w)Error:|error:|FAILED|'
    r'exit code [1-9]|exit status [1-9]|'
    r'connection refused|timeout|timed out|'
    r'permission denied|access denied|unauthorized|'
    r'AZURE_|azure.*error|oidc|federated|'
    r'not found|does not exist|'
    r'lint.*error|golangci|'
    r'build.*fail|compile.*error|'
    r'npm ERR|node.*error|'
    r'OOMKilled|out of memory|'
    r'rate limit|API rate',
    re.IGNORECASE
)
SKIP_RE = re.compile(r'pipefail|--noprofile|set -e|if \[', re.IGNORECASE)


def classify_actual(errors):
    text = ' '.join(errors[:20])
    if re.search(r'--- FAIL:|FAIL\s+Test|test.*fail', text, re.I):
        return 'test_failure'
    if re.search(r'lint|golangci', text, re.I):
        return 'lint'
    if re.search(r'build.*fail|compile|npm ERR', text, re.I):
        return 'build_failure'
    # Runner provisioning, before azure_auth to avoid false positives
    if re.search(r'Current runner version.*Runner Image Provisioner|Hosted Compute Agent', text, re.I):
        if not re.search(r'--- FAIL:|FAIL\s+Test|Error:|error:', text, re.I):
            return 'runner_failure'
    if re.search(r'AADSTS|federated identity|Login failed.*az|Login to Azure.*fail|azure.*credentials.*error', text, re.I):
        return 'azure_auth'
    if re.search(r'timeout|timed out|deadline', text, re.I):
        return 'timeout'
    if re.search(r'connection refused|ECONNREFUSED', text, re.I):
        return 'connection_refused'
    if re.search(r'permission denied|access denied|unauthorized', text, re.I):
        return 'permission'
    if re.search(r'not found|does not exist|404', text, re.I):
        return 'not_found'
    if re.search(r'rate limit', text, re.I):
        return 'rate_limit'
    if re.search(r'panic:', text, re.I):
        return 'panic'
    if not errors:
        return 'no_logs_available'
    return 'unknown'


def judge(predicted, actual_type):
    if actual_type in ('no_logs_available', 'unknown'):
        return '?'
    if predicted == 'flaky':
        if actual_type in ('test_failure', 'timeout', 'connection_refused', 'runner_failure'):
            return '~'
        if actual_type in ('azure_auth', 'build_failure', 'lint'):
            return '✗'
        return '~'
    if predicted == 'code_change':
        if actual_type in ('test_failure', 'build_failure', 'lint', 'panic'):
            return '✓'
        if actual_type in ('timeout', 'connection_refused', 'azure_auth', 'runner_failure'):
            return '✗'
        if actual_type == 'not_found':
            return '~'
        return '~'
    if predicted == 'infra':
        if actual_type in ('azure_auth', 'runner_failure', 'timeout', 'connection_refused'):
            return '✓'
        if actual_type in ('test_failure', 'lint', 'build_failure'):
            return '✗'
        return '~'
    return '?'


def main():
    data = json.loads(urllib.request.urlopen(
        f'{ENGINE_URL}/api/diagnosis/all', timeout=10).read())
    high = [d for d in data if d.get('confidence', 0) >= 0.5]

    graph = json.loads(urllib.request.urlopen(
        f'{ENGINE_URL}/api/graph/export', timeout=10).read())
    props = {n['id']: n.get('properties', {}) for n in graph.get('nodes', [])}

    print(f"Verifying {len(high)} diagnoses against actual logs...\n")

    # Deduplicate by run_id
    runs = {}
    for d in high:
        target = d['target_node']
        p = props.get(target, {})
        run_id = p.get('run_id')
        if run_id:
            if run_id not in runs:
                runs[run_id] = {'diagnoses': [], 'workflow': p.get('workflow', ''),
                                'domain': p.get('domain', '?')}
            runs[run_id]['diagnoses'].append(d)

    print(f"Unique runs: {len(runs)}\n")

    results = []
    for run_id, info in sorted(runs.items()):
        wf = info['workflow']
        domain = info['domain']

        r = subprocess.run(
            ['gh', 'run', 'view', str(run_id), '--repo', REPO, '--log-failed'],
            capture_output=True, text=True, timeout=60, env=ENV)

        log_lines = r.stdout.splitlines() if r.returncode == 0 else []
        errors = []
        for line in log_lines:
            if ERROR_RE.search(line) and not SKIP_RE.search(line):
                clean = re.sub(r'^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*', '', line).strip()
                clean = re.sub(r'^[^\t]+\t', '', clean).strip()
                if len(clean) > 10 and clean not in errors:
                    errors.append(clean[:200])

        actual_type = classify_actual(errors)

        for d in info['diagnoses']:
            target = d['target_node']
            rc = d['root_cause']
            conf = d['confidence']
            p = props.get(target, {})
            job = p.get('job', '')

            if 'commit://' in rc:
                rc_short = rc.split('/')[-1].split()[0]
                predicted = 'code_change'
            elif 'flaky' in rc:
                rc_short = 'flaky-tests'
                predicted = 'flaky'
            else:
                rc_short = rc
                predicted = 'infra'

            match = judge(predicted, actual_type)

            results.append({
                'run_id': run_id, 'job': job, 'wf': wf, 'domain': domain,
                'conf': conf, 'predicted': predicted, 'rc': rc_short,
                'actual_type': actual_type, 'match': match,
                'errors': errors[:5],
            })

    # Print table
    print(f"{'Match':5} {'Conf':5} {'Domain':10} {'Predicted':12} "
          f"{'Actual':20} {'Workflow/Job':50} {'Root Cause'}")
    print("-" * 150)
    for r in sorted(results, key=lambda x: (
            {'✓': 0, '~': 1, '?': 2, '✗': 3}.get(x['match'], 4), -x['conf'])):
        wj = f"{r['wf'][:25]}/{r['job'][:23]}"
        print(f"{r['match']:5} {r['conf']:.0%}   {r['domain']:10} "
              f"{r['predicted']:12} {r['actual_type']:20} {wj:50} {r['rc'][:30]}")

    # Summary
    correct = sum(1 for r in results if r['match'] == '✓')
    wrong = sum(1 for r in results if r['match'] == '✗')
    plausible = sum(1 for r in results if r['match'] == '~')
    unknown = sum(1 for r in results if r['match'] == '?')
    print(f"\n--- Accuracy Summary ---")
    print(f"✓ Correct:    {correct}/{len(results)} ({correct/len(results)*100:.0f}%)")
    print(f"~ Plausible:  {plausible}/{len(results)} ({plausible/len(results)*100:.0f}%)")
    print(f"✗ Wrong:      {wrong}/{len(results)} ({wrong/len(results)*100:.0f}%)")
    print(f"? Unknown:    {unknown}/{len(results)} ({unknown/len(results)*100:.0f}%)")

    # Actual failure type distribution
    from collections import Counter
    type_counts = Counter(r['actual_type'] for r in results)
    print(f"\n--- Actual Failure Types ---")
    for t, c in type_counts.most_common():
        print(f"  {c:3d}  {t}")

    # Sample errors by type
    print(f"\n--- Sample Errors by Type ---")
    seen_types = set()
    for r in results:
        t = r['actual_type']
        if t not in seen_types and r['errors']:
            seen_types.add(t)
            print(f"\n{t}:")
            for e in r['errors'][:3]:
                print(f"  {e[:140]}")

    # Misclassifications detail
    wrongs = [r for r in results if r['match'] == '✗']
    if wrongs:
        print(f"\n--- Misclassifications Detail ---")
        for r in wrongs:
            print(f"\n  Run {r['run_id']} [{r['wf']}/{r['job']}]")
            print(f"  Predicted: {r['predicted']} ({r['rc']})")
            print(f"  Actual:    {r['actual_type']}")
            for e in r['errors'][:3]:
                print(f"    > {e[:140]}")


if __name__ == '__main__':
    main()
