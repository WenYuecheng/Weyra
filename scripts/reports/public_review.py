"""Post-hoc answer-format audit. Does not rewrite primary grades; never evals text."""
import ast
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import json
import re


def _braced(text, start):
    if start >= len(text) or text[start]!='{':
        raise ValueError('Expected brace')
    depth=1
    for end in range(start+1,len(text)):
        depth += (text[end]=='{')-(text[end]=='}')
        if depth==0:
            return text[start+1:end],end+1
    raise ValueError('Unclosed brace')


def _extract(text):
    if isinstance(text,(int,float,Decimal)):
        return str(text)
    if not isinstance(text,str) or len(text)>12000:
        raise ValueError('Unsupported answer')
    text=text.strip()
    try:
        value=json.loads(text, parse_float=Decimal)
        if isinstance(value,dict):
            if len(value)!=1 or next(iter(value)) not in ('answer','final_answer'):
                raise ValueError('Ambiguous JSON')
            return _extract(next(iter(value.values())))
        if isinstance(value,(int,float,Decimal)):
            return str(value)
    except json.JSONDecodeError:
        pass
    text=text.replace('\\\\','\\')
    if '\\boxed{' in text:
        start=text.rfind('\\boxed{')+len('\\boxed')
        value,end=_braced(text,start)
        if text[end:].strip(' .\n$'):
            raise ValueError('Nonfinal boxed answer')
        return value
    text=re.sub(r'^(?:final answer|answer)\s*:\s*','',text,flags=re.I)
    if text.startswith('$') and text.endswith('$'):
        text=text.strip('$')
    return text.strip()


def _latex(text):
    text=text.replace('\\\\','\\').replace('π','pi').replace('−','-')
    while '\\frac{' in text:
        start=text.index('\\frac{')
        numerator,end=_braced(text,start+5)
        denominator,end2=_braced(text,end)
        text=text[:start]+'(('+_latex(numerator)+')/('+_latex(denominator)+'))'+text[end2:]
    text=text.replace('\\cdot','*').replace('\\times','*').replace('\\pi','pi')
    text=text.replace('\\left','').replace('\\right','').replace('^','**')
    text=text.replace('{','(').replace('}',')')
    text=re.sub(r'(?<=\d)\s*(?=pi\b|i\b|\()','*',text)
    text=re.sub(r'(?<=pi)\s+(?=i\b)','*',text)
    return text.strip()


def _add(a,b):
    result=dict(a)
    for key,value in b.items():
        result[key]=result.get(key,Fraction(0))+value
    return {k:v for k,v in result.items() if v}


def _multiply(a,b):
    result={}
    for (p,i),x in a.items():
        for (q,j),y in b.items():
            if p+q>16:
                raise ValueError('Exponent limit')
            power=(i+j)%4
            sign=-1 if power>=2 else 1
            key=(p+q,power%2)
            result[key]=result.get(key,Fraction(0))+sign*x*y
    return {k:v for k,v in result.items() if v}


def _polynomial(node, source):
    if isinstance(node,ast.Constant) and type(node.value) in (int,float):
        if abs(node.value)>10**12:
            raise ValueError('Numeric limit')
        # AST float values are rounded. Recover exact decimal source spelling.
        literal=ast.get_source_segment(source,node)
        decimal=Decimal(literal.replace('_','')) if type(node.value) is float else None
        if decimal is not None and (not decimal.is_finite() or abs(decimal.as_tuple().exponent)>1000):
            raise ValueError('Decimal exponent limit')
        value=Fraction(decimal) if decimal is not None else Fraction(node.value)
        return {(0,0):value} if value else {}
    if isinstance(node,ast.Name) and node.id in ('pi','i'):
        return {(1,0) if node.id=='pi' else (0,1):Fraction(1)}
    if isinstance(node,ast.UnaryOp) and isinstance(node.op,(ast.UAdd,ast.USub)):
        value=_polynomial(node.operand,source)
        return {k:(-v if isinstance(node.op,ast.USub) else v) for k,v in value.items()}
    if not isinstance(node,ast.BinOp):
        raise ValueError('Unsupported expression')
    a,b=_polynomial(node.left,source),_polynomial(node.right,source)
    if isinstance(node.op,ast.Add):
        return _add(a,b)
    if isinstance(node.op,ast.Sub):
        return _add(a,{k:-v for k,v in b.items()})
    if isinstance(node.op,ast.Mult):
        return _multiply(a,b)
    if isinstance(node.op,ast.Div) and set(b)=={(0,0)} and b[(0,0)]:
        return {k:v/b[(0,0)] for k,v in a.items()}
    if isinstance(node.op,ast.Pow) and (not b or set(b)=={(0,0)}):
        power=b.get((0,0),Fraction(0))
        if power.denominator!=1 or not 0<=power<=8:
            raise ValueError('Power limit')
        result={(0,0):Fraction(1)}
        for _ in range(int(power)):
            result=_multiply(result,a)
        return result
    raise ValueError('Unsupported operator')


def review(candidate,reference):
    try:
        a,b=_extract(candidate),_extract(reference)
        if re.fullmatch('[A-H]',b):
            if not re.fullmatch('[A-H]',a):
                raise ValueError('Not an explicit choice')
            equal=a==b
        else:
            a,b=_latex(a),_latex(b)
            if max(len(a),len(b))>2000:
                raise ValueError('Expression size')
            left,right=ast.parse(a,mode='eval'),ast.parse(b,mode='eval')
            if sum(1 for _ in ast.walk(left))>150 or sum(1 for _ in ast.walk(right))>150:
                raise ValueError('Expression complexity')
            equal=_polynomial(left.body,a)==_polynomial(right.body,b)
        return {'verdict':'equivalent' if equal else 'different','method':'restricted symbolic/choice format audit'}
    except (ValueError,TypeError,SyntaxError,OverflowError,ZeroDivisionError,RecursionError,InvalidOperation):
        return {'verdict':'unresolved','method':'restricted symbolic/choice format audit'}


def review_results(root, manifest, output):
    from pathlib import Path
    root,manifest,output=Path(root),Path(manifest),Path(output)
    rows=json.loads((root/'results.json').read_text())
    cases={c['id']:c for c in json.loads(manifest.read_text())['public_cases']}
    result=[]
    for key,row in rows.items():
        if row.get('group')!='public':
            continue
        item={'key':key,'id':row['id'],'variant':row['variant'],'repeat':row['repeat'],
              'primary_status':row['status']}
        if row['status']=='pass':
            item['review']={'verdict':'equivalent','method':'primary exact match'}
        elif row['status']=='pending_review':
            item['review']=review(row.get('answer',''),cases[row['id']]['grading']['expected'])
        else:
            item['review']={'verdict':'unresolved','method':'Incomplete/failed execution not promoted'}
        result.append(item)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps({'scope':'Post-hoc deterministic format review; primary statuses unchanged; no human review claimed',
                                  'rows':result},ensure_ascii=False,indent=2))
    return result


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('root')
    p.add_argument('manifest')
    p.add_argument('output')
    a=p.parse_args()
    rows=review_results(a.root,a.manifest,a.output)
    from collections import Counter
    print(dict(Counter(r['review']['verdict'] for r in rows)))
