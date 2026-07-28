"""
ذخیره‌سازی ساده و سبک (JSON روی دیسک) برای ربط دادن آیدی تلگرام کاربرای عادی
به یوزرنیم VPN‌شون، و ثبت آخرین باری که «شانس هفتگی» رو زدن.

نکته: اگه سرویس بات روی Railway یک Volume نداشته باشه، این فایل با هر Redeploy
از نو (خالی) ساخته می‌شه. برای کاربرد فعلی (یه جایزه‌ی کوچیک هفتگی) ریسک بالایی
نداره، ولی اگه خواستی کاملاً پایدار بمونه، دقیقاً مثل کاری که برای پنل کردیم،
یه Volume به همین سرویس وصل کن و DATA_FILE رو به مسیر همون Volume اشاره بده.
"""
import asyncio
import json
import os

DATA_FILE = os.getenv("CUSTOMER_DATA_FILE", "customer_data.json")
_lock = asyncio.Lock()


def _load() -> dict:
    if not os.path.exists(DATA_FILE):
        return {"users": {}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"users": {}}


def _save(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def get_registration(telegram_id: int) -> dict | None:
    async with _lock:
        data = _load()
        return data["users"].get(str(telegram_id))


async def find_owner_of_username(username: str) -> int | None:
    """اگه یه یوزرنیم از قبل به یه آیدی تلگرام دیگه وصل شده، همون آیدی رو برمی‌گردونه."""
    async with _lock:
        data = _load()
        for tid, info in data["users"].items():
            if info.get("username") == username:
                return int(tid)
        return None


async def register(telegram_id: int, username: str) -> bool:
    async with _lock:
        data = _load()
        for tid, info in data["users"].items():
            if info.get("username") == username and tid != str(telegram_id):
                return False
        data["users"][str(telegram_id)] = {"username": username, "last_spin": None}
        _save(data)
        return True


async def unregister(telegram_id: int):
    async with _lock:
        data = _load()
        data["users"].pop(str(telegram_id), None)
        _save(data)


async def get_last_spin(telegram_id: int) -> str | None:
    async with _lock:
        data = _load()
        info = data["users"].get(str(telegram_id))
        return info.get("last_spin") if info else None


async def set_last_spin(telegram_id: int, iso_time: str):
    async with _lock:
        data = _load()
        if str(telegram_id) in data["users"]:
            data["users"][str(telegram_id)]["last_spin"] = iso_time
            _save(data)
