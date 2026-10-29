"""Volume Alert Bot — bản vá thu hẹp, ưu tiên KHÔNG BỎ SÓT cảnh báo.

Giữ nguyên: watchlist, ngưỡng 1.5 (chưa làm tròn), tail(21) / tối thiểu 15 nến,
công thức giá/thay đổi, cron, tên secret.
Sửa: giờ Việt Nam, khung 1D tường minh, xác nhận Telegram thật sự (ok + message_id),
lỗi một mã không làm mất cảnh báo các mã khác, thử lại khi lỗi tạm thời.
Chống lặp (tùy chọn, fail-open): chỉ báo lại một mã trong cùng ngày khi tỉ lệ volume
tăng thêm >= 50% so với lần báo trước. Trạng thái nhỏ, chỉ của hôm nay, lưu ở file
(Actions cache), không cần quyền ghi repo. Thiếu/hỏng/không ghi được => vẫn báo như thường.
"""
import json
import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

WATCHLIST = ["CTS", "FTS", "SSI", "VIX", "VND", "HCM", "BSI", "NKG", "HSG"]
VOLUME_MULTIPLIER = 1.5
WINDOW = 21          # như bản gốc: tail(21)
MIN_BARS = 15        # như bản gốc: tối thiểu 15 dòng
INTERVAL = "1D"      # giá trị mặc định đã được tài liệu mô tả, nay truyền tường minh
VN = timezone(timedelta(hours=7), "VN")  # Việt Nam không có giờ mùa hè
MAX_TELEGRAM_CHARS = 4000
SEND_ATTEMPTS = 3
FETCH_ATTEMPTS = 2
REALERT_FACTOR = 1.5  # báo lại cùng mã trong ngày khi ratio >= 1.5 x ratio đã báo


class DataError(Exception):
    """Lỗi dữ liệu của một mã (thông điệp tự viết, không chứa bí mật)."""


# ---------------------------------------------------------------- dữ liệu
def bar_date(value):
    """Ngày nến theo giờ VN. Chấp nhận chuỗi, datetime, pandas Timestamp, date."""
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, str):
        value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(VN)
        return value.date()
    if isinstance(value, date):
        return value
    raise DataError("không đọc được ngày nến")


def to_float(value):
    if isinstance(value, bool):
        raise DataError("số liệu không hợp lệ")
    number = float(value)
    if not math.isfinite(number):
        raise DataError("số liệu không hữu hạn")
    return number


def analyze(symbol, rows, today):
    """Cùng chiến lược bản gốc. Trả về kết quả, hoặc {'stale': True} nếu chưa có nến hôm nay."""
    bars = {}
    for row in rows or []:
        try:
            when = bar_date(row.get("time", row.get("date", row.get("index"))))
            close, volume = to_float(row["close"]), to_float(row["volume"])
        except (DataError, KeyError, ValueError, TypeError, AttributeError):
            continue  # một dòng cũ hỏng không được làm mất cả mã
        if close <= 0 or volume < 0:
            continue
        bars[when] = (close, volume)
    series = sorted(bars.items())
    if len(series) < MIN_BARS:
        raise DataError(f"chỉ có {len(series)} nến hợp lệ (cần >= {MIN_BARS})")
    last_date, (last_close, last_volume) = series[-1]
    if last_date > today:
        raise DataError("nến có ngày ở tương lai")
    if last_date != today:
        return {"symbol": symbol, "stale": True, "last_date": last_date.isoformat()}
    recent = series[-WINDOW:]
    prev = recent[:-1]
    avg_volume = sum(v for _, (_, v) in prev) / len(prev)
    ratio = last_volume / avg_volume if avg_volume > 0 else 0
    prev_close = prev[-1][1][0]
    change_pct = (last_close - prev_close) / prev_close * 100
    return {
        "symbol": symbol, "stale": False,
        "price": round(last_close, 2), "volume": int(last_volume),
        "volume_ratio": round(ratio, 2), "change_pct": round(change_pct, 2),
        "is_spike": ratio >= VOLUME_MULTIPLIER,  # so sánh giá trị CHƯA làm tròn
        "baseline_bars": len(prev),
    }


def fetch_rows(symbol, now):
    """Giữ lời gọi Market().equity(...).ohlcv như bản gốc."""
    from vnstock import Market
    api = Market().equity(symbol)
    start = (now - timedelta(days=45)).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")
    try:
        df = api.ohlcv(start=start, end=end, interval=INTERVAL)
    except TypeError:  # bản vnstock cũ chưa nhận tham số interval (mặc định vốn là 1D)
        df = api.ohlcv(start=start, end=end)
    if df is None or len(df) == 0:
        raise DataError("nguồn không trả dữ liệu")
    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]
    interval = (getattr(df, "attrs", None) or {}).get("interval")
    if interval is not None and str(interval).upper() not in ("1D", "D", "1DAY"):
        raise DataError("nguồn trả khung nến khác 1D")
    if "time" not in df.columns and "date" not in df.columns:
        df = df.reset_index()
        df.columns = [str(c).lower() for c in df.columns]
    return df.to_dict("records")


def scan(fetch, now, sleep=time.sleep, log=print):
    today = now.astimezone(VN).date()
    results, stale, failures = [], [], []
    for symbol in WATCHLIST:
        outcome, reason = None, ""
        for attempt in range(FETCH_ATTEMPTS):
            try:
                outcome = analyze(symbol, fetch(symbol, now), today)
                break
            except DataError as exc:
                reason = str(exc)
            except Exception as exc:  # không in nội dung: có thể chứa URL/khóa
                reason = type(exc).__name__
            if attempt < FETCH_ATTEMPTS - 1:
                sleep(2)
        if outcome is None:
            failures.append(symbol)
            log(f"❌ Lỗi {symbol}: {reason}")
        elif outcome["stale"]:
            stale.append(symbol)
            log(f"ℹ️  {symbol}: chưa có nến hôm nay (nến cuối {outcome['last_date']})")
        else:
            results.append(outcome)
    return results, stale, failures


# ------------------------------------------------------- chống lặp (fail-open)
def load_state(path, today_iso, log=print):
    """{mã: ratio đã báo hôm nay}. Mọi sự cố => {} (nghĩa là cứ báo)."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("date") != today_iso:
            return {}
        last = {}
        for symbol, ratio in data.get("last", {}).items():
            if symbol in WATCHLIST and not isinstance(ratio, bool) and isinstance(ratio, (int, float)) \
                    and math.isfinite(ratio) and ratio > 0:
                last[symbol] = float(ratio)
        return last
    except FileNotFoundError:
        return {}
    except Exception:
        log("⚠️ Không đọc được trạng thái chống lặp — coi như chưa báo (vẫn gửi)")
        return {}


def save_state(path, today_iso, last, log=print):
    if not path:
        return
    try:
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump({"date": today_iso, "last": last}, handle, sort_keys=True)
        os.replace(temp, path)
    except Exception:
        log("⚠️ Không ghi được trạng thái chống lặp — lượt sau có thể báo lại (không mất tin)")


def split_new(signals, last):
    """Tách tín hiệu cần gửi (mới, hoặc ratio tăng >= 50%) và tín hiệu đã báo."""
    fresh, suppressed = [], []
    for s in signals:
        previous = last.get(s["symbol"])
        if previous is None or s["volume_ratio"] >= round(previous * REALERT_FACTOR, 2):
            fresh.append(dict(s, prev_ratio=previous))
        else:
            suppressed.append(s)
    return fresh, suppressed


# --------------------------------------------------------------- Telegram
def format_message(signals, failures, now):
    now = now.astimezone(VN)
    msg = f"<b>🔥 Volume đột biến ({now:%d/%m %H:%M} VN)</b>\n"
    msg += "Khung 1D; nến ngày hôm nay có thể chưa đóng.\n\n"
    for s in signals:
        msg += (f"• <b>{s['symbol']}</b>  x{s['volume_ratio']}  |  {s['price']}  "
                f"({s['change_pct']:+.1f}%)")
        if s.get("prev_ratio"):
            msg += f"  ↑ tăng từ x{s['prev_ratio']:.1f}"
        msg += "\n"
    if failures:
        msg += "\n⚠️ Không kiểm tra được: " + ", ".join(failures)
    return msg


def split_message(text, limit=MAX_TELEGRAM_CHARS):
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if current and len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks or [text]


def send_telegram(post, token, chat_id, text, sleep=time.sleep, log=print):
    """True chỉ khi HTTP 200 + ok:true + message_id nguyên dương. Không bao giờ in URL/token."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    attempt = 0
    while attempt < SEND_ATTEMPTS:
        attempt += 1
        status, data = None, None
        try:
            response = post(url, data=payload, timeout=15)
            status = response.status_code
            try:
                data = response.json()
            except Exception:
                data = None
        except Exception:
            pass  # lỗi mạng: thử lại
        if status == 200 and isinstance(data, dict) and data.get("ok") is True:
            result = data.get("result")
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if type(message_id) is int and message_id > 0:
                return True
        log(f"⚠️ Telegram lần {attempt}/{SEND_ATTEMPTS}: HTTP {status}")
        description = str(data.get("description", "")) if isinstance(data, dict) else ""
        if status == 400 and "parse" in description.lower() and "parse_mode" in payload:
            # Lỗi định dạng HTML: gửi lại dạng chữ thường, không tính là một lần thử.
            payload = {"chat_id": chat_id, "text": re.sub(r"<[^>]+>", "", text)}
            attempt -= 1
            continue
        if status in (400, 401, 403, 404):
            return False  # lỗi cấu hình/vĩnh viễn: thử lại vô ích
        if attempt < SEND_ATTEMPTS:
            wait = 2 * attempt
            if status == 429 and isinstance(data, dict):
                try:
                    wait = min(int(data.get("parameters", {}).get("retry_after", wait)), 30)
                except (TypeError, ValueError, AttributeError):
                    pass
            sleep(wait)
    return False


def deliver(post, token, chat_id, text, sleep=time.sleep, log=print):
    ok = True
    for chunk in split_message(text):
        ok = send_telegram(post, token, chat_id, chunk, sleep, log) and ok
    return ok


# ------------------------------------------------------------------- chạy
def run(env, fetch, post, now, sleep=time.sleep, log=print):
    token, chat_id = env.get("TELEGRAM_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log("❌ Thiếu TELEGRAM_TOKEN hoặc TELEGRAM_CHAT_ID — không thể gửi cảnh báo")
        return 1
    now = now.astimezone(VN)
    log(f"🔍 Quét volume - {now:%H:%M %d/%m/%Y} VN (UTC+7), khung {INTERVAL}")
    log(f"Watchlist: {', '.join(WATCHLIST)}")
    log("-" * 60)
    results, stale, failures = scan(fetch, now, sleep, log)
    for r in results:
        flag = "🔥" if r["is_spike"] else "  "
        log(f"{flag} {r['symbol']:5} | {r['price']:>7.2f} | x{r['volume_ratio']:.1f} | {r['change_pct']:+.1f}%")
    log("-" * 60)
    signals = [r for r in results if r["is_spike"]]
    state_path = env.get("VOLUME_ALERT_STATE")
    today_iso = now.date().isoformat()
    last = load_state(state_path, today_iso, log)
    fresh, suppressed = split_new(signals, last)
    for s in suppressed:
        log(f"⏭️  {s['symbol']}: đã báo x{last[s['symbol']]:.1f}, hiện x{s['volume_ratio']} — chưa tăng đủ 50%")

    if len(failures) == len(WATCHLIST):
        notice = f"⚠️ Volume Alert ({now:%d/%m %H:%M} VN): không lấy được dữ liệu cả {len(WATCHLIST)} mã."
        deliver(post, token, chat_id, notice, sleep, log)  # cố gắng báo lỗi, không bắt buộc
        return 1
    if fresh:
        if deliver(post, token, chat_id, format_message(fresh, failures, now), sleep, log):
            log(f"✅ Telegram xác nhận: đã gửi {len(fresh)} tín hiệu")
            # Chỉ ghi nhớ SAU khi Telegram xác nhận; gửi hỏng => lượt sau vẫn báo.
            last.update({s["symbol"]: s["volume_ratio"] for s in fresh})
            save_state(state_path, today_iso, last, log)
            return 0
        log("❌ Gửi Telegram thất bại sau khi thử lại")
        return 1
    if failures:
        log("❌ Có mã không kiểm tra được và không có tín hiệu mới ở các mã còn lại: " + ", ".join(failures))
        return 1
    if suppressed:
        log(f"Có {len(suppressed)} mã vượt ngưỡng nhưng đã báo, chưa tăng đủ 50% — không gửi lại.")
        return 0
    if stale and not results:
        log("Chưa có nến ngày hôm nay cho mã nào (ngày nghỉ hoặc dữ liệu chưa cập nhật) — không phải 'không có tín hiệu'.")
        return 0
    log("Không có mã đạt điều kiện.")
    return 0


def main():
    import requests
    return run(os.environ, fetch_rows, requests.post, datetime.now(VN))


if __name__ == "__main__":
    sys.exit(main())
