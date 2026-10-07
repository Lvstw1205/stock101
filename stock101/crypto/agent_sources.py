"""Stock101's dated Coinbase facts exposed through TradingAgents data tools.

This adapter is installed only inside the isolated worker. Historical runs use
upstream point-in-time vendors; today's saved quotes never enter a past run.
"""
import datetime as dt,json,math


def install(snapshots,run_date):
    from tradingagents.dataflows import router
    from tradingagents.agents import tools
    from tradingagents.dataflows.date_window import get_current_date
    from tradingagents.dataflows.vendors.yahoo import snapshot as yahoo_snapshot
    if run_date!=get_current_date() or not snapshots:return
    import pandas as pd
    from stockstats import StockDataFrame
    original=tools.build_verified_market_snapshot
    from tradingagents.dataflows.errors import VendorUnavailableError
    def rows(ticker,date):
        snap=snapshots.get(ticker)
        if not snap:return None
        cutoff=dt.datetime.combine(dt.date.fromisoformat(date),dt.time.max,dt.timezone.utc).timestamp()
        data=[b for b in snap['daily'] if b['start']+86400<=min(cutoff,snap['captured_at'])]
        return data
    def frame(ticker,date):
        data=rows(ticker,date)
        if not data:return None
        df=pd.DataFrame(data);df['date']=pd.to_datetime(df['start'],unit='s',utc=True)
        return df.set_index('date')[['open','high','low','close','volume']]
    def prices(ticker,start,end):
        df=frame(ticker,end)
        if df is None:raise VendorUnavailableError('No completed Coinbase daily history')
        return f'Coinbase completed daily OHLCV; {snapshots[ticker]["currency"]}; no fabricated candles.\n'+df.loc[start:end].tail(90).to_csv()
    def indicator(ticker,name,date,look_back_days=30):
        df=frame(ticker,date)
        if df is None:raise VendorUnavailableError('No Coinbase history')
        key={'close_50_sma':'close_50_sma','close_200_sma':'close_200_sma','close_10_ema':'close_10_ema','macd':'macd','macds':'macds','macdh':'macdh','rsi':'rsi','boll':'boll','boll_ub':'boll_ub','boll_lb':'boll_lb','atr':'atr','vwma':'vwma'}.get(name)
        if not key:return 'DATA_UNAVAILABLE: unsupported indicator.'
        required=200 if name=='close_200_sma' else 50 if name=='close_50_sma' else 30
        if len(df)<required:return 'DATA_UNAVAILABLE: insufficient completed daily candles.'
        try:return 'Coinbase daily '+name+'\n'+StockDataFrame.retype(df.copy())[key].tail(min(30,max(1,int(look_back_days)))).to_csv()
        except (KeyError,ValueError):return 'DATA_UNAVAILABLE: indicator cannot be calculated.'
    def verified(ticker,date,look_back_days=30):
        if ticker not in snapshots:return original(ticker,date,look_back_days)
        data=rows(ticker,date)
        if not data:return 'DATA_UNAVAILABLE: no Coinbase verified snapshot.'
        return json.dumps({'source':'Coinbase','ticker':ticker,'currency':snapshots[ticker]['currency'],'timeframe':'completed daily','latest':data[-1],
            'indicators':{k:indicator(ticker,k,date,1) for k in ('close_50_sma','close_200_sma','close_10_ema','macd','rsi','atr','boll','boll_ub','boll_lb','vwma')},
            'recent_closes':[{'start':b['start'],'close':b['close']} for b in data[-min(30,max(1,int(look_back_days))):]]},ensure_ascii=False)
    def news(ticker,start,end):
        snap=snapshots.get(ticker)
        if not snap:raise VendorUnavailableError('No stored Stock101 news')
        lo=dt.datetime.fromisoformat(start).replace(tzinfo=dt.timezone.utc).timestamp();hi=dt.datetime.fromisoformat(end).replace(tzinfo=dt.timezone.utc).timestamp()+86400
        articles=[a for a in snap.get('articles',[]) if lo<=float(a.get('published_at',0))<hi and float(a.get('published_at',0))<=snap['captured_at']]
        if not articles:raise VendorUnavailableError('No dated Coinbase news in this range')
        return json.dumps({'source':'Stock101 public news feeds','coverage':'recent collected articles only; not a complete historical archive','status':snap['news_status'],'articles':articles},ensure_ascii=False)
    router.VENDOR_METHODS['get_stock_data']['coinbase']=prices
    router.VENDOR_METHODS['get_indicators']['coinbase']=indicator
    router.VENDOR_METHODS['get_news']['coinbase']=news
    tools.build_verified_market_snapshot=verified
    # The trader's grounding helper may use the module function directly.
    yahoo_snapshot.build_verified_market_snapshot=verified
