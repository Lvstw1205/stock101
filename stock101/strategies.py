"""Validated rules; no eval or model-controlled order execution."""
import math
import re

FIELDS = ['price','rsi','macd','signal','histogram','volume','volume_ma20','ma5','ma10','ma20','ma50','ma60','ma120','ma200','bb_upper','bb_lower']
OPS = ['below','above','crosses_above','crosses_below']
DEFAULT = {'name':'Trend & momentum', 'entry':{'all':[{'field':'price','op':'above','value':'ma200'},{'field':'macd','op':'crosses_above','value':'signal'}]},
           'exit':{'any':[{'field':'rsi','op':'above','value':70},{'field':'macd','op':'crosses_below','value':'signal'}]}}


def validate_node(node, depth=0):
    if depth>5 or not isinstance(node,dict): raise ValueError('Rules can be nested at most five levels.')
    groups = [k for k in ('all','any') if k in node]
    if groups:
        if len(groups)!=1 or set(node)!={groups[0]}: raise ValueError('Use one AND or OR group per node.')
        items=node[groups[0]]
        if not isinstance(items,list) or not 1<=len(items)<=12: raise ValueError('Each group needs 1–12 conditions.')
        for x in items: validate_node(x,depth+1)
    else:
        if set(node)!={'field','op','value'} or node['field'] not in FIELDS or node['op'] not in OPS: raise ValueError('Unsupported condition.')
        value=node['value']
        if isinstance(value,str):
            if value not in FIELDS: raise ValueError('Unknown indicator.')
        elif isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or abs(value)>1e15:
            raise ValueError('Use a finite threshold.')


def validate(strategy):
    if not isinstance(strategy,dict) or not isinstance(strategy.get('name'),str) or not 1<=len(strategy['name'])<=100: raise ValueError('Give the strategy a name.')
    for k in ('entry','exit'): validate_node(strategy.get(k))
    return strategy


def evaluate(node, ind, i):
    if 'all' in node: return all(evaluate(n,ind,i) for n in node['all'])
    if 'any' in node: return any(evaluate(n,ind,i) for n in node['any'])
    def pair(j):
        return ind[node['field']][j], ind[node['value']][j] if isinstance(node['value'],str) else node['value']
    a,b=pair(i)
    if a is None or b is None: return False
    op=node['op']
    if op=='above': return a>b
    if op=='below': return a<b
    if i<1: return False
    x,y=pair(i-1)
    if x is None or y is None: return False
    return (x<=y and a>b) if op=='crosses_above' else (x>=y and a<b)


def reason(node, ind, i):
    if 'all' in node or 'any' in node:
        key='all' if 'all' in node else 'any'
        return (' AND ' if key=='all' else ' OR ').join('('+reason(x,ind,i)+')' for x in node[key])
    val=ind[node['field']][i]
    return f"{node['field']}={val:.4g} {node['op']} {node['value']}" if val is not None else 'Insufficient history'


def parse_text(text):
    """Constrained bilingual parser. Unsupported prose is rejected for manual review."""
    if not isinstance(text,str) or len(text)>2000: raise ValueError('Keep the rule description under 2,000 characters.')
    # Only recognize the exact supported pattern; no guessed trading rules.
    t=text.lower().strip().rstrip('.。')
    patterns=[r'rsi (?:is )?(?:below|<) (\d+(?:\.\d+)?) and macd crosses above (?:its )?signal and price (?:is )?above (?:the )?(\d+)[- ]day (?:moving average|ma)[,; ]+(?:then )?buy[.; ]+(?:sell|exit) (?:when|if) rsi (?:is )?(?:above|>) (\d+(?:\.\d+)?)',
              r'rsi가?\s*(\d+(?:\.\d+)?)\s*(?:아래고|이하고)\s*macd가?\s*골든크로스가?\s*나오면서\s*(\d+)일\s*이동평균선\s*위에\s*있을\s*때\s*매수하고\s*rsi가?\s*(\d+(?:\.\d+)?)\s*넘으면\s*팔아']
    match=next((m for p in patterns if (m:=re.fullmatch(p,t))),None)
    if not match: raise ValueError('This description needs manual review. Use the rule builder, or: RSI below 30 and MACD crosses above signal and price above 200-day MA, buy. Sell when RSI above 70')
    lo,p,hi=match.groups()
    strategy={'name':'Natural-language draft','entry':{'all':[{'field':'rsi','op':'below','value':float(lo)},{'field':'macd','op':'crosses_above','value':'signal'},{'field':'price','op':'above','value':'ma'+p}]},'exit':{'field':'rsi','op':'above','value':float(hi)}}
    return {'strategy':validate(strategy),'requires_review':True,'activated':False,'parser':'Constrained local bilingual parser'}
