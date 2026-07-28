import os

# --- Telegram ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# چند ادمین: با کاما جدا کن، مثلاً "111111,222222,333333"
# (اسم قدیمی ADMIN_ID هم برای سازگاری با قبل پشتیبانی می‌شه)
_raw_admin_ids = os.getenv("ADMIN_IDS", "") or os.getenv("ADMIN_ID", "")
ADMIN_IDS = {
    int(x.strip()) for x in _raw_admin_ids.split(",") if x.strip()
}

# --- Marzban panel ---
# Example: https://your-panel-service.up.railway.app
MARZBAN_URL = os.getenv("MARZBAN_URL", "").rstrip("/")
MARZBAN_USERNAME = os.getenv("MARZBAN_USERNAME", "")
MARZBAN_PASSWORD = os.getenv("MARZBAN_PASSWORD", "")

# ---------- لوکیشن‌ها ----------
# نام نمایشی → لیست تگ اینباندها (دقیقاً مطابق کانفیگ core که دادی)
# اگه اسم لوکیشن‌ها رو می‌خوای عوض کنی فقط اینجا تغییر بده
LOCATIONS = {
    "لوکیشن ۱ (پورت ۴۴۳)": ["VLESS-WS"],
    "لوکیشن ۲ (پورت ۸۴۴۳)": ["VLESS-WS-2"],
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN تنظیم نشده است")
if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS (یا ADMIN_ID) تنظیم نشده است")
if not MARZBAN_URL or not MARZBAN_USERNAME or not MARZBAN_PASSWORD:
    raise RuntimeError("متغیرهای اتصال به مرزبان (MARZBAN_URL/USERNAME/PASSWORD) کامل نیستند")
