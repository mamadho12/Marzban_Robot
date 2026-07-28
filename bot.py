import asyncio
import functools
import logging
import datetime
import json
import base64
import re

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, BufferedInputFile

from config import BOT_TOKEN, ADMIN_IDS, MARZBAN_URL, LOCATIONS
from marzban_api import marzban, MarzbanError
from keyboards import (
    main_menu_kb, users_list_kb, user_detail_kb,
    confirm_kb, cancel_kb, location_kb,
)
from states import AddUser, ExtendUser, AddDataUser, EditLocation

logging.basicConfig(level=logging.INFO)

router = Router()


def admin_only(handler):
    @functools.wraps(handler)
    async def wrapper(event, *args, **kwargs):
        user_id = event.from_user.id
        if user_id not in ADMIN_IDS:
            if isinstance(event, Message):
                await event.answer("⛔️ این ربات خصوصیه و فقط ادمین بهش دسترسی داره.")
            else:
                await event.answer("⛔️ دسترسی نداری", show_alert=True)
            return
        return await handler(event, *args, **kwargs)
    return wrapper


STATUS_FA = {
    "active": "✅ فعال",
    "disabled": "⛔️ غیرفعال",
    "limited": "🚫 اتمام حجم",
    "expired": "⌛️ منقضی‌شده",
}

ONLINE_THRESHOLD_SECONDS = 180


def fmt_bytes(n: int) -> str:
    if not n:
        return "0GB"
    gb = n / (1024 ** 3)
    return f"{gb:.2f}GB"


def fmt_expire(ts) -> str:
    if not ts:
        return "نامحدود"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _parse_online_at(online_at):
    if not online_at:
        return None
    if isinstance(online_at, datetime.datetime):
        dt = online_at
    else:
        try:
            s = str(online_at).replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(s)
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _relative_time_fa(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return "چند لحظه پیش"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} دقیقه پیش"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ساعت پیش"
    days = hours // 24
    return f"{days} روز پیش"


def is_online(user: dict) -> bool:
    dt = _parse_online_at(user.get("online_at"))
    if not dt:
        return False
    delta = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds()
    return 0 <= delta < ONLINE_THRESHOLD_SECONDS


def online_status_line(user: dict) -> str:
    dt = _parse_online_at(user.get("online_at"))
    if not dt:
        return "🔴 اتصال: هرگز وصل نشده"
    delta = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds()
    if 0 <= delta < ONLINE_THRESHOLD_SECONDS:
        return "🟢 اتصال: آنلاین"
    return f"⚪️ آخرین اتصال: {_relative_time_fa(delta)}"


def progress_bar(used: int, limit: int, length: int = 10) -> str:
    if not limit or limit <= 0:
        return ""
    ratio = min(used / limit, 1.0)
    filled = round(ratio * length)
    bar = "▓" * filled + "░" * (length - filled)
    pct = int(ratio * 100)
    return f"<code>[{bar}] {pct}%</code>"


def get_user_locations(user: dict) -> list[str]:
    user_inbounds = user.get("inbounds") or {}
    tags = set()
    for tag_list in user_inbounds.values():
        tags.update(tag_list)

    result = []
    for name, loc_tags in LOCATIONS.items():
        if any(t in tags for t in loc_tags):
            result.append(name)
    return result


def detect_location_from_config(config_line: str) -> str | None:
    """از روی کانفیگ تشخیص می‌ده مال کدوم لوکیشنه (بر اساس path یا port)"""
    line = config_line.lower()
    # اول path رو چک می‌کنیم (مطابق کانفیگ core)
    if "path=%2fvless2" in line or "path=/vless2" in line or "path=%2Fvless2" in line:
        return "🇳🇱 لوکیشن هلند"
    if "path=%2fvless" in line or "path=/vless" in line or "path=%2Fvless" in line:
        return "🇺🇸 لوکیشن آمریکا"
    # fallback روی پورت
    if ":8443" in line:
        return "🇳🇱 لوکیشن هلند"
    if ":443" in line:
        return "🇺🇸 لوکیشن آمریکا"
    return None


async def extract_configs_by_location(user: dict) -> dict[str, list[str]]:
    """
    کانفیگ‌های کاربر رو از لینک اشتراک می‌گیره و بر اساس لوکیشن دسته‌بندی می‌کنه.
    """
    result = {name: [] for name in LOCATIONS}

    sub_url = user.get("subscription_url", "")
    if not sub_url:
        return result

    content = await marzban.fetch_subscription_content(sub_url)
    if not content:
        return result

    # تلاش برای دیکود base64 (اگه لازم باشه)
    try:
        # بعضی وقتا محتوای اشتراک خودش base64 هست
        decoded = base64.b64decode(content.strip() + "==").decode("utf-8", errors="ignore")
        if "vless://" in decoded or "vmess://" in decoded or "trojan://" in decoded:
            content = decoded
    except Exception:
        pass

    lines = [l.strip() for l in content.replace("\r", "").split("\n") if l.strip()]
    
    for line in lines:
        if not (line.startswith("vless://") or line.startswith("vmess://") or line.startswith("trojan://")):
            continue

        loc = detect_location_from_config(line)
        if loc and loc in result:
            result[loc].append(line)
        else:
            # اگه نتونستیم لوکیشن رو تشخیص بدیم، به اولین لوکیشن فعال کاربر می‌دیم
            user_locs = get_user_locations(user)
            if user_locs:
                result[user_locs[0]].append(line)

    return result


async def user_summary(user: dict) -> str:
    username = user.get("username", "-")
    status = user.get("status", "-")
    status_fa = STATUS_FA.get(status, status)

    used = user.get("used_traffic", 0) or 0
    limit = user.get("data_limit", 0) or 0
    used_str = fmt_bytes(used)
    limit_str = fmt_bytes(limit) if limit else "نامحدود"
    bar = progress_bar(used, limit)

    expire = fmt_expire(user.get("expire"))

    locations = get_user_locations(user)
    loc_text = " + ".join(locations) if locations else "هیچکدام"

    sub_url = user.get("subscription_url", "")
    if sub_url and sub_url.startswith("/"):
        sub_url = MARZBAN_URL + sub_url

    lines = [
        f"👤 <b>{username}</b>  —  {status_fa}",
        online_status_line(user),
        "",
        "📊 مصرف:",
        f"<code>{used_str} / {limit_str}</code>",
    ]
    if bar:
        lines.append(bar)
    lines += [
        "",
        f"📅 انقضا:  <code>{expire}</code>",
        f"📍 لوکیشن‌ها:  <b>{loc_text}</b>",
    ]

        # ---------- کانفیگ‌ها ----------
    try:
        configs_by_loc = await extract_configs_by_location(user)
        has_any = any(configs_by_loc.values())
        if has_any:
            lines.append("")
            lines.append("📄 <b>کانفیگ‌ها:</b>")
            for loc_name, confs in configs_by_loc.items():
                if not confs:
                    continue
                lines.append(f"\n{loc_name}:")
                for conf in confs:
                    # کانفیگ کامل نشون داده می‌شه (بدون کوتاه کردن)
                    lines.append(f"<code>{conf}</code>")
    except Exception:
        pass

    if sub_url:
        lines += ["", "🔗 لینک اشتراک:", f"<code>{sub_url}</code>"]

    return "\n".join(lines)


# ---------- شروع و منو ----------

MAIN_MENU_TEXT = "🛡 <b>پنل مدیریت مرزبان</b>\nیکی از گزینه‌ها رو انتخاب کن:"


@router.message(Command("start"))
@admin_only
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(f"سلام! 👋\n\n{MAIN_MENU_TEXT}", reply_markup=main_menu_kb(), parse_mode="HTML")


@router.callback_query(F.data == "main_menu")
@admin_only
async def cb_main_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(MAIN_MENU_TEXT, reply_markup=main_menu_kb(), parse_mode="HTML")
    await call.answer()


# ---------- افزودن کاربر ----------

@router.callback_query(F.data == "add_user")
@admin_only
async def cb_add_user(call: CallbackQuery, state: FSMContext):
    await state.set_state(AddUser.username)
    await call.message.edit_text(
        "نام کاربری جدید رو بفرست (فقط حروف انگلیسی و عدد، بدون فاصله):",
        reply_markup=cancel_kb(),
    )
    await call.answer()


@router.message(AddUser.username)
@admin_only
async def add_user_username(message: Message, state: FSMContext):
    username = message.text.strip()
    if not username.replace("_", "").isalnum():
        await message.answer("نام کاربری نامعتبره، دوباره امتحان کن (فقط حروف/عدد/آندرلاین):")
        return
    await state.update_data(username=username)
    await state.set_state(AddUser.data_limit)
    await message.answer("حجم مصرفی رو به گیگابایت بفرست (برای نامحدود عدد 0 بفرست):")


@router.message(AddUser.data_limit)
@admin_only
async def add_user_data_limit(message: Message, state: FSMContext):
    try:
        gb = float(message.text.strip())
    except ValueError:
        await message.answer("لطفاً یک عدد معتبر بفرست (مثلاً 30 یا 0):")
        return
    await state.update_data(data_limit=gb)
    await state.set_state(AddUser.expire_days)
    await message.answer("مدت اعتبار رو به روز بفرست (برای نامحدود عدد 0 بفرست):")


@router.message(AddUser.expire_days)
@admin_only
async def add_user_expire(message: Message, state: FSMContext):
    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer("لطفاً یک عدد صحیح بفرست (مثلاً 30 یا 0):")
        return

    await state.update_data(expire_days=days)
    await state.set_state(AddUser.locations)
    await state.update_data(selected_locations=set())

    await message.answer(
        "لوکیشن‌های مورد نظر رو انتخاب کن (می‌تونی یکی یا هر دو رو بزنی):",
        reply_markup=location_kb(set(), confirm_cb="loc_confirm_add")
    )


@router.callback_query(F.data.startswith("loc_toggle:"), AddUser.locations)
@admin_only
async def loc_toggle_add(call: CallbackQuery, state: FSMContext):
    name = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected: set = data.get("selected_locations", set())

    if name in selected:
        selected.discard(name)
    else:
        selected.add(name)

    await state.update_data(selected_locations=selected)
    await call.message.edit_reply_markup(
        reply_markup=location_kb(selected, confirm_cb="loc_confirm_add")
    )
    await call.answer()


@router.callback_query(F.data == "loc_confirm_add", AddUser.locations)
@admin_only
async def loc_confirm_add(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = list(data.get("selected_locations", set()))

    if not selected:
        await call.answer("حداقل یک لوکیشن انتخاب کن!", show_alert=True)
        return

    username = data["username"]
    gb = data["data_limit"]
    days = data["expire_days"]
    await state.clear()

    try:
        user = await marzban.create_user(username, gb, days, location_names=selected)
    except MarzbanError as e:
        await call.message.edit_text(f"❌ خطا در ساخت کاربر:\n{e}", reply_markup=main_menu_kb())
        return

    await call.message.edit_text(
        f"✅ کاربر ساخته شد.\n\n{await user_summary(user)}",
        reply_markup=user_detail_kb(username, user.get("status", "active")),
        parse_mode="HTML",
    )
    await call.answer()


# ---------- لیست کاربران ----------

@router.callback_query(F.data.startswith("list_users:"))
@admin_only
async def cb_list_users(call: CallbackQuery, state: FSMContext):
    offset = int(call.data.split(":")[1])
    try:
        users = await marzban.list_users(offset=offset, limit=11)
    except MarzbanError as e:
        await call.message.edit_text(f"❌ خطا:\n{e}", reply_markup=main_menu_kb())
        await call.answer()
        return
    has_more = len(users) > 10
    users = users[:10]
    if not users and offset == 0:
        await call.message.edit_text("هنوز هیچ کاربری ساخته نشده.", reply_markup=main_menu_kb())
        await call.answer()
        return
    rows = [
        {"username": u["username"], "online": is_online(u), "status": u.get("status", "active")}
        for u in users
    ]
    online_count = sum(1 for r in rows if r["online"])
    await call.message.edit_text(
        f"📋 <b>لیست کاربران</b>\n🟢 آنلاین: {online_count} از {len(rows)} کاربر این صفحه",
        reply_markup=users_list_kb(rows, offset, has_more),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("user:"))
@admin_only
async def cb_user_detail(call: CallbackQuery, state: FSMContext):
    username = call.data.split(":", 1)[1]
    try:
        user = await marzban.get_user(username)
    except MarzbanError as e:
        await call.message.edit_text(f"❌ خطا:\n{e}", reply_markup=main_menu_kb())
        await call.answer()
        return
    await call.message.edit_text(
        await user_summary(user),
        reply_markup=user_detail_kb(username, user.get("status", "active")),
        parse_mode="HTML"
    )
    await call.answer()


# ---------- ویرایش لوکیشن ----------

@router.callback_query(F.data.startswith("editloc:"))
@admin_only
async def cb_edit_location(call: CallbackQuery, state: FSMContext):
    username = call.data.split(":", 1)[1]
    try:
        user = await marzban.get_user(username)
    except MarzbanError as e:
        await call.answer(str(e), show_alert=True)
        return

    current = set(get_user_locations(user))
    await state.set_state(EditLocation.select)
    await state.update_data(username=username, selected_locations=current)

    await call.message.edit_text(
        f"ویرایش لوکیشن کاربر <b>{username}</b>\n"
        f"لوکیشن‌های فعلی: {', '.join(current) if current else 'هیچکدام'}\n\n"
        "لوکیشن‌های جدید رو انتخاب کن:",
        reply_markup=location_kb(current, confirm_cb="loc_confirm_edit"),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("loc_toggle:"), EditLocation.select)
@admin_only
async def loc_toggle_edit(call: CallbackQuery, state: FSMContext):
    name = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected: set = data.get("selected_locations", set())

    if name in selected:
        selected.discard(name)
    else:
        selected.add(name)

    await state.update_data(selected_locations=selected)
    await call.message.edit_reply_markup(
        reply_markup=location_kb(selected, confirm_cb="loc_confirm_edit")
    )
    await call.answer()


@router.callback_query(F.data == "loc_confirm_edit", EditLocation.select)
@admin_only
async def loc_confirm_edit(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = list(data.get("selected_locations", set()))
    username = data["username"]

    if not selected:
        await call.answer("حداقل یک لوکیشن انتخاب کن!", show_alert=True)
        return

    await state.clear()

    try:
        user = await marzban.set_user_locations(username, selected)
    except MarzbanError as e:
        await call.message.edit_text(f"❌ خطا:\n{e}", reply_markup=main_menu_kb())
        return

    await call.message.edit_text(
        f"✅ لوکیشن‌ها به‌روز شد.\n\n{await user_summary(user)}",
        reply_markup=user_detail_kb(username, user.get("status", "active")),
        parse_mode="HTML",
    )
    await call.answer()


# ---------- افزودن روز ----------

@router.callback_query(F.data.startswith("extend:"))
@admin_only
async def cb_extend(call: CallbackQuery, state: FSMContext):
    username = call.data.split(":", 1)[1]
    await state.update_data(username=username)
    await state.set_state(ExtendUser.days)
    await call.message.edit_text(f"چند روز به اعتبار «{username}» اضافه بشه؟", reply_markup=cancel_kb())
    await call.answer()


@router.message(ExtendUser.days)
@admin_only
async def extend_days(message: Message, state: FSMContext):
    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer("یک عدد صحیح بفرست:")
        return
    data = await state.get_data()
    username = data["username"]
    await state.clear()
    try:
        user = await marzban.add_days(username, days)
    except MarzbanError as e:
        await message.answer(f"❌ خطا:\n{e}", reply_markup=main_menu_kb())
        return
    await message.answer(
        f"✅ انجام شد.\n\n{await user_summary(user)}",
        reply_markup=user_detail_kb(username, user.get("status", "active")),
        parse_mode="HTML"
    )


# ---------- افزودن حجم ----------

@router.callback_query(F.data.startswith("adddata:"))
@admin_only
async def cb_adddata(call: CallbackQuery, state: FSMContext):
    username = call.data.split(":", 1)[1]
    await state.update_data(username=username)
    await state.set_state(AddDataUser.gb)
    await call.message.edit_text(f"چند گیگابایت به حجم «{username}» اضافه بشه؟", reply_markup=cancel_kb())
    await call.answer()


@router.message(AddDataUser.gb)
@admin_only
async def adddata_gb(message: Message, state: FSMContext):
    try:
        gb = float(message.text.strip())
    except ValueError:
        await message.answer("یک عدد معتبر بفرست:")
        return
    data = await state.get_data()
    username = data["username"]
    await state.clear()
    try:
        user = await marzban.add_data_gb(username, gb)
    except MarzbanError as e:
        await message.answer(f"❌ خطا:\n{e}", reply_markup=main_menu_kb())
        return
    await message.answer(
        f"✅ انجام شد.\n\n{await user_summary(user)}",
        reply_markup=user_detail_kb(username, user.get("status", "active")),
        parse_mode="HTML"
    )


# ---------- ریست مصرف ----------

@router.callback_query(F.data.startswith("reset:"))
@admin_only
async def cb_reset(call: CallbackQuery, state: FSMContext):
    username = call.data.split(":", 1)[1]
    await call.message.edit_text(
        f"مطمئنی می‌خوای مصرف «{username}» صفر بشه؟", reply_markup=confirm_kb("reset", username)
    )
    await call.answer()


# ---------- فعال/غیرفعال ----------

@router.callback_query(F.data.startswith("toggle:"))
@admin_only
async def cb_toggle(call: CallbackQuery, state: FSMContext):
    username = call.data.split(":", 1)[1]
    try:
        user = await marzban.get_user(username)
        new_status = "disabled" if user.get("status") == "active" else "active"
        user = await marzban.modify_user(username, status=new_status)
    except MarzbanError as e:
        await call.message.edit_text(f"❌ خطا:\n{e}", reply_markup=main_menu_kb())
        await call.answer()
        return
    await call.message.edit_text(
        await user_summary(user),
        reply_markup=user_detail_kb(username, user.get("status", "active")),
        parse_mode="HTML"
    )
    await call.answer("انجام شد ✅")


# ---------- حذف ----------

@router.callback_query(F.data.startswith("delete:"))
@admin_only
async def cb_delete(call: CallbackQuery, state: FSMContext):
    username = call.data.split(":", 1)[1]
    await call.message.edit_text(
        f"⚠️ مطمئنی می‌خوای «{username}» رو کامل حذف کنی؟ این کار برگشت‌ناپذیره.",
        reply_markup=confirm_kb("delete", username),
    )
    await call.answer()


# ---------- لینک اشتراک ----------

@router.callback_query(F.data.startswith("link:"))
@admin_only
async def cb_link(call: CallbackQuery, state: FSMContext):
    username = call.data.split(":", 1)[1]
    try:
        user = await marzban.get_user(username)
    except MarzbanError as e:
        await call.answer(f"خطا: {e}", show_alert=True)
        return
    sub_url = user.get("subscription_url", "")
    if sub_url and sub_url.startswith("/"):
        sub_url = MARZBAN_URL + sub_url
    await call.message.answer(f"🔗 لینک اشتراک «{username}»:\n<code>{sub_url}</code>", parse_mode="HTML")
    await call.answer()


# ---------- تایید عملیات‌های خطرناک ----------

@router.callback_query(F.data.startswith("confirm:"))
@admin_only
async def cb_confirm(call: CallbackQuery, state: FSMContext):
    _, action, username = call.data.split(":", 2)
    try:
        if action == "delete":
            await marzban.delete_user(username)
            await call.message.edit_text(f"🗑 کاربر «{username}» حذف شد.", reply_markup=main_menu_kb())
        elif action == "reset":
            user = await marzban.reset_user_data(username)
            await call.message.edit_text(
                f"🔄 مصرف «{username}» ریست شد.\n\n{await user_summary(user)}",
                reply_markup=user_detail_kb(username, user.get("status", "active")),
                parse_mode="HTML",
            )
    except MarzbanError as e:
        await call.message.edit_text(f"❌ خطا:\n{e}", reply_markup=main_menu_kb())
    await call.answer()


# ---------- وضعیت سیستم (با آمار لوکیشن) ----------

@router.callback_query(F.data == "system_stats")
@admin_only
async def cb_system_stats(call: CallbackQuery, state: FSMContext):
    try:
        stats = await marzban.get_system_stats()
        all_users = await marzban.list_all_users()
    except MarzbanError as e:
        await call.message.edit_text(f"❌ خطا:\n{e}", reply_markup=main_menu_kb())
        await call.answer()
        return

    total_traffic = fmt_bytes((stats.get("incoming_bandwidth", 0) or 0) + (stats.get("outgoing_bandwidth", 0) or 0))
    mem_used = fmt_bytes(stats.get("mem_used", 0) or 0)
    mem_total = fmt_bytes(stats.get("mem_total", 0) or 0)

    # آمار به تفکیک لوکیشن
    loc_stats = {name: {"total": 0, "online": 0} for name in LOCATIONS}
    for u in all_users:
        locs = get_user_locations(u)
        online = is_online(u)
        for loc in locs:
            if loc in loc_stats:
                loc_stats[loc]["total"] += 1
                if online:
                    loc_stats[loc]["online"] += 1

    lines = [
        "📊 <b>وضعیت سیستم</b>",
        "",
        f"👥 کاربران کل: <code>{stats.get('total_user', '-')}</code>",
        f"✅ کاربران فعال: <code>{stats.get('users_active', '-')}</code>",
        f"📶 مصرف کل ترافیک: <code>{total_traffic}</code>",
        f"💾 مصرف رم: <code>{mem_used} / {mem_total}</code>",
        f"⚙️ هسته‌های CPU: <code>{stats.get('cpu_cores', '-')}</code>  |  بار: <code>{stats.get('cpu_usage', '-')}%</code>",
        "",
        "📍 <b>آمار به تفکیک لوکیشن:</b>",
    ]

    for loc_name, data in loc_stats.items():
        lines.append(f"{loc_name}:  {data['online']} آنلاین از {data['total']} کاربر")

    await call.message.edit_text("\n".join(lines), reply_markup=main_menu_kb(), parse_mode="HTML")
    await call.answer()


# --- کارهای پس‌زمینه ---

ALERT_CHECK_INTERVAL = 60 * 60
DATA_ALERT_THRESHOLD = 0.9
EXPIRE_ALERT_HOURS = 24
BACKUP_HOUR_UTC = 3

_alerted = set()


async def _check_alerts(bot: Bot):
    try:
        users = await marzban.list_all_users()
    except MarzbanError:
        return

    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    lines = []
    active_keys = set()

    for u in users:
        if u.get("status") != "active":
            continue
        username = u.get("username")
        limit = u.get("data_limit") or 0
        used = u.get("used_traffic") or 0
        expire = u.get("expire") or 0

        if limit and used / limit >= DATA_ALERT_THRESHOLD:
            key = (username, "data")
            active_keys.add(key)
            if key not in _alerted:
                lines.append(f"📦 «{username}» حجمش داره تموم می‌شه: <code>{fmt_bytes(used)} / {fmt_bytes(limit)}</code>")

        if expire and 0 < (expire - now) <= EXPIRE_ALERT_HOURS * 3600:
            key = (username, "expire")
            active_keys.add(key)
            if key not in _alerted:
                hours_left = int((expire - now) / 3600)
                lines.append(f"📅 «{username}» کمتر از {hours_left} ساعت تا انقضا داره")

    _alerted.intersection_update(active_keys)
    _alerted.update(active_keys)

    if lines:
        text = "⚠️ <b>هشدار خودکار</b>\n\n" + "\n".join(lines)
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML")
            except Exception:
                pass


async def _send_backup(bot: Bot):
    try:
        users = await marzban.list_all_users()
    except MarzbanError:
        return
    payload = json.dumps(users, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    doc = BufferedInputFile(payload, filename=f"marzban-backup-{today}.json")
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_document(admin_id, doc, caption=f"🗄 بک‌آپ لیست کاربران — {today}")
        except Exception:
            pass


async def background_jobs(bot: Bot):
    last_backup_date = None
    while True:
        await _check_alerts(bot)
        now = datetime.datetime.now(datetime.timezone.utc)
        if now.hour == BACKUP_HOUR_UTC and last_backup_date != now.date():
            await _send_backup(bot)
            last_backup_date = now.date()
        await asyncio.sleep(ALERT_CHECK_INTERVAL)


async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(background_jobs(bot))
    try:
        await dp.start_polling(bot)
    finally:
        await marzban.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
