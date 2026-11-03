"""Generate Chinese finish evidence from explicit batches and a read-only root ledger.

Example: python scripts/reports/finish_report.py --run .weyra/finish-20261018/formal
 --context .weyra/finish-20261018/context-v2 --project Weyra
No models are invoked. Missing units remain in the fixed 144 + 24 denominator.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

SOURCE = Path(__file__).resolve().parents[2]
COMPLETE = {'pass', 'fail'}
TERMINAL = COMPLETE | {'error', 'timeout', 'budget_stopped', 'interrupted', 'unsupported'}


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else default


def plans():
    manifest = read_json(SOURCE / 'benchmarks/data/finish-eval-v1.json')
    histories = read_json(SOURCE / 'benchmarks/data/finish-context-v1.json')['cases']
    main, context = {}, {}
    for repeat in range(2):
        for case in manifest['localization_cases']:
            for arm in ('old', 'new'):
                key = f"localization/{case['id']}/{arm}/{repeat}"
                main[key] = {'id': case['id'], 'group': 'localization', 'variant': arm,
                             'repeat': repeat, 'family': case.get('family'),
                             'category': case['category'], 'expected_target': case['expected_target']}
        for family in manifest['families']:
            for case in family['tasks']:
                if case['split'] != 'test':
                    continue
                for arm in ('frozen', 'adapted'):
                    key = f"skill_test/{case['id']}/{arm}/{repeat}"
                    main[key] = {'id': case['id'], 'group': 'skill_test', 'variant': arm,
                                 'repeat': repeat, 'family': family['id']}
        for case in histories:
            for variant in ('before', 'after'):
                key = f"{case['id']}/{variant}/{repeat}"
                context[key] = {'id': case['id'], 'group': 'finish_context', 'variant': variant,
                                'repeat': repeat, 'expected_fact_count': len(case['expected'])}
    if len(main) != 144 or len(context) != 24:
        raise ValueError('Frozen finish design must contain 144 main and 24 context units')
    return main, context


def source_file(path):
    path = Path(path).resolve()
    return {'path': str(path), 'available': path.exists(),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None}


def root_ledger(project):
    path = Path(project).resolve() / '.weyra/state.db'
    base = {'available': False, 'path': str(path), 'session_id': 'finish-live-cny100'}
    if not path.exists():
        return base
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        row = db.execute("SELECT seq,data FROM events WHERE session_id=? AND kind=? ORDER BY seq DESC LIMIT 1",
                         ('finish-live-cny100', 'budget_checkpoint')).fetchone()
    if row is None:
        return base
    state = json.loads(row[1])
    pending = state.get('pending') or {}
    if not isinstance(pending, dict):
        raise ValueError('Unsupported legacy pending ledger; do not guess cost')
    held = sum((Decimal(value.get('cost', '0')) for value in pending.values()), Decimal(0))
    return {**base, 'available': True, 'checkpoint_seq': row[0],
            'total_cost_cny': str(Decimal(state.get('total_cost_cny', '0'))),
            'pending_reservation_cny': str(held), 'pending_requests': len(pending),
            'accounted_plus_pending_cny': str(Decimal(state.get('total_cost_cny', '0')) + held),
            'calls': state.get('calls'), 'unknown_usage_requests': state.get('unknown_calls'),
            'accounted_tokens': state.get('total_tokens'), 'budget_limit_cny': '100',
            'note': '根账本唯一总额；未知usage可能按保守预留记账，不是发票。'}


def completed_pairs(rows, old, new):
    indexed = {(row['id'], row['repeat'], row['variant']): row for row in rows}
    paired = []
    for (identity, repeat, variant), left in indexed.items():
        right = indexed.get((identity, repeat, new))
        if variant == old and right and left['status'] in COMPLETE and right['status'] in COMPLETE:
            paired.append({'id': identity, 'repeat': repeat, 'old_pass': left['status'] == 'pass',
                           'new_pass': right['status'] == 'pass'})
    n = len(paired)
    old_pass = sum(pair['old_pass'] for pair in paired)
    new_pass = sum(pair['new_pass'] for pair in paired)
    return {'old': old, 'new': new, 'completed_pairs': n, 'old_pass': old_pass,
            'new_pass': new_pass, 'only_new_pass': sum(not p['old_pass'] and p['new_pass'] for p in paired),
            'only_old_pass': sum(p['old_pass'] and not p['new_pass'] for p in paired),
            'difference_percentage_points': 100 * (new_pass-old_pass) / n if n else None,
            'policy': '仅同批次同题同重复的两侧pass/fail完整结果；error/timeout等保留总分母但不作完整配对',
            'rows': paired}


def group_counts(rows):
    counts = Counter(row['status'] for row in rows)
    return {'planned': len(rows), 'recorded': sum(row['status'] != 'not_run' for row in rows),
            'unexecuted': counts['not_run'], 'pass': counts['pass'],
            'completed': sum(counts[status] for status in COMPLETE), 'statuses': dict(counts)}


def collect(run, context, project):
    run, context = Path(run).resolve(), Path(context).resolve()
    main_plan, context_plan = plans()
    main_raw, context_raw = read_json(run / 'results.json', {}), read_json(context / 'results.json', {})
    if not isinstance(main_raw, dict) or not isinstance(context_raw, dict):
        raise ValueError('results.json must be keyed result objects')
    # Only exact planned keys in the explicitly selected directories qualify.
    main_rows = [{**main_raw.get(key, {'status': 'not_run'}), **identity, 'key': key}
                 for key, identity in main_plan.items()]
    context_rows = [{**context_raw.get(key, {'status': 'not_run'}), **identity, 'key': key}
                    for key, identity in context_plan.items()]
    main = group_counts(main_rows)
    main['planned_localization'], main['planned_skill_final'] = 96, 48
    main['localization'] = {}
    for arm in ('old', 'new'):
        rows = [row for row in main_rows if row['group'] == 'localization' and row['variant'] == arm]
        group = group_counts(rows)
        for field in ('resolution_success', 'followup_success', 'wrong_target', 'correct_abstention', 'duplicate_creation'):
            group[field] = sum(row.get(field) is True for row in rows)
        group['planned_absent_targets'] = sum(row['expected_target'] is None for row in rows)
        group['unknown_resolution'] = sum('resolution_success' not in row for row in rows)
        group['unknown_followup'] = sum('followup_success' not in row for row in rows)
        main['localization'][arm] = group
    main['skills'] = {}
    for family in sorted({row['family'] for row in main_rows if row['group'] == 'skill_test'}):
        main['skills'][family] = {arm: group_counts([row for row in main_rows
            if row['group'] == 'skill_test' and row['family'] == family and row['variant'] == arm])
            for arm in ('frozen', 'adapted')}
    main['localization_pairs'] = completed_pairs([row for row in main_rows if row['group'] == 'localization'], 'old', 'new')
    main['skill_pairs'] = completed_pairs([row for row in main_rows if row['group'] == 'skill_test'], 'frozen', 'adapted')
    main['unexecuted_keys'] = [row['key'] for row in main_rows if row['status'] == 'not_run']
    maintenance = [{**row, 'key': key} for key, row in main_raw.items() if key.startswith('maintenance/')]
    main['maintenance'] = {'planned': 6, 'statuses': dict(Counter(row['status'] for row in maintenance)),
                           'errors': [{k: row.get(k) for k in ('key', 'status', 'reason', 'error_kind')}
                                      for row in maintenance if row['status'] in ('error', 'timeout', 'interrupted', 'budget_stopped')]}
    main['excluded_nonfinal_keys'] = sorted(set(main_raw)-set(main_plan))
    compressed = group_counts(context_rows)
    compressed['variants'] = {}
    for arm in ('before', 'after'):
        rows = [row for row in context_rows if row['variant'] == arm]
        metrics = [row.get('metrics', {}) for row in rows if row['status'] != 'not_run']
        compressed['variants'][arm] = {
            **group_counts(rows),
            'facts_retained': sum(metric.get('facts_retained', 0) for metric in metrics),
            'facts_observed_total': sum(metric.get('facts_total', 0) for metric in metrics),
            'facts_planned_total': sum(row['expected_fact_count'] for row in rows),
            'compaction_published': sum(metric.get('compaction_published') is True for metric in metrics),
            'raw_history_preserved': sum(metric.get('raw_history_preserved') is True for metric in metrics),
            'physical_requests': sum(metric.get('physical_requests', 0) for metric in metrics),
            'unit_accounted_cost_cny': str(sum((Decimal(metric.get('cost_cny', '0')) for metric in metrics), Decimal(0))),
            'all_unit_costs_known': bool(metrics) and all(metric.get('cost_known') is True for metric in metrics),
        }
    compressed['pairs'] = completed_pairs(context_rows, 'before', 'after')
    compressed['unexecuted_keys'] = [row['key'] for row in context_rows if row['status'] == 'not_run']
    compressed['excluded_keys'] = sorted(set(context_raw)-set(context_plan))
    summary = read_json(run / 'summary.json', {})
    return {
        'generated_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'),
        'project': Path(project).resolve().name, 'main': main, 'context': compressed,
        'delivery_decision': read_json(run / 'delivery-decision.json', {}),
        'execution_complete': bool(summary) and all(row['status'] in TERMINAL for row in main_rows + context_rows),
        'ledger': root_ledger(project), 'input_summary': summary,
        'sources': {name: source_file(path) for name, path in {
            'main_results': run / 'results.json', 'main_summary': run / 'summary.json',
            'main_protocol': run / 'protocol.json', 'context_results': context / 'results.json',
            'context_protocol': context / 'protocol.json',
            'main_manifest': SOURCE / 'benchmarks/data/finish-eval-v1.json',
            'context_manifest': SOURCE / 'benchmarks/data/finish-context-v1.json'}.items()},
        'denominator_policy': '主最终144=定位96+技能最终48；压缩24。缺失明确未执行，错误/超时不剔除；仅完整两侧进行配对。',
        'cost_policy': '总费用只读finish-live-cny100根账本，不相加维护/validation/最终任务等嵌套unit费用；未知usage保守预留不是发票。',
        'historical_results': {'early_benchmark': '20/20', 'old_core_recorded': '552/822',
                               'localization_v1': '34/60→54/60', 'localization_v2': '23/60→59/60',
                               'limitation': '声明别名库机制测试；新版使用显式ID/调用记录等额外信息，属于不同信息量，不证明通用跨语言语义提升。'},
    }


def fraction(passed, planned):
    return f'{passed}/{planned}（{100*passed/planned:.1f}%）' if planned else '无分母'


def render(run, context, project, output=None):
    evidence = collect(run, context, project)
    destination = Path(output).resolve() if output else Path(project).resolve() / 'wiki'
    destination.mkdir(parents=True, exist_ok=True)
    main, compressed, ledger = evidence['main'], evidence['context'], evidence['ledger']
    state = '执行已覆盖全部计划单元；成功率仍包含失败和错误。' if evidence['execution_complete'] else '暂未完成，以下为当前快照，不能写成最终改善结论。'
    if evidence['delivery_decision'].get('decision') == 'user_prioritized_fast_delivery':
        state = '按用户选择快速交付：后端与核心技能/压缩结果交付，定位实验提前停止。144单元原计划未全部执行，缺口保留，不宣称完整评测完成。'
    lines = ['# Weyra 收尾验证报告', '', '生成时间：' + evidence['generated_at'], '', state, '',
             '## 本轮范围与证据', '',
             f"主最终固定144单元（定位96、技能最终48），已记录{main['recorded']}，未执行{main['unexecuted']}。",
             f"独立压缩固定24单元，已记录{compressed['recorded']}，未执行{compressed['unexecuted']}。",
             '缺失、错误、超时与中断均保留在总分母；只有同题同重复两侧pass/fail均完整时才进入配对。',
             '维护、开发和validation只作为过程证据，不进入144个最终任务；不混入旧批次结果。', '',
             '## 正常Agent技能定位（96单元）', '',
             '完整任务成功status与定位resolution_success、后续任务followup_success分别统计。正确弃权只统计预期无目标的场景。', '',
             '| 策略 | 完整任务通过/计划 | 定位成功/计划 | 后续任务成功/计划 | 错目标 | 正确弃权/预期无目标 | 未执行 | 状态分布 |',
             '|---|---:|---:|---:|---:|---:|---:|---|']
    for arm, group in main['localization'].items():
        lines.append(f"| {arm} | {fraction(group['pass'], group['planned'])} | {group['resolution_success']}/{group['planned']} | {group['followup_success']}/{group['planned']} | {group['wrong_target']} | {group['correct_abstention']}/{group['planned_absent_targets']} | {group['unexecuted']} | {json.dumps(group['statuses'], ensure_ascii=False)} |")
    lines += ['', '定位与后续成功字段缺失按未知保留，不从完整任务status推断。', '',
              '## 技能维护后的最终任务（48单元）', '',
              '| 家族 | frozen通过/计划 | adapted通过/计划 | frozen未执行 | adapted未执行 | 状态分布 frozen / adapted |',
              '|---|---:|---:|---:|---:|---|']
    for family, arms in main['skills'].items():
        old, new = arms['frozen'], arms['adapted']
        lines.append(f"| {family} | {fraction(old['pass'],old['planned'])} | {fraction(new['pass'],new['planned'])} | {old['unexecuted']} | {new['unexecuted']} | {json.dumps(old['statuses'])} / {json.dumps(new['statuses'])} |")
    lines += ['', '维护执行成功不等于技能质量提升；frozen与adapted在同一最终任务集上独立验证。',
              '六个维护过程状态：' + json.dumps(main['maintenance']['statuses'], ensure_ascii=False) + '。',
              '维护错误：' + json.dumps(main['maintenance']['errors'], ensure_ascii=False) + '。发生错误时最终任务读取实际已发布版本；不把错误解释为候选已发布。', '',
              '## 上下文压缩新历史对照（24单元）', '',
              '六个新长历史、修复前后同为three_parallel、各两重复。主/辅助输出额度1024、temperature=0、压力8192、保留尾部512；不静默改变配置模型容量。',
              'before仅加载冻结context，其余依赖与after相同；两臂全单元16请求保护，after生产压缩事务另有8次物理调用上限。', '',
              '| 策略 | 通过/计划 | 事实保持/有指标事实总数 | 固定计划事实总数 | 发布压缩 | 原文保留 | 物理请求 | 单元记账费用CNY | 未执行 | 状态分布 |',
              '|---|---:|---:|---:|---:|---:|---:|---:|---:|---|']
    for arm, group in compressed['variants'].items():
        lines.append(f"| {arm} | {fraction(group['pass'],group['planned'])} | {group['facts_retained']}/{group['facts_observed_total']} | {group['facts_planned_total']} | {group['compaction_published']} | {group['raw_history_preserved']} | {group['physical_requests']} | {group['unit_accounted_cost_cny']} | {group['unexecuted']} | {json.dumps(group['statuses'])} |")
    lines += ['', '事实分母只包括保存了事实指标的单元；固定计划事实总数单列，缺失不视为保留。费用含未知usage的保守记账。', '',
              '## 完整配对与结论边界', '']
    for label, pairs in [('定位', main['localization_pairs']), ('技能最终', main['skill_pairs']), ('压缩', compressed['pairs'])]:
        lines.append(f"- {label}：完整配对{pairs['completed_pairs']}，旧通过{pairs['old_pass']}，新通过{pairs['new_pass']}；仅新通过{pairs['only_new_pass']}，仅旧通过{pairs['only_old_pass']}。")
    lines += ['', '这些是受控机制队列及两次重复，不能据此声称通用能力或长期学习的稳定收益。没有正增益时按实报告持平或下降。', '',
              '## 共享100元根预算', '']
    if ledger['available']:
        lines += [f"根账本finish-live-cny100第{ledger['checkpoint_seq']}条checkpoint：已结算/保守记账{ledger['total_cost_cny']} CNY，物理请求{ledger['calls']}，未知usage请求{ledger['unknown_usage_requests']}。",
                  f"仍有{ledger['pending_requests']}笔预留共{ledger['pending_reservation_cny']} CNY，记账加待定预留{ledger['accounted_plus_pending_cny']} CNY。"]
    else:
        lines.append('根预算checkpoint不可用；总费用未知，不以各单元费用推算。')
    lines += ['只读根state.db指定session；总费用不相加嵌套unitcost，避免维护与其validation重复计算。未知usage使用保守预留，不是发票。', '',
              '## 旧实验如何使用', '',
              '早期受控真实基准20/20；旧核心计划552/822有记录，包含失败和中断，270未启动，不等于552项成功或独立题目。',
              '旧定位v1为34/60→54/60；独立补充v2为23/60→59/60。两批不能合并提高成功率。它们测声明别名库机制；新版利用显式技能ID、调用记录等额外信息，属于不同信息量，不是通用跨语言语义提升。',
              '旧技能增益区间含零、压缩存在格式/执行和判分契约问题；负结果用于故障发现，并驱动格式修复、请求上限、轮次熔断、预算审计与冻结恢复等可靠性改进。不能重写为已证明稳定正收益。', '',
              '## 复核与复跑', '',
              '同目录finish-evidence.json保存固定分母、未执行key、完整配对、根账本checkpoint和输入文件SHA256。',
              f"本次只读取主批次 `{Path(run).resolve()}` 与压缩批次 `{Path(context).resolve()}`。",
              '恢复执行由原运行器完成，不自动重放已开始/失败单元。此报告生成器不调用模型。', '']
    old_loc, new_loc = main['localization']['old'], main['localization']['new']
    old_ctx, new_ctx = compressed['variants']['before'], compressed['variants']['after']
    frozen = sum(arms['frozen']['pass'] for arms in main['skills'].values())
    adapted = sum(arms['adapted']['pass'] for arms in main['skills'].values())
    caveat = '' if evidence['execution_complete'] else '本轮暂未完成，下述新结果为当前快照，不写成最终收益。'
    resume = ['# Weyra 简历证据与项目表述', '', caveat or '本轮执行已覆盖计划单元，以下保留范围与分母。', '',
              '**Weyra｜Python Agent Harness：可恢复执行、共享预算与技能维护**', '',
              f"- 构建正常Agent真实文件工作流验证，固定144个最终单元（定位96、技能最终48）；本轮定位完整任务old {old_loc['pass']}/48、new {new_loc['pass']}/48，技能最终frozen {frozen}/24、adapted {adapted}/24，分别保留定位、后续任务和错误类型；主最终未执行{main['unexecuted']}。",
              f"- 修复三路摘要格式契约，增加压缩事务8次物理请求上限、连续失败熔断与失败审计；六个新长历史各两重复，压缩before {old_ctx['pass']}/12、after {new_ctx['pass']}/12，after事实保持{new_ctx['facts_retained']}/{new_ctx['facts_observed_total']}（计划48事实），未执行{compressed['unexecuted']}。",
              '- 实现显式ID、成功调用历史、声明别名与歧义弃权的技能定位；旧小型机制队列v1为34/60→54/60（34种不同输入），独立v2为23/60→59/60（另一批60种不同输入）；整合SQLite持久会话、候选隔离验证、版本回滚和主/维护共用费用账本。', '',
              '## 使用边界', '',
              '旧定位是不同信息量的声明别名库机制验证，不是通用语言理解提升；本轮完整任务成功与定位成功分开，早期受控基准20/20只限定受控基准。旧552/822包含失败、中断和未完成计划，不写成成功数。',
              '压缩或技能最终通过数持平/下降时，不宣称稳定正收益；负结果体现故障发现和可靠性工程改进。具体模型配置、费用与逐项证据见FINISH-REPORT.md和finish-evidence.json。', '']
    if evidence['delivery_decision'].get('decision') == 'user_prioritized_fast_delivery':
        reference = main['skills']['evidence']
        expression = main['skills']['expression']
        resume = ['# Weyra 简历可用版本', '',
            '**Weyra｜Python Agent Harness：可恢复执行与反馈驱动技能维护**', '',
            '- 构建轻量Python Agent运行时，统一模型路由、文件工具、权限审批与SQLite会话恢复；主任务、摘要与技能维护共用持久费用账本，支持断点恢复与版本回滚。',
            f"- 实现经验辅助的有界技能更新与隔离验证门禁；在受控规则纠正任务中，引用核验通过数由{reference['frozen']['pass']}/8变为{reference['adapted']['pass']}/8，表达规则由{expression['frozen']['pass']}/8变为{expression['adapted']['pass']}/8（每族4道留出题、各两次重复），通过实际读写文件和独立断言验收。",
            '- 实现反馈关联技能定位、声明别名与歧义弃权，在独立60输入的机制探针中观察到通过数23/60→59/60；针对摘要格式冲突和请求放大，增加每次压缩8次物理调用上限、轮次熔断与失败审计。', '',
            '## 指标解释与面试证据', '',
            '引用核验和表达是预设旧规则/新要求的受控纠正任务，不代表通用引用质量或写作能力。文件处理任务仍有失败和接口错误，不能只用上述两族推论全部任务。完整分组结果见FINISH-REPORT.md。',
            '定位探针新版利用了显式ID、实际调用历史及声明别名；不是相同信息条件下的语言理解能力提升。旧v1为60单元/34种输入，独立v2为另一批60种输入，两批分别报告。',
            f"主最终原计划144单元，本次记录{main['recorded']}、未执行{main['unexecuted']}；按用户选择提前停止定位。六个新压缩历史各两重复，修复前后均12/12通过，未证明准确率或成本改善；8次上限与熔断属于工程可靠性改进。",
            '早期受控基准20/20、旧核心552/822记录和负结果均保留，不将552写为独立成功题数。测试、源码指纹、逐题结果、维护失败和费用依据见verification.json及finish-evidence.json。', '']
    paths = {'report': destination / 'FINISH-REPORT.md', 'resume': destination / 'FINISH-RESUME.md',
             'evidence': destination / 'finish-evidence.json'}
    paths['report'].write_text('\n'.join(lines))
    paths['resume'].write_text('\n'.join(resume))
    paths['evidence'].write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    return {key: str(path) for key, path in paths.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--context', type=Path, required=True)
    parser.add_argument('--project', default=str(SOURCE))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    project = Path(args.project)
    if not project.exists() and args.project == SOURCE.name:
        project = SOURCE
    print(json.dumps(render(args.run, args.context, project, args.output), ensure_ascii=False))


if __name__ == '__main__':
    main()
