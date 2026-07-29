from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import LOCATIONS


def main_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="افزودن کاربر", callback_data="add_user"))
    b.row(InlineKeyboardButton(text="لیست کاربران", callback_data="list_users:0"))
    b.row(InlineKeyboardButton(text="وضعیت سیستم", callback_data="system_stats"))
    return b.as_markup()


def users_list_kb(users: list[dict], offset: int, has_more: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for u in users:
        dot = "🟢" if u.get("online") else "⚪️"
        status_icon = "" if u.get("status") == "active" else " ⛔️"
        b.row(InlineKeyboardButton(
            text=f"{dot} {u['username']}{status_icon}",
            callback_data=f"user:{u['username']}"
        ))
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="قبلی", callback_data=f"list_users:{max(offset-10,0)}"))
    if has_more:
        nav.append(InlineKeyboardButton(text="بعدی", callback_data=f"list_users:{offset+10}"))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="منوی اصلی", callback_data="main_menu"))
    return b.as_markup()


def user_detail_kb(username: str, status: str = "active") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="افزودن روز", callback_data=f"extend:{username}"),
        InlineKeyboardButton(text="افزودن حجم", callback_data=f"adddata:{username}"),
    )
    b.row(
        InlineKeyboardButton(text="➖ کم‌کردن روز", callback_data=f"reduceday:{username}"),
        InlineKeyboardButton(text="➖ کم‌کردن حجم", callback_data=f"reducedata:{username}"),
    )
    b.row(
        InlineKeyboardButton(text="ریست مصرف", callback_data=f"reset:{username}"),
        InlineKeyboardButton(text="لینک اشتراک", callback_data=f"link:{username}"),
    )
    b.row(
        InlineKeyboardButton(text="کپی همه کانفیگ‌ها", callback_data=f"copyconfigs:{username}")
    )
    status_toggle = "غیرفعال‌سازی" if status == "active" else "فعال‌سازی"
    b.row(InlineKeyboardButton(text=status_toggle, callback_data=f"toggle:{username}"))
    b.row(InlineKeyboardButton(text="ویرایش لوکیشن", callback_data=f"editloc:{username}"))
    b.row(InlineKeyboardButton(text="حذف کاربر", callback_data=f"delete:{username}"))
    b.row(InlineKeyboardButton(text="لیست کاربران", callback_data="list_users:0"))
    return b.as_markup()


def location_kb(selected: set[str] | None = None, confirm_cb: str = "loc_confirm") -> InlineKeyboardMarkup:
    if selected is None:
        selected = set()
    b = InlineKeyboardBuilder()
    for name in LOCATIONS:
        icon = "✅ " if name in selected else ""
        b.row(InlineKeyboardButton(
            text=f"{icon}{name}",
            callback_data=f"loc_toggle:{name}"
        ))
    b.row(InlineKeyboardButton(text="✅ تأیید انتخاب", callback_data=confirm_cb))
    b.row(InlineKeyboardButton(text="لغو", callback_data="main_menu"))
    return b.as_markup()


def confirm_kb(action: str, username: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="بله", callback_data=f"confirm:{action}:{username}"),
        InlineKeyboardButton(text="نه", callback_data=f"user:{username}"),
    )
    return b.as_markup()


def cancel_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="لغو", callback_data="main_menu"))
    return b.as_markup()
