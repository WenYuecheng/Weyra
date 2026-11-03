"""Offline final reporting only: preserve raw grades, overlay audited recoveries."""
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from benchmarks.final_campaign_report import paired  # noqa: E402

ROOT = PROJECT / '.weyra/final-20261019-v2'
def read(p):
    return json.loads(p.read_text())
def write(p, value):
    p.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

def main():
    report = read(ROOT / 'evidence.json')
    legacy = read(ROOT / 'legacy/results.json')
    new = read(ROOT / 'skill/results.json')
    original = read(PROJECT / '.weyra/finish-20261018/formal-v2/results.json')
    effective = {k:v for k,v in original.items() if k.startswith(('localization/', 'skill_test/'))}
    pre_recovery = dict(effective)
    progressive = []
    for entry in legacy['results'].values():
        row = entry.get('result')
        if row and entry['run_type'] in ('missing', 'recovery'):
            effective[entry['source_key']] = row
            if entry['run_type'] == 'missing':
                pre_recovery[entry['source_key']] = row
        elif row and entry['run_type'] == 'progressive':
            progressive.append(row)
    loc = [v for k,v in effective.items() if k.startswith('localization/')]
    old_skill = [v for k,v in effective.items() if k.startswith('skill_test/')]
    heldout = [v for k,v in new.items() if k.startswith('heldout/')]
    blind = {k:v for k,v in new.items() if k.startswith('blind/')}
    blind_recoveries = []
    for key, row in new.items():
        if key.startswith('blind_recovery/'):
            source = row['source_key']
            blind_recoveries.append({'original_key': source, 'recovery_key': key,
                                     'original_status': blind[source]['status'], 'recovered_status': row['status']})
            blind[source] = row
    report['blind_raw'] = report['blind']
    report['blind_effective'] = {}
    for family in ('evidence', 'multi_file', 'expression'):
        report['blind_effective'][family] = {}
        for baseline in ('old_no_feedback', 'old_direct_feedback'):
            rows = [v for v in blind.values() if v['family']==family and v['baseline']==baseline]
            report['blind_effective'][family][baseline] = {
                'planned':8, 'outcomes':dict(Counter(v.get('preferred',v['status']) for v in rows)),
                'order_consistent':sum(v.get('preferred') in ('old','candidate','tie') for v in rows)}
    report['blind_recoveries'] = blind_recoveries
    report['localization_metrics'] = {}
    for arm in ('old', 'new'):
        rows = [v for v in loc if v['variant']==arm]
        report['localization_metrics'][arm] = {k:sum(bool(v.get(k)) for v in rows) for k in
            ('resolution_success','wrong_target','correct_abstention','skill_loaded','followup_success')}
        report['localization_metrics'][arm].update(planned=48,
            actual_modifications=sum(bool(v.get('changed_skills')) for v in rows),
            wrong_modifications=sum(bool(v.get('changed_skills')) and bool(v.get('wrong_target')) for v in rows))
    report['legacy_skill_pairs'] = {f:paired([v for v in old_skill if v['cluster']==f], 'frozen','adapted',repeats=True)
                                     for f in ('evidence','files','expression')}
    report['progressive_pairs'] = {a:paired(progressive,a,'new_new',repeats=True) for a in ('old_old','old_new')}
    report['all_recorded_before_recovery'] = {prefix:dict(Counter(v['status'] for k,v in pre_recovery.items() if k.startswith(prefix+'/')))
                                               for prefix in ('localization','skill_test')}
    report['localization_before_recovery_pairs'] = paired([v for k,v in pre_recovery.items() if k.startswith('localization/')], 'old','new',repeats=True)
    report['runtime_accounting'] = {name: {'sum_task_seconds':sum(float(v.get('elapsed_seconds',0)) for v in rows), 'recorded_task_calls':sum(v.get('physical_requests',0) for v in rows)}
        for name,rows in [('localization',loc),('legacy_skill',old_skill),('progressive',progressive),('new_skill',heldout)]}
    report['runtime_accounting_note'] = 'Sum of task durations, not campaign wall time; task counters exclude or can nest auxiliary calls. Root ledger is authoritative for total cost/calls.'
    report['original_pre_recovery_statuses'] = {prefix:dict(Counter(v['status'] for k,v in original.items() if k.startswith(prefix+'/')))
                                               for prefix in ('localization','skill_test')}
    report['scope_amendment'] = read(ROOT/'scope-amendment.json')
    report['gate_special_status'] = 'cancelled_by_user_before_execution_0_of_9'
    report['maintenance'] = {p.stem:read(p) for p in (ROOT/'skill/maintenance').glob('*snapshot.json')}
    report['new_skill_hard_failures'] = {a:sum(v['grade'].get('hard_failures',0) for v in heldout if v['variant']==a)
                                       for a in ('old_no_feedback','old_direct_feedback','adapted_skill')}
    report['terminal_final_statuses'] = dict(Counter(v['status'] for v in loc+old_skill+progressive+heldout))
    report['complete_under_amended_scope'] = (legacy['completed_new_executions']==148
        and sum(report['final_slot_counts'].values())==294 and len(blind)==48
        and all(v['status'] in ('pass','fail') for v in loc+old_skill+progressive+heldout)
        and all(v['status'] in ('pass','fail') for v in blind.values()))
    report['raw_complete_flag_explanation'] = 'Frozen reporter requires cancelled nine controls and ignores one separately stored blind recovery; original complete=false retained.'
    report['cost']['increment_since_final_plan_cny'] = str(Decimal(report['cost']['total_cny'])-Decimal('14.8571202754308'))
    report['skill_stage_cost'] = read(ROOT/'skill-stage-settlement.json')
    report['source_drift'] = read(ROOT/'summary.json').get('source_drift',[])
    report['scoring_caveats'] = ['Raw host grades preserved without retuning: source-file paths may fail exact source-ID matching despite correct evidence.',
        'Name anchors can reject shortened salutations; host grade is a strict contract check, not general writing quality.',
        'All three candidate bodies equal initial skills: new cohort C-A variation is not an evolution effect.',
        'Wrong-target selection is not actual wrong modification; all localization runs published zero changes.']
    write(ROOT/'final-evidence.json',report)
    write(ROOT/'final-task-records.json',{'effective_legacy':effective,'progressive':progressive,
        'compression':read(PROJECT/'.weyra/finish-20261018/context-v2/results.json'),'heldout':heldout,'effective_blind':blind,'blind_recoveries':blind_recoveries})
    wiki=PROJECT/'wiki'
    write(wiki/'FINAL-EVIDENCE.json',report)
    lines=['# Weyra 最终技能评测与交付报告','', '结案日期：2026-10-20。按用户最后指示取消尚未启动的9个门禁故障注入候选；不追加实验、不重跑行为失败。',
        '', '## 完成情况','',
        '294个最终槽位全部形成终态：定位96、原技能48、压缩24、渐进加载54、新技能72。本轮新增220次最终执行全部完成，其中包括预先冻结的11次基础设施恢复；恢复前记录完整保留。48组产物盲评均完成，包括单列的一次中断恢复。',
        '', f"原100元共享账本累计 **{Decimal(report['cost']['total_cny']):.2f}元**，待结算预留0笔。本轮从14.8571元起新增{Decimal(report['cost']['increment_since_final_plan_cny']):.2f}元；技能、维护与裁判阶段新增{Decimal(report['skill_stage_cost']['increment_cny']):.2f}/20元。账本共有3740次物理请求记录（包含本轮前记录），71次用量不明按保守规则记账；不是平台发票。",
        '', '## 可展示的定位收益','',
        '| 指标 | 旧词法定位 | 当前定位 |','|---|---:|---:|',
        '| 正确目标或正确弃权 | 26/48 | 40/48 |',
        '| 错误目标选择 | 22/48 | 8/48 |',
        '| 歧义题正确弃权 | 0/8 | 8/8 |',
        '| 实际加载过技能 | 41/48 | 43/48 |',
        '| 后续文件任务成功 | 48/48 | 48/48 |',
        '| 实际发布修改／错误发布 | 0次／0次 | 0次／0次 |','',
        '24道独立场景、每组重复两次；同题配对提升29.17个百分点，14胜、0负、34平。按独立题目聚类重采样的95%区间为[12.5, 50.0]个百分点。提升主要来自别名关联与歧义弃权；无合适技能的8次仍全部选错，应列为明确边界。这里证明定位机制收益，不能声称后续任务或持久更新因此改善。',
        '', '## 旧技能维护对照（独立批次）','', '| 任务族 | 冻结技能 | 更新技能 |','|---|---:|---:|',
        '| 证据引用 | 4/8 | 8/8 |','| 文件处理 | 4/8 | 5/8 |','| 表达与例外 | 0/8 | 8/8 |','',
        '每族4道独立题、每组重复两次。以上为明确旧规则纠正的受控任务，包含7次基础设施恢复后的有效结果，不能作为通用写作质量或独立模型门禁效果。逐族配对区间及原始异常分布见机器证据。',
        '', '## 渐进加载（原协议补齐）','',
        '| 路径 | 任务通过 | 实际加载 |','|---|---:|---:|',
        '| 旧技能＋旧定位 | 14/18 | 18/18 |','| 旧技能＋新定位 | 18/18 | 18/18 |','| 实际发布技能＋新定位 | 17/18 | 18/18 |','',
        '六族×三重复×三路径，读取旧维护目录实际技能，未重新维护或人工补丁。仅单技能目录，不能解释为大技能库检索、真实重复创建率或长期稳定性。',
        '', '## 新24题三组对照：未证明持久更新收益','',
        '| 严格宿主检查 | A原技能 | B直接反馈 | C持久版本 |','|---|---:|---:|---:|',
        '| 证据引用 | 3/8 | 3/8 | 4/8 |','| 多文件处理 | 8/8 | 8/8 | 8/8 |','| 表达与例外 | 6/8 | 7/8 | 5/8 |','',
        '三族均未发布新技能，C实际仍为旧版本。组间波动不能归因于自进化；文件族已处于天花板。三个组均24/24实际加载技能、读取输入并写产物，宿主硬副作用失败为0。严格检查包含引用标识和姓名锚点：正确来源文件路径可能未匹配精确来源ID，简写称呼也可能未过全名检查。原分数不修改，不能把它解释为开放文本质量。',
        '', '### 交换顺序的独立模型盲评','',
        '裁判读取任务、标准及实际产物，隐藏组别与维护理由；每对交换顺序两次，冲突记不确定。下面的胜负以C为主体，非任务成功率。',
        '', '| 任务族 | 对照 | C胜 | 平 | C负 | 不确定 |','|---|---|---:|---:|---:|---:|']
    for family,groups in report['blind_effective'].items():
        for base,data in groups.items():
            c=data['outcomes']
            lines.append(f"| {family} | {'A' if base=='old_no_feedback' else 'B'} | {c.get('candidate',0)} | {c.get('tie',0)} | {c.get('old',0)} | {8-data['order_consistent']} |")
    consistent=sum(v['order_consistent'] for g in report['blind_effective'].values() for v in g.values())
    lines += ['',f'顺序一致率{consistent}/48；未观察到C的盲评优势。模型只是质量代理，不覆盖宿主硬失败，也不构成门禁优于其他方法的消融。',
        '', '## 工程机制与压缩边界','',
        '实现反馈轮次关联、显式技能ID、别名和歧义弃权；有界候选经隔离的新旧任务执行、宿主硬检查、独立模型顺序交换盲评，再由严格收益门禁原子发布。默认关闭自进化，开启必须提供明确裁判路由和宿主验证清单；缺失报错，平局、错误和预算不足保留旧版本。现有回调也不能绕过模型评审。保留资源漂移检查和版本回滚；门禁模式不支持创建新技能。',
        '', '压缩共享8次物理辅助请求上限、连续失败熔断、失败保留原投影；DSH默认，三层摘要实验启用。原24次对照修复前后各12/12成功、关键事实均48/48；调用50→52、费用1.12135→1.18680元，未证明质量或费用收益。未执行64k长历史或跨厂商验证。',
        '', '工程验证沿用本轮已完成记录：474项测试及5项子测试通过、全库静态检查通过、构建和独立安装完成。按用户最新要求不追加防御性测试，9个专项候选取消，不能报告错误接受率或错误拒绝率。源码冻结后另一个工作流修改了4个入口/展示相关文件；正式运行与交付轮子使用冻结副本，不把当前工作区与冻结包误称为完全相同。',
        '', '## 证据与复跑','',
        '- `wiki/FINAL-EVIDENCE.json`：指标、配对区间、恢复清单、费用与边界。',
        '- `.weyra/final-20261019-v2/final-task-records.json`：有效逐题记录、产物内容及盲评证据；原记录在同目录skill/、legacy/中保留。',
        '- `.weyra/final-20261019-v2/protocol.json`、`frozen-source/`：源码、任务、路由、价格及参数指纹。模型名为服务端点声明，未做原厂身份认证。',
        '- `.weyra/final-20261019-v2/skill/maintenance/`：实际保留技能版本、候选拒绝原因和维护耗时记录。',
        '- `.weyra/final-20261019-v2/legacy/protocol.json`：83缺项、11恢复及54加载的冻结清单。',
        '- `.weyra/final-20261019-v2/artifacts/frozen-package/`：冻结构建产物。',
        '', '离线重建最终汇总：`python scripts/finalize_skill_evidence.py`，不调用模型。原始冻结报告的complete=false保留：它仍要求已取消的9项专项，且未读取独立盲评恢复；最终汇总使用complete_under_amended_scope，未修改任何逐题评分。',
        '', '付费复跑必须先核对原100元账本，使用冻结源码和同一协议；不要为覆盖成绩而重放已完成槽位。原822矩阵其余缺项历史结案，各批次不拼接一个总成功率。当前结论不支持学术首创、工业级、通用自主学习提升或完整Shell沙箱等表述。']
    (wiki/'FINAL-REPORT.md').write_text('\n'.join(lines)+'\n')
    (wiki/'FINAL-RESUME.md').write_text('''# Weyra 简历口径（最终复核）

**Weyra：反馈驱动的持久技能维护 Agent 框架｜Python**

- 实现技能使用轮次关联、显式ID、别名定位与歧义弃权；在24道独立场景、两次重复的正常Agent对照中，目标正确／合理弃权由26/48提升至40/48（54.2%→83.3%，+29.2个百分点），配对14胜0负，任务聚类95%区间[12.5,50.0]个百分点。
- 实现有界技能补丁、隔离行为验证、独立模型交换顺序盲评、严格收益门禁与原子发布；提供配置启用、预算控制、资源漂移检查及版本回滚。定位收益不等于任务执行收益，日常门禁新三族未发布候选，不宣称普遍自主学习改善。
- 建立冻结协议、宿主隐藏评分、真实文件产物与费用审计；完成294个最终槽位及48组双顺序盲评，保留11次基础设施恢复原始记录。工程验证474项测试及5项子测试通过。

可选面试补充：旧规则纠正小样本中，证据引用4/8→8/8、表达0/8→8/8；每族仅4道独立题×2重复，属于受控偏好纠正，不能替换为通用写作质量提升。新的24题三组实验未证明持久更新收益；压缩对照也未证明质量或成本收益。

创新表述使用“反馈驱动、独立模型评审与行为验证结合的持久技能维护工程机制”；不声称学术首创。完整证据见FINAL-REPORT.md和FINAL-EVIDENCE.json。
''')
    print(json.dumps({'complete_under_amended_scope':report['complete_under_amended_scope'],
          'slots':report['final_slot_counts'],'cost':report['cost'],'blind_consistent':consistent},ensure_ascii=False))

if __name__=='__main__':
    main()
