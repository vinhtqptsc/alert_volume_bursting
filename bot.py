from vnstock import Market
from datetime import datetime, timedelta
import requests
import os

# ====================== CẤU HÌNH ======================
WATCHLIST = ["CTS", "FTS", "SSI", "VIX", "VND", "HCM", "BSI", "NKG", "HSG"]
VOLUME_MULTIPLIER = 1.5

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
# ======================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  Chưa có TELEGRAM_TOKEN hoặc TELEGRAM_CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, data=payload, timeout=15)
        print("✅ Đã gửi Telegram" if r.status_code == 200 else f"❌ Lỗi Telegram: {r.text}")
    except Exception as e:
        print(f"❌ Lỗi gửi Telegram: {e}")

def get_data(symbol):
    try:
        mkt = Market()
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
        df = mkt.equity(symbol).ohlcv(start=start, end=end)
        if df is None or len(df) < 15:
            return None
        df.columns = [c.lower() for c in df.columns]
        return df
    except Exception as e:
        print(f"❌ Lỗi {symbol}: {e}")
        return None

def check_volume_spike(symbol):
    df = get_data(symbol)
    if df is None:
        return None
    recent = df.tail(21).copy()
    if len(recent) < 15:
        return None
    last = recent.iloc[-1]
    prev = recent.iloc[:-1]
    avg_volume = prev["volume"].mean()
    current_volume = last["volume"]
    volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0
    prev_close = recent.iloc[-2]["close"]
    change_pct = (last["close"] - prev_close) / prev_close * 100
    return {
        "symbol": symbol,
        "price": round(float(last["close"]), 2),
        "volume": int(current_volume),
        "volume_ratio": round(volume_ratio, 2),
        "change_pct": round(change_pct, 2),
        "is_spike": volume_ratio >= VOLUME_MULTIPLIER
    }

def main():
    print(f"🔍 Quét volume - {datetime.now().strftime('%H:%M %d/%m/%Y')}")
    print(f"Watchlist: {', '.join(WATCHLIST)}")
    print("-" * 60)

    signals = []
    for symbol in WATCHLIST:
        res = check_volume_spike(symbol)
        if res is None:
            continue
        status = "🔥" if res["is_spike"] else "  "
        print(f"{status} {res['symbol']:5} | {res['price']:>7.2f} | x{res['volume_ratio']:.1f} | {res['change_pct']:+.1f}%")
        if res["is_spike"]:
            signals.append(res)

    print("-" * 60)

    if signals:
        msg = f"<b>🔥 Volume đột biến ({datetime.now().strftime('%d/%m %H:%M')})</b>\n\n"
        for s in signals:
            msg += f"• <b>{s['symbol']}</b>  x{s['volume_ratio']}  |  {s['price']}  ({s['change_pct']:+.1f}%)\n"
        send_telegram(msg)
        print(f"✅ Đã gửi {len(signals)} tín hiệu")
    else:
        print("Không có mã đạt điều kiện.")

if __name__ == "__main__":
    main()
