import ccxt
import time
import pandas as pd
import os
import json
import threading
import queue
import numpy as np
import tkinter as tk
from tkinter import ttk, scrolledtext
from dotenv import load_dotenv
import requests
from datetime import datetime

# 1) 환경변수 로드
load_dotenv()
API_KEY = os.getenv("BINANCE_ACCESS")
SECRET_KEY = os.getenv("BINANCE_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ---------------------------------------------------------
# [설정] V7.7.4 (Smart Expansion + System Test)
# - 기본 2개 제한 (안전)
# - BTC 추세 일치 시 3개 허용 (수익 극대화)
# - 시작 시 가장 저렴한 코인으로 시스템 점검 수행
# ---------------------------------------------------------
MAX_MARGIN_RISK = 0.50  # 리스크 관리 (50%)
BOT_VERSION = "7.7.4"

TARGET_COINS = {
    # 1. ETH: 비중 확대 (30%)
    "ETH/USDT": {
        "leverage": 4, 
        "ratio": 0.30,      
        "min_qty": 0.01, 
        "rsi_min": 30, 
        "rsi_max": 70, 
        "revenge_mode": True 
    },
    # 2. SOL: 주력 공격수 (20%)
    "SOL/USDT": {
        "leverage": 3, 
        "ratio": 0.20, 
        "min_qty": 0.04, 
        "rsi_min": 33, 
        "rsi_max": 65, 
        "revenge_mode": True 
    },
    # 3. SUI: 추세 추종 (20%)
    "SUI/USDT": {
        "leverage": 2, 
        "ratio": 0.20, 
        "min_qty": 0.4, 
        "rsi_min": 30, 
        "rsi_max": 70, 
        "revenge_mode": False 
    }
}

# ---------------------------------------------------------
# [Journal] 매매 일지
# ---------------------------------------------------------
class TradeJournal:
    def __init__(self, filename="trade_history.json"):
        self.filename = filename
        self.lock = threading.Lock()
        self._ensure_file()

    def _ensure_file(self):
        if not os.path.exists(self.filename):
            init_data = {"total_profit_usdt": 0.0, "history": []}
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump(init_data, f, indent=4, ensure_ascii=False)

    def load(self):
        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except: return {"total_profit_usdt": 0.0, "history": []}

    def save(self, data):
        with open(self.filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

    def log_action(self, symbol, side, action, price, amount, msg="", roe=0.0, pnl=0.0):
        with self.lock:
            data = self.load()
            record = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": symbol, "side": side, "action": action,
                "price": price, "amount": amount, "msg": msg
            }
            if action == "CLOSE":
                record["roe"], record["pnl"] = round(roe, 2), round(pnl, 4)
                data["total_profit_usdt"] += pnl
            data["history"].append(record)
            self.save(data)
            return data["total_profit_usdt"]

# ---------------------------------------------------------
# [Core] 매매 로직
# ---------------------------------------------------------
class BinanceMultiTrader:
    def __init__(self, log_queue=None):
        self.state_file = "bot_state_multi.json"
        self.log_queue = log_queue 
        self.is_running = False          
        self.journal = TradeJournal() 
        self.hedge_mode = False
        self.data_lock = threading.Lock()

        self.gui_data = {
            "total_usdt": 0.0, "total_unrealized_pnl": 0.0,
            "accumulated_profit": self.journal.load().get("total_profit_usdt", 0.0), 
            "btc_trend": "WAIT", 
            "coins": {} 
        }

        self.exchange = ccxt.binance({
            "apiKey": API_KEY, "secret": SECRET_KEY, "enableRateLimit": True,
            "options": {"defaultType": "future", "adjustForTimeDifference": True, "recvWindow": 20000},
            "timeout": 20000,
        })

        self.targets = TARGET_COINS
        self.symbols = list(self.targets.keys())
        
        self._pos_state = {}        
        self._trend_cache = {}      
        self._whale_lines = {}
        self._macro_zones = {} 
        self._macro_last_update = {} 
        self._cooldown_until = {}   
        self._exit_cooldown = {}    
        self._last_entry_candle = {} 
        self._cached_raw_positions = []
        self._btc_trend_cache = {'ts': 0, 'val': "UNKNOWN"}
        self._loss_streak = {}
        self._lockdown_until = {}
        self._pos_missing_count = {} 
        self._pos_missing_first_ts = {} 
        self._trap_history = {} 

        for sym in self.symbols:
            self._pos_state[sym] = {
                "has": False, "side": "none", "entry_ts": 0.0, 
                "status": "NORMAL", "max_roe": None,
                "regime": "RANGE",
                "is_revenge": False
            }
            self._trend_cache[sym] = {
                "1h": {"ts": 0, "val": "UNKNOWN"}, 
                "4h": {"ts": 0, "val": "UNKNOWN"}
            }
            self._whale_lines[sym] = []
            self._macro_zones[sym] = [] 
            self._macro_last_update[sym] = 0
            self._cooldown_until[sym] = 0
            self._exit_cooldown[sym] = 0
            self._last_entry_candle[sym] = 0
            self._loss_streak[sym] = 0
            self._lockdown_until[sym] = 0
            self._pos_missing_count[sym] = 0
            self._pos_missing_first_ts[sym] = 0
            self._trap_history[sym] = None 
            
            with self.data_lock:
                self.gui_data["coins"][sym] = {
                    "price": 0, "rsi": 0, "trend_1h": "-", "regime": "-", 
                    "pos_side": "NONE", "roe": 0, "entry": 0, "streak": 0
                }

        self.markets_loaded = False
        self.load_state()

    def log(self, msg):
        print(msg, flush=True) 
        if self.log_queue:
            self.log_queue.put(msg)
        if any(x in msg for x in ["진입", "청산", "에러", "실패", "리셋", "시작", "분할", "본전", "Regime", "Streak", "완화", "복수", "함정", "Lockdown", "방어", "Zone", "급락"]):
            self.send_telegram(msg)

    def send_telegram(self, msg: str):
        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
            except: pass

    def normalize_symbol(self, symbol):
        return symbol.replace("/", "").replace(":", "").replace("_", "")

    def safe_call(self, fn, *args, **kwargs):
        last_err = None
        for i in range(3):
            try: return fn(*args, **kwargs)
            except Exception as e:
                last_err = e
                msg = str(e)
                if "precision" in msg or "ReduceOnly" in msg or "insufficient" in msg: raise e
                time.sleep(0.5)
        raise Exception(f"API Failed: {last_err}")

    def initialize_account(self):
        try:
            self.safe_call(self.exchange.load_markets)
            self.markets_loaded = True
            try:
                if hasattr(self.exchange, "fapiPrivateGetPositionSideDual"):
                    r = self.safe_call(self.exchange.fapiPrivateGetPositionSideDual)
                    self.hedge_mode = str((r or {}).get("dualSidePosition", "false")).lower() == "true"
            except: pass
            self.log(f"[*] 계정 모드: {'Hedge' if self.hedge_mode else 'One-Way'}")
            for sym in self.symbols:
                try: self.safe_call(self.exchange.set_leverage, self.targets[sym]['leverage'], sym)
                except: pass
            self.log("[*] 계정 초기화 완료")
        except Exception as e: self.log(f"[!] 초기화 실패: {e}")

    def save_state(self):
        try:
            tmp = self.state_file + ".tmp"
            save_data = {
                "version": BOT_VERSION,
                "pos_state": self._pos_state,
                "loss_streak": self._loss_streak,
                "trap_history": self._trap_history,
                "lockdown_until": self._lockdown_until,
                "macro_zones": self._macro_zones 
            }
            with open(tmp, "w", encoding="utf-8") as f: json.dump(save_data, f, ensure_ascii=False)
            os.replace(tmp, self.state_file)
        except: pass

    def load_state(self):
        if not os.path.exists(self.state_file): return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
            
            if saved.get("version") != BOT_VERSION:
                self.log(f"⚠️ 버전 불일치 ({saved.get('version')} -> {BOT_VERSION}). 상태 초기화.")
                return 

            if "pos_state" in saved:
                raw_pos = saved["pos_state"]
                raw_streak = saved.get("loss_streak", {})
                raw_trap = saved.get("trap_history", {})
                raw_lock = saved.get("lockdown_until", {})
                raw_zones = saved.get("macro_zones", {})
            else:
                raw_pos = saved
                raw_streak = {}
                raw_trap = {}
                raw_lock = {}
                raw_zones = {}

            for sym in self.symbols:
                if sym in raw_pos:
                    self._pos_state[sym] = raw_pos[sym]
                    if "status" not in self._pos_state[sym]: self._pos_state[sym]["status"] = "NORMAL"
                    if "regime" not in self._pos_state[sym]: self._pos_state[sym]["regime"] = "RANGE"
                    if "is_revenge" not in self._pos_state[sym]: self._pos_state[sym]["is_revenge"] = False
                
                if sym in raw_streak: self._loss_streak[sym] = raw_streak[sym]
                if sym in raw_trap: self._trap_history[sym] = raw_trap[sym]
                if sym in raw_lock: self._lockdown_until[sym] = raw_lock[sym]
                if sym in raw_zones: self._macro_zones[sym] = raw_zones[sym]

        except: pass

    def safe_force_reset(self, sym, reason):
        try:
            self.refresh_all_positions()
            side, amt, _, _, _ = self.get_position_details(sym)
            if side != "none" and amt > 0:
                min_q = self.targets[sym].get("min_qty", 0.0)
                if amt > min_q: 
                    self.log(f"🚫 [{sym}] 상태 리셋 차단! (실제 잔고 발견: {amt})")
                    if not self._pos_state[sym]['has']:
                        self._pos_state[sym]['has'] = True
                        self._pos_state[sym]['side'] = side
                    return
        except: pass
        self._force_reset_state(sym, reason)

    def _force_reset_state(self, sym, reason=""):
        self.log(f"♻️ [{sym}] 상태 리셋 ({reason})")
        current_regime = self._pos_state[sym].get("regime", "RANGE")
        self._pos_state[sym] = {
            "has": False, "side": "none", "entry_ts": 0.0, 
            "status": "NORMAL", "max_roe": None,
            "regime": current_regime,
            "is_revenge": False
        }
        self.save_state()

    def get_btc_trend(self, force=False):
        now = time.time()
        cache_ttl = 60 
        if not force and (now - self._btc_trend_cache['ts']) < cache_ttl: 
            return self._btc_trend_cache['val']
        try:
            ohlcv_15m = self.safe_call(self.exchange.fetch_ohlcv, "BTC/USDT", "15m", limit=50)
            ohlcv_1h = self.safe_call(self.exchange.fetch_ohlcv, "BTC/USDT", "1h", limit=50)
            df_15 = pd.DataFrame(ohlcv_15m, columns=["ts", "o", "h", "l", "c", "v"])
            df_1h = pd.DataFrame(ohlcv_1h, columns=["ts", "o", "h", "l", "c", "v"])
            ma20_15 = df_15['c'].rolling(20).mean().iloc[-1]
            curr = df_15['c'].iloc[-1]
            ma20_1h = df_1h['c'].rolling(20).mean().iloc[-1]
            
            trend_1h = "UP" if df_1h['c'].iloc[-1] > ma20_1h else "DOWN"
            diff = (curr - ma20_15) / ma20_15
            
            val = "SIDEWAYS"
            if abs(diff) >= 0.001: 
                if diff > 0: val = "UP"
                else: val = "DOWN"
            
            if val == "UP" and trend_1h == "DOWN": val = "SIDEWAYS"
            if val == "DOWN" and trend_1h == "UP": val = "SIDEWAYS"

            self._btc_trend_cache = {'ts': now, 'val': val}
            with self.data_lock: self.gui_data["btc_trend"] = f"{val}"
            return val
        except: return "UNKNOWN"

    def refresh_all_positions(self):
        try:
            raw = self.safe_call(self.exchange.fetch_positions)
            self._cached_raw_positions = raw
            bal = self.safe_call(self.exchange.fetch_balance, {"type": "future"})
            total_usdt = float(bal['USDT']['total'])
            total_unrealized = 0.0
            norm_symbols = {self.normalize_symbol(s): s for s in self.symbols}
            for p in raw:
                p_norm = self.normalize_symbol(p['symbol'])
                if p_norm in norm_symbols:
                    total_unrealized += float(p.get('unrealizedPnl', 0))
            with self.data_lock:
                self.gui_data["total_usdt"] = total_usdt
                self.gui_data["total_unrealized_pnl"] = total_unrealized
        except Exception as e: self.log(f"[!] 포지션 갱신 실패: {e}")

    def get_position_details(self, sym):
        if not hasattr(self, '_cached_raw_positions'): return "none", 0,0,0,0
        
        target_norm = self.normalize_symbol(sym) # 예: SOLUSDT
        target_pos = None
        
        for p in self._cached_raw_positions:
            if p.get('info', {}).get('symbol') == target_norm:
                target_pos = p
                break
            if p['symbol'] == sym:
                target_pos = p
                break
        
        if not target_pos: return "none", 0.0, 0.0, 0.0, 0.0
        
        def _to_float(x):
            try: return float(x)
            except: return 0.0
            
        info = target_pos.get("info", {})
        raw_amt = _to_float(info.get("positionAmt", 0))
        amt = abs(raw_amt)
        if amt == 0: return "none", 0.0, 0.0, 0.0, 0.0
        side = "long" if raw_amt > 0 else "short"
        ep = _to_float(target_pos.get("entryPrice", 0))
        pnl = _to_float(target_pos.get("unrealizedPnl", 0))
        margin = _to_float(target_pos.get("initialMargin", 0))
        if margin == 0 and ep > 0: margin = (amt * ep) / self.targets[sym]['leverage']
        return side, amt, ep, pnl, margin

    def get_market_data(self, sym):
        ohlcv = self.safe_call(self.exchange.fetch_ohlcv, sym, "5m", limit=250)
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        delta = df["close"].diff()
        up = delta.clip(lower=0); down = -1 * delta.clip(upper=0)
        ma_up = up.ewm(alpha=1/14).mean(); ma_down = down.ewm(alpha=1/14).mean()
        df["rsi"] = 100 - (100 / (1 + ma_up/ma_down))
        df["ma20"] = df["close"].rolling(20).mean()
        df["ma200"] = df["close"].rolling(200).mean()
        df["vol_ma"] = df["volume"].rolling(20).mean()
        df["vol_ratio"] = df["volume"] / df["vol_ma"]
        df['std'] = df['close'].rolling(20).std()
        df['upper'] = df['ma20'] + (df['std'] * 2)
        df['lower'] = df['ma20'] - (df['std'] * 2)
        df['tr'] = np.maximum(df['high'] - df['low'], 
                              np.maximum(abs(df['high'] - df['close'].shift(1)), 
                                         abs(df['low'] - df['close'].shift(1))))
        df['dm_plus'] = np.where((df['high'] - df['high'].shift(1)) > (df['low'].shift(1) - df['low']), 
                                 np.maximum(df['high'] - df['high'].shift(1), 0), 0)
        df['dm_minus'] = np.where((df['low'].shift(1) - df['low']) > (df['high'] - df['high'].shift(1)), 
                                  np.maximum(df['low'].shift(1) - df['low'], 0), 0)
        alpha = 1/14
        df['str'] = df['tr'].ewm(alpha=alpha).mean()
        df['sdm_plus'] = df['dm_plus'].ewm(alpha=alpha).mean()
        df['sdm_minus'] = df['dm_minus'].ewm(alpha=alpha).mean()
        df['di_plus'] = (df['sdm_plus'] / df['str']) * 100
        df['di_minus'] = (df['sdm_minus'] / df['str']) * 100
        df['dx'] = (abs(df['di_plus'] - df['di_minus']) / (df['di_plus'] + df['di_minus'])) * 100
        df['adx'] = df['dx'].ewm(alpha=alpha).mean()
        return df

    def get_trend(self, sym, tf):
        cache = self._trend_cache[sym].get(tf, {"ts": 0, "val": "UNKNOWN"})
        ttl = 300 if tf == "1h" else 900 
        if time.time() - cache['ts'] < ttl: return cache['val']
        try:
            ohlcv = self.safe_call(self.exchange.fetch_ohlcv, sym, tf, limit=50)
            df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
            ma200 = df['c'].rolling(200).mean().iloc[-1]
            val = "UP" if df['c'].iloc[-1] > ma200 else "DOWN"
        except: val = "UNKNOWN"
        self._trend_cache[sym][tf] = {'ts': time.time(), 'val': val}
        return val

    def update_macro_zones(self, sym, current_price):
        if time.time() - self._macro_last_update.get(sym, 0) < 14400: # 4시간
            return

        try:
            ohlcv = self.safe_call(self.exchange.fetch_ohlcv, sym, "4h", limit=250)
            df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
            
            vol_mean = df['v'].mean()
            zones = []
            
            existing_zones = self._macro_zones.get(sym, [])
            if len(existing_zones) > 10: existing_zones = existing_zones[-5:]

            recent_df = df.iloc[-50:]
            for idx, row in recent_df.iterrows():
                if row['v'] > vol_mean * 1.5: 
                    body = abs(row['c'] - row['o'])
                    upper_wick = row['h'] - max(row['c'], row['o'])
                    lower_wick = min(row['c'], row['o']) - row['l']
                    
                    if lower_wick > body:
                        zones.append({'price': row['l'], 'type': 'SUPPORT', 'strength': row['v']})
                    if upper_wick > body:
                        zones.append({'price': row['h'], 'type': 'RESISTANCE', 'strength': row['v']})

            merged_zones = existing_zones 
            for z in zones:
                is_duplicate = False
                for ez in merged_zones:
                    if abs(ez['price'] - z['price']) / z['price'] < 0.01:
                        is_duplicate = True
                        break
                if not is_duplicate:
                    merged_zones.append(z)
            
            final_zones = []
            for z in merged_zones:
                if current_price > z['price']: z['type'] = 'SUPPORT'
                else: z['type'] = 'RESISTANCE'
                final_zones.append(z)
            
            final_zones.sort(key=lambda x: abs(x['price'] - current_price))
            self._macro_zones[sym] = final_zones[:5]
            self._macro_last_update[sym] = time.time()
            
        except Exception as e:
            pass 

    def check_macro_safety(self, sym, side, current_price):
        self.update_macro_zones(sym, current_price)
        zones = self._macro_zones.get(sym, [])
        
        for z in zones:
            if side == "short" and z['type'] == 'SUPPORT':
                dist = (current_price - z['price']) / current_price
                if 0 < dist < 0.008: 
                    self.log(f"🛡️ [{sym}] 4H 대형 지지선({z['price']}) 접근중({dist*100:.2f}%) -> 숏 진입 방어")
                    return False
            
            if side == "long" and z['type'] == 'RESISTANCE':
                dist = (z['price'] - current_price) / current_price
                if 0 < dist < 0.008:
                    self.log(f"🛡️ [{sym}] 4H 대형 저항선({z['price']}) 접근중({dist*100:.2f}%) -> 롱 진입 방어")
                    return False
        return True

    def update_whale_lines(self, sym, df):
        if len(df) < 30: return
        prev = df.iloc[-2]
        if prev['volume'] >= prev['vol_ma'] * 3.0 and prev['close'] > prev['open']:
            mid_price = (prev['high'] + prev['low']) / 2.0
            if not any(abs(x - mid_price)/mid_price < 0.001 for x in self._whale_lines[sym]):
                self._whale_lines[sym].append(mid_price)
        confirmed_close = df.iloc[-2]['close']
        valid_lines = []
        for line in self._whale_lines[sym]:
            if confirmed_close >= line: valid_lines.append(line)
        if len(valid_lines) > 5: valid_lines = valid_lines[-5:]
        self._whale_lines[sym] = valid_lines

    def check_whale_support(self, sym, curr):
        for line in self._whale_lines[sym]:
            if curr < line * 0.99: continue 
            if abs(curr - line) / line <= 0.002: return True
        return False

    def check_breakout(self, df):
        curr = df.iloc[-1]; prev = df.iloc[-2]
        rh = df['high'].iloc[-22:-2].max(); rl = df['low'].iloc[-22:-2].min()
        buf = 0.001
        is_up = (prev['close'] <= rh) and (curr['close'] > rh * (1 + buf))
        is_down = (prev['close'] >= rl) and (curr['close'] < rl * (1 - buf))
        return is_up, is_down

    def execute_entry(self, sym, side, reason, candle_ts, boost=False):
        if time.time() < self._lockdown_until.get(sym, 0): return 

        is_revenge = "복수" in reason
        is_box = "박스" in reason 

        if not is_revenge:
            if time.time() < self._cooldown_until[sym]: return
            if time.time() < self._exit_cooldown[sym]: return
        
        if candle_ts <= self._last_entry_candle[sym]: return

        # [V7.7.4 Upgrade] 스마트 동시 방향 리스크 제어 (BTC 트렌드 연동)
        same_side_count = 0
        for s in self.symbols:
            if self._pos_state[s]['has'] and self._pos_state[s]['side'] == side:
                same_side_count += 1
        
        # 기본 제한: 2개 (보수적 운용)
        max_allowed = 2 
        
        # BTC 트렌드 확인 (API 호출 없이 캐시 사용)
        btc_trend = self.get_btc_trend(force=False)

        # [조건] BTC 추세와 내 진입 방향이 일치하면 -> 제한을 3개로 확장
        if (side == "long" and btc_trend == "UP") or \
           (side == "short" and btc_trend == "DOWN"):
            max_allowed = 3
        
        if same_side_count >= max_allowed:
            # self.log(f"⚖️ [{sym}] {side} 포지션 과다({same_side_count} >= {max_allowed}) -> 진입 제한")
            return

        try:
            ticker = self.safe_call(self.exchange.fetch_ticker, sym)
            curr_p = float(ticker['last'])
            if not self.check_macro_safety(sym, side, curr_p):
                return 
        except: pass

        streak = self._loss_streak.get(sym, 0)
        if streak >= 4:
            self._lockdown_until[sym] = time.time() + 86400 
            self._loss_streak[sym] = 2 
            self.log(f"🚨 [{sym}] 연패 누적(4회)으로 24시간 Lockdown 진입")
            self.save_state()
            return
        
        if streak >= 3:
            if not (is_revenge or is_box): return

        try:
            bal = self.safe_call(self.exchange.fetch_balance, {"type": "future"})
            total = float(bal['USDT']['total'])
            free = float(bal['USDT']['free'])
            
            used_ratio = 1.0 - (free / max(total, 1.0))
            if used_ratio > MAX_MARGIN_RISK: return

            ratio = self.targets[sym]['ratio']
            lev = self.targets[sym]['leverage']

            if streak == 2:
                ratio *= 0.5
                reason += "(Streak 2: Size 50%)"
            elif streak >= 3:
                if is_box:
                    ratio *= 0.2 
                    reason += f"(Streak {streak}: Box Size 20%)"
                else:
                    ratio *= 0.3 
                    reason += f"(Streak {streak}: Size 30%)"
            
            if is_revenge:
                if streak < 3: ratio *= 0.7 

            cost = max(6.0, total * ratio) 
            
            ticker = self.safe_call(self.exchange.fetch_ticker, sym)
            price = float(ticker['last'])
            amt = float(self.exchange.amount_to_precision(sym, (cost * lev) / price))
            if amt <= 0: return

            params = {}
            if self.hedge_mode: params["positionSide"] = "LONG" if side == "long" else "SHORT"

            if side == "long": self.safe_call(self.exchange.create_market_buy_order, sym, amt, params)
            else: self.safe_call(self.exchange.create_market_sell_order, sym, amt, params)
            
            time.sleep(0.5)
            self.refresh_all_positions()
            real_side, real_amt, _, _, _ = self.get_position_details(sym)
            if real_amt == 0:
                self.log(f"⚠️ [{sym}] 주문이 체결되지 않음 (Verify Failed). 상태 롤백.")
                return

            tag = "🔥" if boost else ""
            msg = f"🚀 [{sym}] {side.upper()} 진입 ({reason}) {tag}| 💰약 {cost:.1f}$"
            self.log(msg)
            self.journal.log_action(sym, side, "OPEN", price, amt, msg)
            self._last_entry_candle[sym] = candle_ts 
            self._cooldown_until[sym] = time.time() + 20
            self._pos_state[sym] = {
                "has": True, "side": side, "entry_ts": time.time(), 
                "status": "NORMAL", "max_roe": None, 
                "regime": self._pos_state[sym].get("regime", "RANGE"),
                "is_revenge": is_revenge
            }
            self.save_state()
            
        except Exception as e: self.log(f"[!] 주문 에러 ({sym}): {e}")

    def execute_exit(self, sym, side, amount, reason, entry_price, is_partial=False):
        try:
            amt = float(self.exchange.amount_to_precision(sym, amount))
            ticker = self.safe_call(self.exchange.fetch_ticker, sym)
            curr_price = float(ticker['last'])

            params = {"reduceOnly": True}
            if self.hedge_mode: params["positionSide"] = "LONG" if side == "long" else "SHORT"

            if side == "long": self.safe_call(self.exchange.create_market_sell_order, sym, amt, params)
            else: self.safe_call(self.exchange.create_market_buy_order, sym, amt, params)
            
            if side == "long": pnl = (curr_price - entry_price) * amt
            else: pnl = (entry_price - curr_price) * amt
            
            margin = (amt * entry_price) / self.targets[sym]['leverage']
            roe = 0.0
            if margin > 0: roe = (pnl / margin * 100)

            msg = f"💰 [{sym}] 청산: {reason} (PnL: {pnl:+.2f}$)"
            self.log(msg)
            new_total = self.journal.log_action(sym, side, "CLOSE", curr_price, amt, reason, roe, pnl)
            with self.data_lock: self.gui_data["accumulated_profit"] = new_total

            if not is_partial:
                if pnl < 0:
                    self._loss_streak[sym] = self._loss_streak.get(sym, 0) + 1
                    self.log(f"⚠️ [{sym}] 손절 누적: {self._loss_streak[sym]}회")
                    if self._loss_streak[sym] >= 4:
                        self._lockdown_until[sym] = time.time() + 86400
                        self.log(f"🧊 [{sym}] 4연속 손절 -> 24시간 Lockdown")
                    elif self._loss_streak[sym] == 3:
                        self.log(f"🧊 [{sym}] 3연속 손절 -> Revenge/Box Only 모드")
                    
                    if self.targets[sym].get("revenge_mode", False):
                        # [Fix 3] force=True 과다 호출 방지 (여기선 필요) -> But 아래 Trap 체크에선 False 권장
                        btc_trend = self.get_btc_trend(force=True)
                        is_btc_force = False
                        if side == "long" and btc_trend == "DOWN": is_btc_force = True
                        if side == "short" and btc_trend == "UP": is_btc_force = True
                        
                        if not is_btc_force:
                            self._trap_history[sym] = {
                                "price": curr_price, 
                                "side": side,        
                                "ts": time.time(),
                                "extreme_price": curr_price,
                                "btc_trend": btc_trend
                            }
                            self.log(f"📝 [{sym}] 함정(Trap) 후보 등록: {side} @ {curr_price} (15분 관찰 시작)")

                elif roe >= 1.5 and pnl > 0: 
                    if self._loss_streak[sym] > 0:
                        self._loss_streak[sym] -= 1
                        self.log(f"👍 [{sym}] 진짜 승리(ROE {roe:.2f}%, PnL {pnl:.2f})! 연패 완화")
                    else:
                        self._loss_streak[sym] = 0
                    self._trap_history[sym] = None 

                cooldown = 300 
                if "하드" in reason: cooldown = 3600 
                elif "스마트" in reason or "손절" in reason or "급락" in reason: cooldown = 900 
                
                if self._loss_streak[sym] < 3:
                     self._exit_cooldown[sym] = time.time() + cooldown

                self.safe_force_reset(sym, f"완전청산({reason})")
            
            else:
                 self.refresh_all_positions()
                 s2, a2, _, _, _ = self.get_position_details(sym)
                 min_q = self.targets[sym].get("min_qty", 0.0)
                 if s2 != "none" and a2 > 0 and a2 < min_q:
                     self.log(f"🧹 [{sym}] Dust({a2}) 정리 시도")
                     if s2 == "long": self.safe_call(self.exchange.create_market_sell_order, sym, a2, params)
                     else: self.safe_call(self.exchange.create_market_buy_order, sym, a2, params)
                     self.safe_force_reset(sym, "부분청산 후 Dust 정리")

            self._cooldown_until[sym] = time.time() + 10
            self.save_state()
            
        except Exception as e:
            err_msg = str(e)
            if "precision" in err_msg or "ReduceOnly" in err_msg or "insufficient" in err_msg:
                self.safe_force_reset(sym, "API Error(Dust)")
            else: self.log(f"[!] 청산 에러 ({sym}): {e}")
            self.refresh_all_positions()

    def process_symbol(self, sym):
        try:
            if time.time() < self._lockdown_until.get(sym, 0): return

            side, amt, entry, pnl, margin = self.get_position_details(sym)
            if side == "error": return
            
            if side != "none":
                self._pos_missing_count[sym] = 0
                self._pos_missing_first_ts[sym] = 0
                if not self._pos_state[sym]['has']:
                    self.log(f"🧟 [{sym}] 좀비 포지션 발견! 상태 복구.")
                    self._pos_state[sym] = {"has": True, "side": side, "entry_ts": time.time(), "status": "NORMAL", "max_roe": None, "is_revenge": False}
                    self.save_state()
                
                min_q = self.targets[sym].get("min_qty", 0.0)
                if amt < min_q:
                     self.log(f"🧹 [{sym}] Dust({amt}) 정리")
                     params = {"reduceOnly": True}
                     if self.hedge_mode: params["positionSide"] = "LONG" if side == "long" else "SHORT"
                     if side == "long": self.safe_call(self.exchange.create_market_sell_order, sym, amt, params)
                     else: self.safe_call(self.exchange.create_market_buy_order, sym, amt, params)
                     self.safe_force_reset(sym, "Dust 정리")
                     return
            else:
                if self._pos_state[sym]['has']:
                    self._pos_missing_count[sym] += 1
                    if self._pos_missing_first_ts.get(sym, 0) == 0:
                        self._pos_missing_first_ts[sym] = time.time()
                    elapsed = time.time() - self._pos_missing_first_ts[sym]
                    if self._pos_missing_count[sym] >= 3 and elapsed >= 10:
                        self.safe_force_reset(sym, "실포지션 없음(3회+10초)")
                        self._pos_missing_count[sym] = 0
                        self._pos_missing_first_ts[sym] = 0
                else:
                     self._pos_missing_count[sym] = 0
                     self._pos_missing_first_ts[sym] = 0

            df = self.get_market_data(sym)
            if len(df) < 50: return
            
            closed = df.iloc[-2]; curr_row = df.iloc[-1]
            curr = float(curr_row['close']); candle_ts = int(closed['timestamp'])
            rsi = float(closed['rsi']); ma20 = float(closed['ma20'])
            trend_1h = self.get_trend(sym, "1h"); trend_4h = self.get_trend(sym, "4h")
            btc_trend = self.get_btc_trend()
            
            adx = float(closed['adx'])
            prev_adx = float(df.iloc[-3]['adx'])
            prev2_adx = float(df.iloc[-4]['adx']) 
            upper_band = float(closed['upper'])
            lower_band = float(closed['lower'])
            
            vol_ratio = float(curr_row['vol_ratio'])
            is_green = curr_row['close'] > curr_row['open']
            is_red = curr_row['close'] < curr_row['open']
            
            current_regime = self._pos_state[sym].get("regime", "RANGE")
            new_regime = current_regime

            if current_regime == "RANGE":
                if adx > 25 and adx > prev_adx and prev_adx > prev2_adx:
                    new_regime = "TREND"
            else: 
                if adx < 20:
                    new_regime = "RANGE"
            
            self._pos_state[sym]["regime"] = new_regime

            self.update_whale_lines(sym, df)
            is_whale = self.check_whale_support(sym, curr)
            mem_cnt = len(self._whale_lines[sym])
            
            roe = 0.0
            if margin > 0: roe = (pnl / margin * 100)
            
            streak = self._loss_streak.get(sym, 0)
            
            with self.data_lock:
                self.gui_data["coins"][sym] = {
                    "price": curr, "rsi": rsi, "trend_1h": f"{trend_1h}({mem_cnt})",
                    "regime": f"{new_regime}({adx:.0f})",
                    "pos_side": side.upper(), "roe": roe, "entry": entry, "streak": streak
                }

            if self._pos_state[sym]['has']:
                hold = time.time() - float(self._pos_state[sym]['entry_ts'])
                status = self._pos_state[sym]['status']
                is_revenge = self._pos_state[sym].get("is_revenge", False)

                if roe <= -4.5: 
                    self.execute_exit(sym, side, amt, f"하드스탑({roe:.1f}%)", entry, is_partial=False); return
                
                if status == "PARTIAL_SL" and roe <= -3.0:
                    self.execute_exit(sym, side, amt, f"소프트스탑({roe:.1f}%)", entry, is_partial=False); return

                if roe <= -2.5:
                    if (side=="long" and curr < ma20) or (side=="short" and curr > ma20):
                        self.execute_exit(sym, side, amt, f"스마트손절({roe:.1f}%)", entry, is_partial=False); return

                if hold < 30: return 

                if is_revenge:
                    if roe >= 1.0: 
                        self.execute_exit(sym, side, amt * 0.5, f"🐢복수성공({roe:.1f}%)", entry, is_partial=True)
                        self._pos_state[sym]['status'] = "PARTIAL_TP"
                        self._pos_state[sym]['max_roe'] = roe
                        self.save_state(); return
                    
                    if status == "PARTIAL_TP":
                        mx = float(self._pos_state[sym]['max_roe'] or roe)
                        if roe > mx: self._pos_state[sym]['max_roe'] = roe; self.save_state()
                        
                        if (mx - roe) >= 0.3: 
                            self.execute_exit(sym, side, amt, f"🐢복수완료({roe:.1f}%)", entry, is_partial=False); return
                    return 

                if status == "PARTIAL_SL":
                    if roe >= 1.5: 
                        self._pos_state[sym]['status'] = "PARTIAL_TP"
                        self.save_state()
                        return
                    if roe >= 0.5: 
                        self.execute_exit(sym, side, amt, f"본전탈출({roe:.2f}%)", entry, is_partial=False); return
                    if roe <= -2.0:
                        self.execute_exit(sym, side, amt, f"급락방어({roe:.2f}%)", entry, is_partial=False); return

                if hold > 600 and roe < -0.2 and status == "NORMAL":
                    self.execute_exit(sym, side, amt * 0.5, f"분할손절({roe:.2f}%)", entry, is_partial=True)
                    self._pos_state[sym]['status'] = "PARTIAL_SL"
                    self.save_state(); return

                target_roe = 2.0 if new_regime == "TREND" else 1.5
                if roe >= target_roe and status == "NORMAL":
                    self.execute_exit(sym, side, amt * 0.5, f"반익절({roe:.1f}%)", entry, is_partial=True)
                    self._pos_state[sym]['status'] = "PARTIAL_TP"
                    self._pos_state[sym]['max_roe'] = roe
                    self.save_state(); return

                if status == "PARTIAL_TP":
                    mx = float(self._pos_state[sym]['max_roe'] or roe)
                    if roe > mx: self._pos_state[sym]['max_roe'] = roe; self.save_state()
                    
                    trailing_gap = 1.2 if new_regime == "TREND" else 0.5
                    
                    btc_now = self.get_btc_trend(force=False) 
                    if (side == "long" and btc_now == "DOWN") or (side == "short" and btc_now == "UP"):
                         trailing_gap *= 0.5

                    if (mx - roe) >= trailing_gap: 
                        self.execute_exit(sym, side, amt, f"트레일링({roe:.1f}%)", entry, is_partial=False); return

                if hold >= 3600 and abs(roe) < 0.5:
                    self.execute_exit(sym, side, amt, f"타임아웃({roe:.2f}%)", entry, is_partial=False); return
                
                return 

            if side == "none":
                trap = self._trap_history.get(sym)
                if trap and (time.time() - trap['ts'] < 7200): 
                    trap_price = float(trap['price'])
                    trap_side = trap['side']
                    
                    # [Fix 3] Trap 감시 루프에선 API 호출 줄이기 (force=False)
                    current_btc = self.get_btc_trend(force=False)
                    if trap.get('btc_trend') != current_btc:
                        self._trap_history[sym] = None
                        self.log(f"🚫 [{sym}] 복수 취소 (BTC 추세 변경)")
                        return

                    elapsed = time.time() - trap['ts']
                    if elapsed < 900: 
                        if trap_side == "short": 
                            if curr < float(trap.get('extreme_price', 999999)):
                                trap['extreme_price'] = curr
                        elif trap_side == "long": 
                            if curr > float(trap.get('extreme_price', 0)):
                                trap['extreme_price'] = curr
                        self._trap_history[sym] = trap 
                        return 

                    extreme = float(trap.get('extreme_price', trap_price))
                    
                    if trap_side == "short": 
                        if extreme < trap_price * 0.99: 
                            self._trap_history[sym] = None 
                            return
                        if curr > trap_price * 1.0005 and curr < trap_price * 1.005:
                            if is_green and vol_ratio > 1.2:
                                self.execute_entry(sym, "long", "🐢복수(Revenge Long)", candle_ts, boost=False)
                                self._trap_history[sym] = None 
                                return

                    elif trap_side == "long":
                        if extreme > trap_price * 1.01:
                            self._trap_history[sym] = None
                            return
                        if curr < trap_price * 0.9995 and curr > trap_price * 0.995:
                            if is_red and vol_ratio > 1.2:
                                self.execute_entry(sym, "short", "🐢복수(Revenge Short)", candle_ts, boost=False)
                                self._trap_history[sym] = None
                                return

                if streak >= 3: 
                    if new_regime != "RANGE":
                        return 

                rsi_min = self.targets[sym].get("rsi_min", 35) 
                rsi_max = self.targets[sym].get("rsi_max", 65)

                allow_sideways = True if new_regime == "RANGE" else False

                if new_regime == "TREND":
                    is_bu, is_bd = self.check_breakout(df)
                    if not allow_sideways and btc_trend == "SIDEWAYS": return 

                    if trend_1h == "UP" and btc_trend == "UP":
                        if is_bu and trend_4h == "UP" and rsi < 75 and vol_ratio > 1.5 and is_green:
                            self.execute_entry(sym, "long", "추세돌파(Vol)", candle_ts, boost=True)
                        elif (rsi < rsi_min) or (is_whale and trend_1h == "UP"):
                            if is_whale and closed['close'] <= closed['open']: return 
                            if vol_ratio < 0.8 and is_green:
                                self.execute_entry(sym, "long", "추세눌림(Dry)", candle_ts, boost=False)
                    
                    elif trend_1h == "DOWN" and btc_trend == "DOWN":
                        if is_bd and trend_4h == "DOWN" and rsi > 25 and vol_ratio > 1.5 and is_red:
                            self.execute_entry(sym, "short", "추세이탈(Vol)", candle_ts, boost=True)
                        elif rsi >= rsi_max:
                            self.execute_entry(sym, "short", "추세고점", candle_ts, boost=False)

                else:
                    if curr <= lower_band * 1.005 and rsi < rsi_min:
                         if btc_trend == "DOWN": return 
                         if is_green:
                             self.execute_entry(sym, "long", "박스권반등", candle_ts, boost=False)
                    
                    elif curr >= upper_band * 0.995 and rsi > 65: 
                         if btc_trend == "UP": return 
                         if is_red: 
                             self.execute_entry(sym, "short", "박스권저항", candle_ts, boost=False)

        except Exception as e: self.log(f"[!] 로직 에러 ({sym}): {e}")

    # [새로 추가] 시스템 점검 및 테스트 매매 함수
    def run_system_test(self):
        self.log("🔧 [시스템 점검] 가장 저렴한 타겟 코인으로 테스트 매매 시작...")
        
        try:
            # 1. 타겟 코인 중 가장 저렴한 코인 찾기
            cheapest_sym = None
            min_price = float('inf')
            
            for sym in self.symbols:
                ticker = self.safe_call(self.exchange.fetch_ticker, sym)
                price = float(ticker['last'])
                if price < min_price:
                    min_price = price
                    cheapest_sym = sym
            
            if not cheapest_sym:
                raise Exception("시세 조회 실패")

            self.log(f"📉 가장 저렴한 코인 선정: {cheapest_sym} (가격: {min_price})")

            # 2. 최소 주문 수량 계산 (약 6$ 어치 - 바이낸스 최소주문액 5$ 상회)
            cost = 6.0 
            amt = float(self.exchange.amount_to_precision(cheapest_sym, cost / min_price))
            
            # 3. 매수 테스트 (Long)
            self.log(f"🧪 [1/2] 매수 주문 시도 ({amt} {cheapest_sym})...")
            params = {}
            if self.hedge_mode: params["positionSide"] = "LONG"
            self.safe_call(self.exchange.create_market_buy_order, cheapest_sym, amt, params)
            self.log("✅ 매수 성공! (API 권한 정상)")
            
            time.sleep(2.0) # 체결 대기

            # 4. 매도 테스트 (Close)
            self.log(f"🧪 [2/2] 매도(청산) 주문 시도...")
            params = {"reduceOnly": True}
            if self.hedge_mode: params["positionSide"] = "LONG"
            self.safe_call(self.exchange.create_market_sell_order, cheapest_sym, amt, params)
            self.log("✅ 매도 성공! 테스트 완료. (봇 정상 가동)")
            self.log("-" * 30)

        except Exception as e:
            self.log(f"❌ [테스트 실패] 원인: {e}")
            self.log("👉 해결책: 윈도우 시간 동기화(1순위) 또는 API 키 설정을 확인하세요.")
            self.log("🛑 봇을 종료합니다.")
            self.is_running = False

    def run_loop(self):
        self.initialize_account()
        
        # [테스트 실행] 시작하자마자 테스트 실행
        self.run_system_test() 
        if not self.is_running: return # 테스트 실패 시 종료

        self.log(f"🤖 [V7.7.4 Smart Expansion] 매매 시작: {self.symbols}")
        while self.is_running:
            print(".", end="", flush=True)
            self.refresh_all_positions()
            for sym in self.symbols:
                if not self.is_running: break
                self.process_symbol(sym)
                time.sleep(1.0) 
            time.sleep(2.0)
        self.log("⏹ 매매 루프가 중지되었습니다.")

# ---------------------------------------------------------
# [GUI]
# ---------------------------------------------------------
class TradingGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Binance Smart Trader V7.7.4 (Smart Expansion)")
        self.root.geometry("700x800")
        
        self.log_queue = queue.Queue()
        self.bot = BinanceMultiTrader(log_queue=self.log_queue)
        self.bot_thread = None

        style = ttk.Style()
        style.theme_use('clam')
        style.configure("TLabel", font=("Arial", 10))
        style.configure("Header.TLabel", font=("Arial", 12, "bold"))
        style.configure("Big.TLabel", font=("Arial", 14, "bold"))

        header_frame = ttk.Frame(root, padding=10)
        header_frame.pack(fill="x")
        
        ttk.Label(header_frame, text="누적 실현수익:").grid(row=0, column=0, sticky="e")
        self.lbl_accum_pnl = ttk.Label(header_frame, text="0.0 $", style="Big.TLabel", foreground="blue")
        self.lbl_accum_pnl.grid(row=0, column=1, sticky="w", padx=10)

        ttk.Label(header_frame, text="현재 진행손익:").grid(row=0, column=2, sticky="e")
        self.lbl_total_pnl = ttk.Label(header_frame, text="0.0 $", style="Big.TLabel")
        self.lbl_total_pnl.grid(row=0, column=3, sticky="w", padx=10)

        ttk.Label(header_frame, text="총 자산 (USDT):").grid(row=1, column=0, sticky="e", pady=5)
        self.lbl_total_usdt = ttk.Label(header_frame, text="0.0 $", style="Big.TLabel")
        self.lbl_total_usdt.grid(row=1, column=1, sticky="w", padx=10, pady=5)
        
        ttk.Label(header_frame, text="비트코인 추세:").grid(row=1, column=2, sticky="e")
        self.lbl_btc_trend = ttk.Label(header_frame, text="WAIT", style="Big.TLabel", foreground="gray")
        self.lbl_btc_trend.grid(row=1, column=3, sticky="w", padx=10)

        btn_frame = ttk.Frame(root, padding=5)
        btn_frame.pack(fill="x")
        
        self.btn_start = ttk.Button(btn_frame, text="▶ 매매 시작", command=self.start_bot)
        self.btn_start.pack(side="left", expand=True, fill="x", padx=5)
        
        self.btn_stop = ttk.Button(btn_frame, text="⏹ 매매 중지", command=self.stop_bot, state="disabled")
        self.btn_stop.pack(side="left", expand=True, fill="x", padx=5)

        self.coin_frames = {}
        coins_container = ttk.LabelFrame(root, text="코인별 실시간 현황 (V7.7.4)", padding=10)
        coins_container.pack(fill="both", expand=True, padx=10, pady=5)
        
        cols = ["종목", "현재가", "RSI", "추세(Mem)", "장세(ADX)", "포지션", "수익률", "연패"]
        for i, col in enumerate(cols):
            ttk.Label(coins_container, text=col, style="Header.TLabel").grid(row=0, column=i, padx=5, pady=5)

        for idx, sym in enumerate(self.bot.symbols):
            row = idx + 1
            widgets = {}
            ttk.Label(coins_container, text=sym, font=("Arial", 10, "bold")).grid(row=row, column=0, pady=5)
            
            widgets["price"] = ttk.Label(coins_container, text="-")
            widgets["price"].grid(row=row, column=1)
            widgets["rsi"] = ttk.Label(coins_container, text="-")
            widgets["rsi"].grid(row=row, column=2)
            widgets["trend"] = ttk.Label(coins_container, text="-")
            widgets["trend"].grid(row=row, column=3)
            widgets["regime"] = ttk.Label(coins_container, text="-")
            widgets["regime"].grid(row=row, column=4)
            widgets["pos"] = ttk.Label(coins_container, text="NONE", foreground="gray")
            widgets["pos"].grid(row=row, column=5)
            widgets["roe"] = ttk.Label(coins_container, text="0.00 %")
            widgets["roe"].grid(row=row, column=6)
            widgets["streak"] = ttk.Label(coins_container, text="0")
            widgets["streak"].grid(row=row, column=7)
            self.coin_frames[sym] = widgets

        log_container = ttk.LabelFrame(root, text="실행 로그", padding=10)
        log_container.pack(fill="both", expand=True, padx=10, pady=5)
        
        self.var_view_pnl_only = tk.BooleanVar(value=False)
        self.chk_filter = ttk.Checkbutton(log_container, text="💰 수익(청산) 로그만 보기", variable=self.var_view_pnl_only)
        self.chk_filter.pack(anchor="ne")

        self.log_area = scrolledtext.ScrolledText(log_container, height=12, state='disabled', font=("Consolas", 9))
        self.log_area.pack(fill="both", expand=True)

        self.root.after(100, self.process_log_queue)
        self.root.after(1000, self.update_ui)

    def process_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.append_log(msg)
        except queue.Empty: pass
        finally: self.root.after(100, self.process_log_queue)

    def append_log(self, msg):
        if self.var_view_pnl_only.get():
            if not any(k in msg for k in ["💰", "청산", "익절", "손절", "수익", "에러", "[!]"]): return
        try:
            num_lines = float(self.log_area.index('end-1c'))
            if num_lines > 1000:
                self.log_area.config(state='normal')
                self.log_area.delete('1.0', '2.0')
        except: pass
        now = datetime.now().strftime("[%H:%M:%S] ")
        self.log_area.config(state='normal')
        self.log_area.insert(tk.END, now + str(msg) + "\n")
        self.log_area.see(tk.END)
        self.log_area.config(state='disabled')

    def start_bot(self):
        if not self.bot.is_running:
            self.bot.is_running = True
            self.btn_start.config(state="disabled")
            self.btn_stop.config(state="normal")
            self.bot_thread = threading.Thread(target=self.bot.run_loop, daemon=True)
            self.bot_thread.start()

    def stop_bot(self):
        if self.bot.is_running:
            self.bot.is_running = False
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")
            self.log_queue.put("⏹ 중지 요청됨... (대기)")

    def update_ui(self):
        try:
            with self.bot.data_lock:
                data = self.bot.gui_data.copy()
            self.lbl_total_usdt.config(text=f"{data['total_usdt']:.2f} $")
            accum = data['accumulated_profit']
            self.lbl_accum_pnl.config(text=f"{accum:+.2f} $", foreground="blue" if accum >= 0 else "red")
            pnl = data['total_unrealized_pnl']
            color = "green" if pnl > 0 else "red" if pnl < 0 else "black"
            self.lbl_total_pnl.config(text=f"{pnl:+.2f} $", foreground=color)
            btc = data.get("btc_trend", "WAIT")
            self.lbl_btc_trend.config(text=btc)
            if "UP" in btc: self.lbl_btc_trend.config(foreground="green")
            elif "DOWN" in btc: self.lbl_btc_trend.config(foreground="red")
            else: self.lbl_btc_trend.config(foreground="gray")
            for sym, widgets in self.coin_frames.items():
                coin_data = data["coins"].get(sym, {})
                widgets["price"].config(text=f"{coin_data.get('price', 0):.4f}")
                widgets["rsi"].config(text=f"{coin_data.get('rsi', 0):.1f}")
                widgets["trend"].config(text=coin_data.get('trend_1h', '-'))
                
                regime = coin_data.get('regime', '-')
                widgets["regime"].config(text=regime)
                if "TREND" in regime: widgets["regime"].config(foreground="orange")
                elif "RANGE" in regime: widgets["regime"].config(foreground="blue")
                
                pos = coin_data.get('pos_side', 'NONE')
                widgets["pos"].config(text=pos)
                if pos == "LONG": widgets["pos"].config(foreground="green")
                elif pos == "SHORT": widgets["pos"].config(foreground="red")
                else: widgets["pos"].config(foreground="gray")
                roe = coin_data.get('roe', 0.0)
                widgets["roe"].config(text=f"{roe:+.2f} %")
                if roe > 0: widgets["roe"].config(foreground="green")
                elif roe < 0: widgets["roe"].config(foreground="red")
                else: widgets["roe"].config(foreground="black")
                
                streak = coin_data.get('streak', 0)
                widgets["streak"].config(text=str(streak))
                if streak >= 2: widgets["streak"].config(foreground="red")
                else: widgets["streak"].config(foreground="black")

        except: pass
        self.root.after(1000, self.update_ui)

if __name__ == "__main__":
    root = tk.Tk()
    app = TradingGUI(root)
    root.mainloop()