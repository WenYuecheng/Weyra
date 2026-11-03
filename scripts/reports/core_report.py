"""Render host-side core results without changing frozen experiments or old reports."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.reports.core_resources import collect  # noqa: E402
from zoneinfo import ZoneInfo


def render(root, destination):
    root=Path(root).resolve()
    destination=Path(destination).resolve()
    raw_results=(root/'results.json').read_bytes()
    raw_summary=(root/'summary.json').read_bytes()
    rows=json.loads(raw_results)
    summary=json.loads(raw_summary)
    if not summary.get('execution_closed') or summary.get('recorded_units')!=len(rows):
        raise ValueError('Report requires a closed execution and matching final summary')
    budget_path=destination.parent/'CORE-BUDGET-AUDIT.json'
    budget_audit=json.loads(budget_path.read_text()) if budget_path.exists() else {}
    final_budget=budget_audit.get('recorded_total_cny')==summary.get('cost_cny') and budget_audit.get('pending_requests')==0
    official={k:v for k,v in rows.items() if not k.startswith('pilot/')}
    from benchmarks.core_scoring import build_scoring_validity_audit
    raw_manifest=(root/'frozen-source/data/core-eval-v1.json').read_bytes()
    manifest=json.loads(raw_manifest)
    cases=manifest['long_history_cases']+manifest['public_cases']
    cases += [t for f in manifest['evolution_families'] for t in f['tasks']]
    provenance={'results_sha256':hashlib.sha256(raw_results).hexdigest(),
                'protocol_sha256':hashlib.sha256((root/'protocol.json').read_bytes()).hexdigest(),
                'execution_eligibility':'completed-pass-fail-pending-v1',
                'manifest_sha256':hashlib.sha256(raw_manifest).hexdigest(),
                'scoring_policy_sha256':hashlib.sha256(Path(__import__('benchmarks.core_scoring',fromlist=['x']).__file__).read_bytes()).hexdigest()}
    audit_id=hashlib.sha256(json.dumps(provenance,sort_keys=True).encode()).hexdigest()[:16]
    audit_path=destination.parent/f'core-scoring-validity-audit-{audit_id}.json'
    if audit_path.exists():
        audit=json.loads(audit_path.read_text())
        if audit.get('source')!=provenance:
            raise ValueError('Scoring validity audit provenance mismatch')
    else:
        audit=build_scoring_validity_audit(cases,[
            {'case_id':r['id'],'record_id':k,'answer':r.get('answer'), 'original_grade':r['grade']}
            for k,r in official.items() if 'grade' in r and r['status'] in ('pass','fail','pending_review')])
        audit['source']=provenance
        with audit_path.open('x') as handle:
            json.dump(audit,handle,ensure_ascii=False,indent=2)
    disputed=[r for r in audit['records'] if r['review_status']=='pending_review'
              and official[r['record_id']]['status']=='fail']
    resources=collect(root,rows=rows)
    (destination.parent/'core-resource-usage.json').write_text(json.dumps(resources,ensure_ascii=False,indent=2)+'\n')
    indexed={(r['group'],r['id'],r['repeat'],r['variant']):(k,r) for k,r in official.items()}
    paired_details=[]
    pools={'localization':manifest['localization_cases'],
           'compression':manifest['long_history_cases'],
           'skill_test':[t for f in manifest['evolution_families'] for t in f['tasks'] if t['split']=='test']}
    for comparison in summary.get('comparisons',[]):
        group=comparison['group']
        for case in pools.get(group,[]):
            if group=='compression' and comparison.get('trigger') and (
                comparison['trigger']!=case.get('trigger') or comparison.get('regime')!=case.get('context_regime')):
                continue
            for repeat in range(1 if group=='localization' else 3):
                sides={}
                for side,arm in [('old',comparison['old']),('new',comparison['new'])]:
                    key,row=indexed.get((group,case['id'],repeat,arm),(None,{}))
                    sides[side]={'record_id':key,'variant':arm,'status':row.get('status','unstarted'),
                                 'hard_failures':row.get('grade',{}).get('hard_failures'),
                                 'cost_cny':row.get('cost_cny'),
                                 'elapsed_seconds':row.get('elapsed_seconds'),
                                 'tokens':resources['units'].get(key)}
                old,new=sides['old'],sides['new']
                paired_details.append({'group':group,'id':case['id'],'repeat':repeat,
                    'trigger':comparison.get('trigger'),'regime':comparison.get('regime'),
                    'input_sha256':hashlib.sha256(json.dumps(case.get('input'),sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
                    **sides,'confirmed_success_difference':None if 'unstarted' in (old['status'],new['status']) else
                      int(new['status']=='pass')-int(old['status']=='pass'),
                    'hard_failure_difference':None if old['hard_failures'] is None or new['hard_failures'] is None else
                      new['hard_failures']-old['hard_failures']})
    (destination.parent/'core-paired-details.json').write_text(json.dumps({'policy':
        'All planned pairs retained, including unstarted sides. Confirmed success is distinct from semantic correctness. '
        'Localization repeated inputs remain flagged by input hash; inferential summary deduplicates them.',
        'rows':paired_details},ensure_ascii=False,indent=2)+'\n')
    groups=defaultdict(list)
    for row in official.values():
        groups[(row['group'],row['variant'])].append(row)
    planned={'localization':{'old':60,'new':60},
      'maintenance':{a:18 for a in ('no_experience_full','experience_full','no_experience_bounded','experience_bounded')},
      'skill_test':{a:54 for a in ('no_skill','frozen_old','no_experience_full','experience_full','no_experience_bounded','experience_bounded')},
      'progressive':{a:18 for a in ('old_old','old_new','new_new')},
      'compression':{a:36 for a in ('none','single_json','dsh_checkpoint','three_parallel','dsh_no_prune')},
      'public':{'none':36,'dsh_configured':36}}
    for g,variants in planned.items():
        for a in variants:
            groups.setdefault((g,a),[])
    lines=['# Weyra 核心评测报告 v1','',
       '生成时间：'+datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'),' ',
       '## 状态与证据边界','',
       f"正式计划822单元，已记录{len(official)}单元；另有{len(rows)-len(official)}个开发试跑单元。",
       '本批不混入此前试跑、预检和停止批次的成绩；它们另行留档，费用仍包含在同一根账本。',
       f"根账本保守费用：{summary.get('cost_cny','待最终汇总')}元；物理请求：{summary.get('physical_requests','待汇总')}；未知用量：{summary.get('unknown_usage_requests','待汇总')}。新增上限50元，实际平台账单未知。",'',
       (f"包含尚未入账的未知用量全文上界补差后，独立保守复核合计 **{budget_audit['adjusted_settled_upper_cny']}元**，无待返回请求。" if final_budget else
        '独立保守补差以最终预算审计为准；此处账面金额不能替代缺失用量的全文上界。')+
       ' [费用复核](CORE-BUDGET-AUDIT.json)。','',
       '本轮尚不能证明最初的长期自演化与长任务收益目标已经达成。工程可靠性改进已有回归证据；核心实验是否覆盖完整应以下列固定分母和未启动清单为准。', '',
       '本轮为受控机制实验与公开题能力检查。维护单元成功只表示执行完成；定位为解析器探针；结构化任务检查可观察规则和事实。未把这些数字当通用写作质量、论文复现成绩或多厂商稳定性。','',
       '## 固定分母结果','',
       '| 实验 | 变体 | 固定计划数 | 记录数 | 确认成功 | 失败 | 错误/超时 | 待复核 | 中断/预算停止 | 未启动 |',
       '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for (group,variant),records in sorted(groups.items()):
        c=Counter(r['status'] for r in records)
        lines.append(f"| {group} | {variant} | {planned.get(group,{}).get(variant,0)} | {len(records)} | {c['pass']} | {c['fail']} | {c['error']+c['timeout']} | {c['pending_review']} | {c['interrupted']+c['budget_stopped']} | {max(0,planned.get(group,{}).get(variant,0)-len(records))} |")
    lines+=['','以上为冻结评分器原始结果，未覆盖或追改。另有'+str(len(disputed))+
            '条原始fail因题面没有约定字符串词汇而被独立有效性审计标记待复核；它们不能作为已确认语义错误。审计只降格争议失败，不自动提升成功。',
            f'[评分有效性审计]({audit_path.name})。原始失败率和涉及这些题目的差值只供排查，不能证明记忆丢失或行为退化。','',
            '## 费用、用量与时间','',
            '| 实验/变体 | 记录单元费用上界/元 | 结算请求 | 有记录输入token | 有记录输出token | 未知用量记录 | 单元耗时合计/秒 |',
            '|---|---:|---:|---:|---:|---:|---:|']
    for (group,variant),records in sorted(groups.items()):
        usage=resources['groups'].get(group+'/'+variant,{})
        cost=sum((Decimal(r.get('cost_cny','0')) for r in records),Decimal(0))
        lines.append(f"| {group}/{variant} | {cost:.6f} | {sum(r.get('physical_requests',0) for r in records)} | {int(usage.get('input_tokens',0))} | {int(usage.get('output_tokens',0))} | {int(usage.get('unknown_usage_records',0))} | {sum(r.get('elapsed_seconds',0) for r in records):.1f} |")
    lines+=['','单元费用不含被中断后未形成结果的调用，不能相加替代根账本总额。token仅汇总实际轨迹，缺失用量不当零费用；耗时包含排队且并发重叠，不当总墙钟时间或公平延迟排名。',
            '[逐单元资源审计](core-resource-usage.json)。各批次试跑与失败费用均包含在根账本，旧版本预留偏低的未知用量补差另见最终预算审计。']
    lines+=['','## 技能定位','']
    for arm in ['old','new']:
        group=groups.get(('localization',arm),[])
        lines.append(f"- {arm}：正确 {sum(r['status']=='pass' for r in group)}/{len(group)}；错误目标 {sum(r.get('wrong_target',False) for r in group)}；正确弃权 {sum(r.get('correct_abstention',False) for r in group)}。")
    supplement_path=destination.parent/'core-localization-supplement-v2.json'
    if supplement_path.exists():
        supplement=json.loads(supplement_path.read_text())['counts']
        lines+=['',f"独立事后补充：另冻结{supplement['unique_inputs']}个不同输入，旧版{supplement['old_successes']}/{supplement['rows']}、新版{supplement['new_successes']}/{supplement['rows']}。全部错误保留，未据结果改题；该结果与v1分开，仍只是小型声明别名库中的功能路径对比。",
                '[补充逐题结果](core-localization-supplement-v2.json) · [范围说明](CORE-LOCALIZATION-SUPPLEMENT-V2.md)']
    lines+=['','重复创建未在解析器探针中实际执行，因此不填一个推断的“重复创建率”；生产路径的拒绝创建行为由回归测试单独验证。','',
            '## 技能维护与实际加载','']
    for arm in sorted({v for g,v in groups if g=='maintenance'}):
        records=groups[('maintenance',arm)]
        actions=Counter(item['result']['status'] for row in records for item in row.get('rounds',[]) if 'result' in item)
        lines.append(f'- {arm}：更新/拒绝动作 {dict(actions)}。')
    for arm in ['old_old','old_new','new_new']:
        records=groups.get(('progressive',arm),[])
        lines.append(f"- 按需加载 {arm}：实际skill工具成功加载 {sum(r.get('skill_loaded',False) for r in records)}/{len(records)}。")
    lines+=['','门禁逐次重新生成旧版与候选答案；严格增加成功数且硬失败不增加才接纳。拒绝保持旧版，最终测试不回流维护。维护发生错误的实验保留实际生效技能哈希和版本，不能解释成“新技能已发布”。','',
            '## 上下文机制覆盖','',
            '| 变体 | 固定/压力 | fit/overflow | 记录数 | 成功折叠总次数 | 原始记录保持 | 容量/压缩错误 |',
            '|---|---|---|---:|---:|---:|---:|']
    strata=defaultdict(list)
    for row in official.values():
        if row['group']=='compression':
            strata[(row['variant'],row.get('trigger','?'),row.get('regime','?'))].append(row)
    for (arm,trigger,regime),records in sorted(strata.items()):
        lines.append(f"| {arm} | {trigger} | {regime} | {len(records)} | {sum(r.get('metrics',{}).get('effective_fold_count',0) for r in records)} | {sum(r.get('metrics',{}).get('raw_history_preserved',False) for r in records)} | {sum(r['status']=='error' for r in records)} |")
    lines+=['','fit/overflow是按源历史构造的压力层次；实际是否溢出以服务端结果为准。固定触发与生产压力触发分列。工具历史为预先构造的合法调用/结果记录，非付费现场执行的完整长任务；相关结论仅适用于状态保持。','',
            '## 成对比较与不确定性','',
            '下表差值为新组减旧组的原始确认成功率；按任务族聚类bootstrap，2000次重采样。重复运行未当作独立题目。待复核按未确认成功计，不能解释为语义错误；中断及未覆盖重复造成选择性覆盖，本表不构成完整消融结论。小样本区间仅描述本任务集。','',
            '| 实验 | 旧→新 | 触发/层次 | 成对单元 | 聚类数 | 差值 | 95%区间 |',
            '|---|---|---|---:|---:|---:|---|']
    for item in summary.get('comparisons',[]):
        lines.append(f"| {item['group']} | {item['old']}→{item['new']} | {item.get('trigger','all')}/{item.get('regime','all')} | {item['paired_units']} | {item['clusters']} | {item['difference']} | {item['ci95']} |")
    lines+=['','[逐题、逐重复成对差异及未执行侧](core-paired-details.json)保留原状态、硬约束差、费用、用量与耗时；尚未启动的一侧不计算差值。']
    skill_comparison=next((c for c in summary.get('comparisons',[]) if c['group']=='skill_test'
        and c['old']=='frozen_old' and c['new']=='experience_bounded'),None)
    if skill_comparison and skill_comparison['difference'] is not None:
        lo,hi=skill_comparison['ci95']
        lines+=['',f"经验＋有界修改对冻结旧技能：现有{skill_comparison['paired_units']}对的任务族平均差值为{100*skill_comparison['difference']:.2f}个百分点，描述性95%区间[{100*lo:.2f}, {100*hi:.2f}]个百分点。覆盖不齐且样本少，现有证据不足以证明稳定改善。"]
    lines+=['','当前压缩结果主要暴露执行可靠性问题：单JSON有摘要截断和最低折叠次数错误，三并行有格式契约冲突、重试放大与中断；原始任务失败还包含未约定词汇的评分争议。应先修复并用新开发集验证，不能把这些数字直接解释为三层记忆原理无效或DSH已带来收益。']
    lines+=['','## 公开题与待复核','',
       '题库：BearCode已有GAIA165题与HLE500题。按稳定ID审核题面和当前能力，选择12道自包含HLE数学题；全部审核决策及源记录哈希见公开题审核表。无需图片的题目可能在原库标为MM，此处按完整题面可解性审核，不声称覆盖多模态。',
       '公开题没有真实先前交互，两组都仅作外部能力检查，**不作为压缩收益证据**。精确规范化相等为确认成功；非空不相等进入待复核，等价公式可能被保守暂挂。没有人工复核的答案不称人工评估完成。','',
       '[公开答案的确定性格式等价补充检查](core-public-answer-review.json)仅作事后附录，保留原成绩，不代替开放式或人工质量评估。','',
       '## 失败与未运行项','']
    failures=[(k,r) for k,r in official.items() if r['status'] not in ('pass',)]
    counts=Counter((r['group'],r.get('reason') or r.get('error_kind') or r['status']) for _,r in failures)
    for (group,reason),n in sorted(counts.items()):
        lines.append(f'- {group} / {reason}：{n}。')
    missing=summary.get('unstarted_units',[])
    lines.append(f'- 尚未启动：{len(missing)}，完整清单保留在summary.json。')
    lines+=['','## 可复现产物','',
       f'- [冻结协议]({root}/protocol.json)',f'- [逐次结果]({destination.parent}/core-evaluation-results.json)',
       f'- [汇总与未运行清单]({destination.parent}/core-evaluation-summary.json)',
       f'- [冻结源码清单]({root}/source-snapshot.json)',
       f'- [本地完整运行目录]({root})',
       '- [评测方法与矩阵](CORE-EVALUATION-PLAN.md)',
       '- [当前生产代码工程回归](CORE-ENGINEERING-VERIFICATION.json)',
       '- [隔离安装验证](CORE-PACKAGE-VERIFICATION.json)',
       '- [公开题审核](../benchmarks/data/core-public-audit-v1.json)','',
       '## 结论与下一轮优先级','',
       '1. 技能定位、隔离候选与严格发布门禁已有实现；定位夹具仅34种不同输入，不能把60条都当独立语言理解证据。验证题只有两道且存在高起点能力，尚不足以证明长期演化收益。',
       '2. 优先修复摘要输出契约与压力触发的重试放大，再用新的开发数据验证。当前题的自然语言评分有契约缺口，保留本轮结果；新一轮另行冻结，不在已见测试上调参。',
       '3. 补充真实文件操作、开放文本盲评和实际按需加载覆盖；再判断改进是否进入正常Agent流程。当前JSON计划题不等于真实文件工作流。',
       '4. DSH、三层记忆是机制对照；未完成DeepAgent训练方法、WikiSkill完整wiki编译或SkillOpt完整优化器复现。UI、多厂商和子任务效果按本轮范围未做专项评测。',
       '5. 未执行项需要下一轮独立预算与冻结协议；不削减重复次数后称本轮已完整完成。', '',
       '- [逐项验收与缺口](CORE-ACCEPTANCE.md)',
       '- [技能案例分析](CORE-SKILL-FINDINGS.md)',
       '- [上下文失败分析](CORE-CONTEXT-FAILURES.md)', '',
       '运行：使用冻结副本的agents包，在仓库外启动 `python -m benchmarks.core_evaluation --project <配置目录> --output <本轮目录> --ledger-root <原共享账本目录> --stage all`。重用本轮目录会恢复账本、跳过已有结果；冻结v3不含端点摘要，恢复时必须人工保持原CUN端点；新版v4另增加端点摘要和持久账本绑定。配置/源码指纹不同会拒绝恢复，已开始但未完成的单元不自动重放。','']
    destination.write_text('\n'.join(lines))
    (destination.parent/'core-evaluation-results.json').write_bytes(raw_results)
    (destination.parent/'core-evaluation-summary.json').write_bytes(raw_summary)
    report_provenance={**provenance,'budget_audit_sha256':hashlib.sha256(budget_path.read_bytes()).hexdigest() if final_budget else None}
    (destination.parent/'core-report-provenance.json').write_text(json.dumps(report_provenance,indent=2)+'\n')
    snapshot={'protocol_sha256':hashlib.sha256((root/'protocol.json').read_bytes()).hexdigest(),
              'formal_recorded':len(official),'status_counts':dict(Counter(r['status'] for r in official.values()))}
    print(json.dumps(snapshot,ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('root',type=Path)
    p.add_argument('destination',type=Path)
    args=p.parse_args()
    render(args.root,args.destination)
