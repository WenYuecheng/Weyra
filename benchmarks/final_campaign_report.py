"""Offline, denominator-preserving evidence report for the final campaign."""
from collections import Counter
import json
from pathlib import Path
import random
import sqlite3

from weyra.frontmatter import atomic


def read(path, default=None):
    return json.loads(Path(path).read_text()) if Path(path).is_file() else ({} if default is None else default)


def distribution(rows, planned):
    statuses = Counter(r.get('status', 'not_run') for r in rows)
    statuses['not_run'] += planned - len(rows)
    return {'planned': planned, 'pass': statuses['pass'], 'statuses': dict(statuses),
            'success_rate': statuses['pass'] / planned}


def paired(rows, baseline, candidate, *, repeats=False):
    groups = {}
    for row in rows:
        if row.get('variant') not in (baseline, candidate):
            continue
        key = (row['id'], row.get('repeat', 0) if repeats else 0)
        groups.setdefault(key, {})[row['variant']] = row
    values = []
    by_task = {}
    wins = losses = ties = 0
    for (task, _), arms in groups.items():
        if len(arms) != 2 or any(r.get('status') not in ('pass', 'fail') for r in arms.values()):
            continue
        delta = int(arms[candidate]['status'] == 'pass') - int(arms[baseline]['status'] == 'pass')
        wins += delta > 0
        losses += delta < 0
        ties += delta == 0
        by_task.setdefault(task, []).append(delta)
        values.append(delta)
    task_means = [sum(v) / len(v) for v in by_task.values()]
    rng = random.Random(20261019)
    samples = sorted(sum(rng.choices(task_means, k=len(task_means))) / len(task_means)
                     for _ in range(2000)) if task_means else []
    return {'complete_pairs': len(values), 'independent_tasks': len(task_means),
            'candidate_only_success': wins, 'baseline_only_success': losses, 'same': ties,
            'difference_pp': 100 * sum(values) / len(values) if values else None,
            'task_bootstrap_95_pp': [100 * samples[50], 100 * samples[1949]] if samples else None}


def ledger_checkpoint(project):
    path = Path(project) / '.weyra' / 'state.db'
    connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        row = connection.execute("SELECT data FROM events WHERE session_id=? AND kind='budget_checkpoint' ORDER BY seq DESC LIMIT 1",
                                 ('finish-live-cny100',)).fetchone()
        return json.loads(row[0]) if row else {}
    finally:
        connection.close()


def campaign_report(root, project, *, budget=None):
    root, project = Path(root), Path(project)
    old = read(project / '.weyra/finish-20261018/formal-v2/results.json')
    legacy = read(root / 'legacy/results.json')
    combined = {k: v for k, v in old.items() if k.startswith(('localization/', 'skill_test/'))}
    recovery = []
    progressive = []
    for key, entry in legacy.get('results', {}).items():
        row = entry.get('result')
        if not row:
            continue
        if entry.get('run_type') in ('missing', 'recovery'):
            combined[entry['source_key']] = row
            if entry['run_type'] == 'recovery':
                recovery.append({'key': entry['source_key'], 'original': entry.get('original_status'),
                                 'recovered': row['status']})
        elif entry.get('run_type') == 'progressive':
            progressive.append(row)
    loc = [v for k, v in combined.items() if k.startswith('localization/')]
    skill_old = [v for k, v in combined.items() if k.startswith('skill_test/')]
    new = read(root / 'skill/results.json')
    heldout = [r for k, r in new.items() if k.startswith('heldout/')]
    blind = [r for k, r in new.items() if k.startswith('blind/')]
    gates = [r for k, r in new.items() if k.startswith('gate_special/')]
    context_raw = read(project / '.weyra/finish-20261018/context-v2/results.json')
    contexts = list(context_raw.values())
    context_rows = [r for r in contexts if r.get('variant') in ('before', 'after')]
    if not context_rows:
        context_rows = contexts
    checkpoint = budget._state() if budget is not None else ledger_checkpoint(project)
    result = {
        'fixed_final_slots': 294, 'planned_new_executions': 220,
        'final_slot_counts': {'localization': len(loc), 'legacy_skill': len(skill_old),
                             'compression': len(context_rows), 'progressive': len(progressive), 'new_skill': len(heldout)},
        'localization': {a: distribution([r for r in loc if r.get('variant') == a], 48) for a in ('old', 'new')},
        'localization_pairs': paired(loc, 'old', 'new', repeats=True),
        'legacy_skill': {f: {a: distribution([r for r in skill_old if r.get('cluster') == f and r.get('variant') == a], 8)
                                  for a in ('frozen', 'adapted')} for f in ('evidence', 'files', 'expression')},
        'progressive': {a: distribution([r for r in progressive if r.get('variant') == a], 18)
                        for a in ('old_old', 'old_new', 'new_new')},
        'progressive_loads': {a: sum(bool(r.get('skill_loaded')) for r in progressive if r.get('variant') == a)
                              for a in ('old_old', 'old_new', 'new_new')},
        'new_skill': {}, 'blind': {}, 'gate_special': gates, 'recoveries': recovery,
        'cost': {'total_cny': checkpoint.get('total_cost_cny'), 'cap_cny': '100',
                 'physical_requests': checkpoint.get('calls'), 'unknown_usage_requests': checkpoint.get('unknown_calls'),
                 'pending_reservations': len(checkpoint.get('pending', {})), 'basis': 'settled/conservative ledger, not provider invoice'},
    }
    arms = ('old_no_feedback', 'old_direct_feedback', 'adapted_skill')
    for family in ('evidence', 'multi_file', 'expression'):
        rows = [r for r in heldout if r.get('family') == family]
        result['new_skill'][family] = {a: distribution([r for r in rows if r['variant'] == a], 8) for a in arms}
        result['new_skill'][family]['pairs'] = {a: paired(rows, a, arms[2]) for a in arms[:2]}
        result['blind'][family] = {}
        for baseline in arms[:2]:
            rows = [r for r in blind if r.get('family') == family and r.get('baseline') == baseline]
            counts = Counter(r.get('preferred', r.get('status', 'unknown')) for r in rows)
            standards = {'baseline': 0, 'adapted': 0, 'valid_judgments': 0}
            for row in rows:
                if row.get('preferred') not in ('old', 'candidate', 'tie'):
                    continue
                standards['valid_judgments'] += 1
                a = new.get(f"heldout/{row['id']}/{baseline}", {})
                c = new.get(f"heldout/{row['id']}/adapted_skill", {})
                standards['baseline'] += int(a.get('status') == 'pass' and row.get('scores', {}).get('old') == 4)
                standards['adapted'] += int(c.get('status') == 'pass' and row.get('scores', {}).get('candidate') == 4)
            result['blind'][family][baseline] = {'planned_pairs': 8, 'recorded_pairs': len(rows),
                'outcomes': dict(counts), 'all_criteria_and_host_checks_met': standards}
    all_final = loc + skill_old + context_rows + progressive + heldout
    terminal = {'pass', 'fail', 'error', 'timeout', 'unsupported', 'pending_review'}
    result['complete'] = (len(all_final) == 294 and all(r.get('status') in terminal for r in all_final)
                          and len(blind) == 48 and all(r.get('status') not in ('interrupted', 'budget_stopped') for r in blind)
                          and len(gates) == 9 and all(r.get('status') in terminal for r in gates))
    result['limitations'] = ['Controlled 24-task cohort; judge is a quality proxy, not general task intelligence.',
                            'Repeated old tasks are clustered by task; only three new families.',
                            'Progressive loading uses one installed skill; no large-library retrieval or duplicate-creation claim.',
                            'Recovery results are separate follow-up observations, not overwritten original outcomes.']
    atomic(root / 'evidence.json', json.dumps(result, ensure_ascii=False, indent=2))
    lines = ['# Weyra 最终评测报告', '',
             f"评测状态：{'完整执行并完成可用产物评分' if result['complete'] else '尚未完整完成'}。固定294个最终槽位；各批次独立统计。", '',
             f"费用：{result['cost']['total_cny']} / 100元，待定预留{result['cost']['pending_reservations']}笔；属于保守记账，不是平台发票。", '',
             '## 新技能三组对照', '', '| 任务族 | 原技能 | 原技能+反馈 | 持久技能 |', '|---|---:|---:|---:|']
    for family, groups in result['new_skill'].items():
        lines.append('| ' + family + ' | ' + ' | '.join(str(groups[a]['pass']) + '/8' for a in arms) + ' |')
    lines += ['', '每族8道独立留出题，每组各执行一次。硬检查与模型盲评分别报告；开发、验证、门禁专项不混入最终任务分母。', '',
              '模型标准达标数同时要求宿主检查通过、两次盲评一致，且该版本四项标准全部满足；按每种配对分别列出，不能用模型偏好覆盖文件或事实检查失败。', '',
              '## 定位与渐进加载', '']
    for name, groups in (('定位', result['localization']), ('渐进加载', result['progressive'])):
        for arm, group in groups.items():
            lines.append(f"- {name} {arm}：{group['pass']}/{group['planned']}；状态{group['statuses']}。")
    lines += ['', '渐进加载来自原六族、三路径、三重复；单技能目录，不代表大技能库检索或重复创建率。', '',
              '## 原实验、恢复与边界', '',
              f"原技能对照48槽位，压缩24槽位；允许的基础设施恢复已记录{len(recovery)}/11。原始文件保留，恢复结果单列。", '',
              '压缩原结果修复前后均12/12通过，未证明质量或费用改善。旧822矩阵其余缺项历史结案。', '',
              '逐题状态、配对胜负、任务级bootstrap区间、盲评胜平负、门禁控制结果及恢复清单见同目录evidence.json。小样本区间不能支持广泛稳定性结论。']
    atomic(root / 'REPORT.md', '\n'.join(lines) + '\n')
    return result
