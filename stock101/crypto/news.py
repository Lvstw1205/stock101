"""Bounded public RSS/news searches. External text is evidence, never instructions."""
import datetime as dt
import email.utils
import hashlib
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

FEEDS={'CoinDesk':'https://www.coindesk.com/arc/outboundfeeds/rss/',
       'Cointelegraph':'https://cointelegraph.com/rss',
       'Coinbase Status':'https://status.coinbase.com/history.rss'}
ALIASES={'BTC':['bitcoin'],'ETH':['ethereum','ether'],'SOL':['solana'],'XRP':['ripple'],
         'DOGE':['dogecoin'],'ADA':['cardano'],'AVAX':['avalanche'],'LINK':['chainlink'],
         'XLM':['stellar'],'HBAR':['hedera'],'BCH':['bitcoin cash'],'LTC':['litecoin'],
         'DOT':['polkadot'],'NEAR':['near protocol'],'QNT':['quant'],'SHIB':['shiba inu']}

def fetch(url):
    req=urllib.request.Request(url,headers={'User-Agent':'Stock101-Research/3.0','Accept':'application/rss+xml, application/xml, text/xml'})
    with urllib.request.urlopen(req,timeout=8) as response:
        raw=response.read(2_000_001)
    if len(raw)>2_000_000 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():raise ValueError('UNSAFE_OR_OVERSIZE_FEED')
    return raw

def parse(raw, source, now=None):
    now=time.time() if now is None else now
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():raise ValueError('UNSAFE_FEED')
    result=[]
    for item in ET.fromstring(raw).findall('.//item')[:150]:
        title=re.sub('<[^>]+>','',item.findtext('title','')).strip()[:350]
        if re.search(r'\b(sponsored|advertorial|paid content|press release)\b',title,re.I):continue
        url=item.findtext('link','').strip()
        try:stamp=email.utils.parsedate_to_datetime(item.findtext('pubDate','')).timestamp()
        except Exception:continue
        if not title or not 0<=now-stamp<=86400 or urllib.parse.urlparse(url).scheme!='https':continue
        publisher=item.findtext('source') or source
        host=(urllib.parse.urlparse(url).hostname or '').lower()
        provenance='OFFICIAL_STATUS' if host in ('status.coinbase.com','coinbase.statuspage.io') else 'PUBLISHER_FEED' if source in FEEDS and source!='Coinbase Status' else 'AGGREGATED_UNVERIFIED'
        # Headline flags are deliberately labelled unverified; no sentiment profit boost.
        flags=[x for x in ('exploit','hack','delist','outage','suspend','bankrupt') if re.search(r'\b'+x+r'\w*',title,re.I)]
        result.append({'id':hashlib.sha256(title.lower().encode()).hexdigest()[:24], 'title':title,'url':url,
                       'source':publisher[:100],'via':source,'published_at':stamp,'fetched_at':now,
                       'source_class':provenance,'source_host':host,'claim_verified':False,
                       'event_keywords':flags,'analysis':'headline keyword match; article claim not independently verified'})
    return result

def relevant(article, symbol, name=''):
    name=name or ''
    title=article['title']; words=ALIASES.get(symbol,[])
    if len(name)>4 and name.lower() not in ('token','coin'):words=words+[name.lower()]
    if any(re.search(r'(?<!\w)'+re.escape(w)+r'(?!\w)',title,re.I) for w in words):return True
    if symbol in {'ONE','FOR','ON','IN','NEAR','GAS','MASK','SPELL','TIME','HIGH','LOW','SUI'}:
        return bool(re.search(r'\$'+re.escape(symbol)+r'\b|\('+re.escape(symbol)+r'\)',title))
    return len(symbol)>=3 and bool(re.search(r'(?<!\w)'+re.escape(symbol)+r'(?!\w)',title))

class NewsResearch:
    def __init__(self, repo, loader=fetch):
        self.repo=repo; self.loader=loader; self.lock=threading.RLock(); self.products=[]; self.thread=None; self.cursor=0
        with repo.db() as c:saved=repo.get(c,'research_news',{})
        self.articles=saved.get('articles',[]); self.sources=saved.get('sources',{}); self.searched={}; self.last_search=None
    def set_products(self, products):
        with self.lock:self.products=list(products)
        if not self.thread:
            self.thread=threading.Thread(target=self.run,name='stock101-public-news',daemon=True); self.thread.start()
    def collect(self, source, url):
        try:
            items=parse(self.loader(url),source)
            with self.lock:
                rows={a['id']:a for a in self.articles if 0<=time.time()-a['published_at']<=86400}
                rows.update({a['id']:a for a in items})
                self.articles=sorted(rows.values(),key=lambda a:a['published_at'],reverse=True)[:400]
                self.sources[source]={'checked_at':time.time(),'ok':True,'articles':len(items)}
        except Exception:
            with self.lock:self.sources[source]={'checked_at':time.time(),'ok':False,'error':'NEWS_SOURCE_UNAVAILABLE'}
        with self.lock:saved={'articles':self.articles,'sources':self.sources}
        with self.repo.db() as c:self.repo.set(c,'research_news',saved)
    def run(self):
        feed_at=0
        while True:
            if time.time()-feed_at>600:
                for source,url in FEEDS.items():self.collect(source,url)
                feed_at=time.time()
            with self.lock:
                batch=[]
                for _ in range(min(5,len(self.products))):
                    p=self.products[self.cursor%len(self.products)]; self.cursor+=1; batch.append(p)
            if batch:
                terms=[(ALIASES.get(p['base_currency_id']) or [p.get('base_name') or p['base_currency_id']])[0] for p in batch]
                query='('+' OR '.join('"'+t.replace('"','')+'"' for t in terms)+') crypto when:1d'
                url='https://news.google.com/rss/search?'+urllib.parse.urlencode({'q':query,'hl':'en-CA','gl':'CA','ceid':'CA:en'})
                self.collect('Google News search',url)
                with self.lock:
                    self.last_search={'at':time.time(),'assets':[p['base_currency_id'] for p in batch]}
                    if self.sources.get('Google News search',{}).get('ok'):
                        for p in batch:self.searched[p['base_currency_id']]=time.time()
            threading.Event().wait(90)
    def context(self, product):
        now=time.time(); symbol=product['base_currency_id']
        with self.lock:
            rows=[a for a in self.articles if 0<=now-a['published_at']<=86400 and relevant(a,symbol,product.get('base_name',''))][:5]
            fresh=any(s.get('ok') and now-s['checked_at']<1200 for s in self.sources.values())
            searched=self.searched.get(symbol)
        flagged=[a for a in rows if a['event_keywords'] and now-a['published_at']<21600]
        return {'status':'CURRENT' if fresh else 'UNAVAILABLE_OR_STALE','articles':rows,'searched_at':searched,
                'allocation_multiplier':.5 if flagged else .75 if not fresh else 1,
                'impact':'SMALLER_ENTRY_HEADLINE_EVENT' if flagged else 'SMALLER_ENTRY_NEWS_UNAVAILABLE' if not fresh else 'NO_DETECTED_HEADLINE_EVENT',
                'method':'Published headlines + rotating public web searches. No LLM sentiment or news-only orders.'}
    def state(self):
        with self.lock:return {'sources':dict(self.sources),'articles':[a for a in self.articles if 0<=time.time()-a['published_at']<=86400][:8],
                              'searched_assets':len(self.searched),'last_search':self.last_search,'feed_interval_seconds':600,'search_interval_seconds':90}
