import asyncio
import functools
import logging
import datetime
import base64

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from config import BOT_TOKEN, ADMIN_IDS, MARZBAN_URL, LOCATIONS
from marzban_api import marzban, MarzbanError
from keyboards import (
    main_menu_kb, users_list_kb, user_detail_kb,
    confirm_kb, cancel_kb, location_kb,
)
from states import AddUser, ExtendUser, ReduceDays, AddDataUser, ReduceDataUser, EditLocation

logging.basicConfig(level=logging.INFO)

router = Router()

LARGE_REDUCE_DAYS = 7
LARGE_REDUCE_GB = 5.0


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
    if not n or n < 0:
        return "0 GB"
    gb = n / (1024 ** 3)
    if gb < 0.01:
        mb = n / (1024 ** 2)
        if mb < 1:
            return f"{n} B"
        return f"{mb:.1f} MB"
    return f"{gb:.2f} GB"


def fmt_expire(ts) -> str:
    if not ts:
        return "نامحدود"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y/%m/%d")


def remaining_time_fa(ts) -> str:
    if not ts:
        return "نامحدود"
    now = datetime.datetime.now().timestamp()
    delta = int(ts - now)
    if delta <= 0:
        return "منقضی شده"
    days = delta // 86400
    hours = (delta % 86400) // 3600
    if days > 0 and hours > 0:
        return f"{days} روز و {hours} ساعت"
    if days > 0:
        return f"{days} روز"
    if hours > 0:
        return f"{hours} ساعت"
    minutes = (delta % 3600) // 60
    return f"{max(minutes, 1)} دقیقه"


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
        return "🔴 هرگز متصل نشده"
    delta = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds()
    if 0 <= delta < ONLINE_THRESHOLD_SECONDS:
        return "🟢 آنلاین"
    return f"⚪️ آخرین اتصال: {_relative_time_fa(delta)}"


def progress_bar(used: int, limit: int, length: int = 12) -> str:
    if not limit or limit <= 0:
        return ""
    ratio = min(used / limit, 1.0)
    filled = round(ratio * length)
    bar = "█" * filled + "░" * (length - filled)
    pct = int(ratio * 100)
    return f"<code>{bar}</code>  {pct}%"


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
    line = config_line.lower()

    if "path=%2fvless3" in line or "path=/vless3" in line or "path=%2Fvless3" in line:
        return "🇸🇬 لوکیشن سنگاپور"
    if "path=%2fvless2" in line or "path=/vless2" in line or "path=%2Fvless2" in line:
        return "🇳🇱 لوکیشن هلند"
    if "path=%2fvless" in line or "path=/vless" in line or "path=%2Fvless" in line:
        return "🇺🇸 لوکیشن آمریکا"

    if ":2053" in line:
        return "🇸🇬 لوکیشن سنگاپور"
    if ":8443" in line:
        return "🇳🇱 لوکیشن هلند"
    if ":443" in line:
        return "🇺🇸 لوکیشن آمریکا"

    return None


async def extract_configs_by_location(user: dict) -> dict[str, list[str]]:
    result = {name: [] for name in LOCATIONS}

    sub_url = user.get("subscription_url", "")
    if not sub_url:
        return result

    content = await marzban.fetch_subscription_content(sub_url)
    if not content:
        return result

    try:
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
            user_locs = get_user_locations(user)
            if user_locs:
                result[user_locs[0]].append(line)

    return result


async def get_nodes_status_text() -> str:
    try:
        nodes = await marzban.get_nodes()
    except Exception:
        return "📍 وضعیت نودها: در دسترس نیست"

    if not nodes:
        return "📍 هیچ نودی پیدا نشد"

    lines = ["📍 <b>وضعیت نودها</b>"]
    for node in nodes:
        name = node.get("name", "بدون‌نام")
        status = node.get("status", "unknown")
        icon = "🟢" if status == "connected" else "🔴"
        lines.append(f"  {icon}  {name}")

    return "\n".join(lines)


async def user_summary(user: dict) -> str:
    username = user.get("username", "-")
    status = user.get("status", "-")
    status_fa = STATUS_FA.get(status, status)

    used = user.get("used_traffic", 0) or 0
    limit = user.get("data_limit", 0) or 0
    used_str = fmt_bytes(used)
    bar = progress_bar(used, limit)

    if limit and limit > 0:
        left = max(limit - used, 0)
        usage_block = (
            f"<code>{used_str}</code>  /  <code>{fmt_bytes(limit)}</code>\n"
            f"باقی‌مانده:  <b>{fmt_bytes(left)}</b>"
        )
    else:
        usage_block = (
            f"مصرف‌شده:  <code>{used_str}</code>\n"
            f"سقف:  <b>نامحدود</b>"
        )

    expire_ts = user.get("expire")
    expire = fmt_expire(expire_ts)
    remain = remaining_time_fa(expire_ts)

    locations = get_user_locations(user)
    loc_text = "  ·  ".join(locations) if locations else "—"

    sub_url = user.get("subscription_url", "")
    if sub_url and sub_url.startswith("/"):
        sub_url = MARZBAN_URL + sub_url

    lines = [
        f"┏━━━━━━━━━━━━━━━━━━",
        f"┃  👤  <b>{username}</b>",
        f"┃  {status_fa}   ·   {online_status_line(user)}",
        f"┗━━━━━━━━━━━━━━━━━━",
        "",
        "📊  <b>مصرف حجم</b>",
        usage_block,
    ]
    if bar:
        lines.append(bar)

    lines += [
        "",
        "📅  <b>اعتبار</b>",
        f"انقضا:  <code>{expire}</code>",
        f"باقی‌مانده:  <b>{remain}</b>",
        "",
        "📍  <b>لوکیشن‌ها</b>",
        f"{loc_text}",
    ]

    if sub_url:
        lines += [
            "",
            "🔗  <b>لینک اشتراک</b>",
            f"<code>{sub_url}</code>",
        ]

    return "\n".join(lines)


@router.message(Command("start"))
@admin_only
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    nodes_text = await get_nodes_status_text()
    text = (
        f"سلام 👋\n\n"
        f"🛡  <b>پنل مدیریت مرزبان</b>\n\n"
        f"{nodes_text}\n\n"
        f"یکی از گزینه‌ها را انتخاب کنید:"
    )
    await message.answer(text, reply_markup=main_menu_kb(), parse_mode="HTML")


@router.callback_query(F.data == "main_menu")
@admin_only
async def cb_main_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    nodes_text = await get_nodes_status_text()
    text = (
        f"🛡  <b>پنل مدیریت مرزبان</b>\n\n"
        f"{nodes_text}\n\n"
        f"یکی از گزینه‌ها را انتخاب کنید:"
    )
    await call.message.edit_text(text, reply_markup=main_menu_kb(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "add_user")
@admin_only
async def cb_add_user(call: CallbackQuery, state: FSMContext):
    await state.set_state(AddUser.username)
    await call.message.edit_text(
        "نام کاربری جدید را بفرستید\n<code>فقط حروف انگلیسی، عدد و _</code>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@router.message(AddUser.username)
@admin_only
async def add_user_username(message: Message, state: FSMContext):
    username = message.text.strip()
    if not username.replace("_", "").isalnum():
        await message.answer("نام کاربری نامعتبر است. دوباره امتحان کنید:")
        return
    await state.update_data(username=username)
    await state.set_state(AddUser.data_limit)
    await message.answer("حجم را به گیگابایت بفرستید\n<code>برای نامحدود: 0</code>", parse_mode="HTML")


@router.message(AddUser.data_limit)
@admin_only
async def add_user_data_limit(message: Message, state: FSMContext):
    try:
        gb = float(message.text.strip())
    except ValueError:
        await message.answer("یک عدد معتبر بفرستید (مثلاً 30 یا 0):")
        return
    await state.update_data(data_limit=gb)
    await state.set_state(AddUser.expire_days)
    await message.answer("مدت اعتبار را به روز بفرستید\n<code>برای نامحدود: 0</code>", parse_mode="HTML")


@router.message(AddUser.expire_days)
@admin_only
async def add_user_expire(message: Message, state: FSMContext):
    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer("یک عدد صحیح بفرستید (مثلاً 30 یا 0):")
        return

    await state.update_data(expire_days=days)
    await state.set_state(AddUser.locations)
    await state.update_data(selected_locations=set())

    await message.answer(
        "لوکیشن‌های مورد نظر را انتخاب کنید:",
        reply_markup=location_kb(set(), confirm_cb="loc_confirm_add"),
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
        await call.answer("حداقل یک لوکیشن انتخاب کنید", show_alert=True)
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
        f"✅ کاربر ساخته شد\n\n{await user_summary(user)}",
        reply_markup=user_detail_kb(username, user.get("status", "active")),
        parse_mode="HTML",
    )
    await call.answer()


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
        f"📋  <b>لیست کاربران</b>\n"
        f"🟢 آنلاین این صفحه: <b>{online_count}</b> از {len(rows)}",
        reply_markup=users_list_kb(rows, offset, has_more),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("user:"))
@admin_only
async def cb_user_detail(call: CallbackQuery, state: FSMContext):
    await state.clear()
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
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("copyconfigs:"))
@admin_only
async def cb_copy_configs(call: CallbackQuery, state: FSMContext):
    username = call.data.split(":", 1)[1]
    try:
        user = await marzban.get_user(username)
    except MarzbanError as e:
        await call.answer(f"خطا: {e}", show_alert=True)
        return

    try:
        configs_by_loc = await extract_configs_by_location(user)
    except Exception:
        await call.answer("خطا در دریافت کانفیگ‌ها", show_alert=True)
        return

    has_any = any(configs_by_loc.values())
    if not has_any:
        await call.answer("هیچ کانفیگی پیدا نشد", show_alert=True)
        return

    lines = [f"📄  <b>کانفیگ‌های {username}</b>", ""]
    for loc_name, confs in configs_by_loc.items():
        if not confs:
            continue
        lines.append(f"{loc_name}")
        for conf in confs:
            lines.append(f"<code>{conf}</code>")
        lines.append("")

    text = "\n".join(lines).strip()
    if len(text) > 4000:
        text = text[:3900] + "\n\n… (پیام کوتاه شد)"

    await call.message.answer(text, parse_mode="HTML")
    await call.answer("ارسال شد")


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
    if not sub_url:
        await call.answer("لینک اشتراک پیدا نشد", show_alert=True)
        return
    if sub_url.startswith("/"):
        sub_url = MARZBAN_URL + sub_url
    await call.message.answer(
        f"🔗  <b>لینک اشتراک</b>  ·  <code>{username}</code>\n\n"
        f"<code>{sub_url}</code>",
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("changelink:"))
@admin_only
async def cb_change_link(call: CallbackQuery, state: FSMContext):
    username = call.data.split(":", 1)[1]
    await call.message.edit_text(
        f"🔄  <b>تغییر لینک اشتراک</b>\n\n"
        f"کاربر:  <code>{username}</code>\n\n"
        f"لینک فعلی باطل می‌شود و لینک جدید ساخته می‌شود.\n"
        f"لینک‌های قبلی دیگر کار نخواهند کرد.\n\n"
        f"مطمئن هستید؟",
        reply_markup=confirm_kb("changelink", username),
        parse_mode="HTML",
    )
    await call.answer()


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
        f"📍  ویرایش لوکیشن  ·  <b>{username}</b>\n"
        f"فعلی: {', '.join(current) if current else '—'}\n\n"
        f"لوکیشن‌های جدید را انتخاب کنید:",
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
        await call.answer("حداقل یک لوکیشن انتخاب کنید", show_alert=True)
        return

    await state.clear()

    try:
        user = await marzban.set_user_locations(username, selected)
    except MarzbanError as e:
        await call.message.edit_text(f"❌ خطا:\n{e}", reply_markup=main_menu_kb())
        return

    await call.message.edit_text(
        f"✅ لوکیشن‌ها به‌روز شد\n\n{await user_summary(user)}",
        reply_markup=user_detail_kb(username, user.get("status", "active")),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("extend:"))
@admin_only
async def cb_extend(call: CallbackQuery, state: FSMContext):
    username = call.data.split(":", 1)[1]
    await state.update_data(username=username)
    await state.set_state(ExtendUser.days)
    await call.message.edit_text(
        f"چند روز به اعتبار <b>{username}</b> اضافه شود؟",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@router.message(ExtendUser.days)
@admin_only
async def extend_days(message: Message, state: FSMContext):
    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer("یک عدد صحیح بفرستید:")
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
        f"✅  <b>+{days} روز</b> اضافه شد\n\n{await user_summary(user)}",
        reply_markup=user_detail_kb(username, user.get("status", "active")),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("reduceday:"))
@admin_only
async def cb_reduce_day(call: CallbackQuery, state: FSMContext):
    username = call.data.split(":", 1)[1]
    await state.update_data(username=username)
    await state.set_state(ReduceDays.days)
    await call.message.edit_text(
        f"چند روز از اعتبار <b>{username}</b> کم شود؟",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@router.message(ReduceDays.days)
@admin_only
async def reduce_days(message: Message, state: FSMContext):
    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer("یک عدد صحیح بفرستید:")
        return
    if days <= 0:
        await message.answer("یک عدد مثبت بفرستید:")
        return
    data = await state.get_data()
    username = data["username"]
    await state.clear()

    if days >= LARGE_REDUCE_DAYS:
        await message.answer(
            f"⚠️  در حال کم کردن <b>{days} روز</b> از <code>{username}</code>\n"
            f"این مقدار نسبتاً زیاد است. مطمئن هستید؟",
            reply_markup=confirm_kb("reduceday", username, str(days)),
            parse_mode="HTML",
        )
        return

    try:
        user = await marzban.subtract_days(username, days)
    except MarzbanError as e:
        await message.answer(f"❌ خطا:\n{e}", reply_markup=main_menu_kb())
        return
    await message.answer(
        f"✅  <b>−{days} روز</b> کم شد\n\n{await user_summary(user)}",
        reply_markup=user_detail_kb(username, user.get("status", "active")),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("adddata:"))
@admin_only
async def cb_adddata(call: CallbackQuery, state: FSMContext):
    username = call.data.split(":", 1)[1]
    await state.update_data(username=username)
    await state.set_state(AddDataUser.gb)
    await call.message.edit_text(
        f"چند گیگابایت به حجم <b>{username}</b> اضافه شود؟",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@router.message(AddDataUser.gb)
@admin_only
async def adddata_gb(message: Message, state: FSMContext):
    try:
        gb = float(message.text.strip())
    except ValueError:
        await message.answer("یک عدد معتبر بفرستید:")
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
        f"✅  <b>+{gb:g} GB</b> اضافه شد\n\n{await user_summary(user)}",
        reply_markup=user_detail_kb(username, user.get("status", "active")),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("reducedata:"))
@admin_only
async def cb_reduce_data(call: CallbackQuery, state: FSMContext):
    username = call.data.split(":", 1)[1]
    await state.update_data(username=username)
    await state.set_state(ReduceDataUser.gb)
    await call.message.edit_text(
        f"چند گیگابایت از حجم <b>{username}</b> کم شود؟",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@router.message(ReduceDataUser.gb)
@admin_only
async def reduce_data_gb(message: Message, state: FSMContext):
    try:
        gb = float(message.text.strip())
    except ValueError:
        await message.answer("یک عدد معتبر بفرستید:")
        return
    if gb <= 0:
        await message.answer("یک عدد مثبت بفرستید:")
        return
    data = await state.get_data()
    username = data["username"]
    await state.clear()

    if gb >= LARGE_REDUCE_GB:
        await message.answer(
            f"⚠️  در حال کم کردن <b>{gb:g} GB</b> از <code>{username}</code>\n"
            f"این مقدار نسبتاً زیاد است. مطمئن هستید؟",
            reply_markup=confirm_kb("reducedata", username, str(gb)),
            parse_mode="HTML",
        )
        return

    try:
        user = await marzban.subtract_data_gb(username, gb)
    except MarzbanError as e:
        await message.answer(f"❌ خطا:\n{e}", reply_markup=main_menu_kb())
        return
    await message.answer(
        f"✅  <b>−{gb:g} GB</b> کم شد\n\n{await user_summary(user)}",
        reply_markup=user_detail_kb(username, user.get("status", "active")),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("reset:"))
@admin_only
async def cb_reset(call: CallbackQuery, state: FSMContext):
    username = call.data.split(":", 1)[1]
    await call.message.edit_text(
        f"🔄  ریست مصرف <code>{username}</code>\n\nمطمئن هستید؟",
        reply_markup=confirm_kb("reset", username),
        parse_mode="HTML",
    )
    await call.answer()


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
        parse_mode="HTML",
    )
    await call.answer("انجام شد")


@router.callback_query(F.data.startswith("delete:"))
@admin_only
async def cb_delete(call: CallbackQuery, state: FSMContext):
    username = call.data.split(":", 1)[1]
    await call.message.edit_text(
        f"🗑  حذف کاربر <code>{username}</code>\n\n"
        f"این کار برگشت‌ناپذیر است. مطمئن هستید؟",
        reply_markup=confirm_kb("delete", username),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("confirm:"))
@admin_only
async def cb_confirm(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    action = parts[1]
    username = parts[2]
    extra = parts[3] if len(parts) > 3 else None

    try:
        if action == "delete":
            await marzban.delete_user(username)
            await call.message.edit_text(
                f"🗑  کاربر <code>{username}</code> حذف شد.",
                reply_markup=main_menu_kb(),
                parse_mode="HTML",
            )
        elif action == "reset":
            user = await marzban.reset_user_data(username)
            await call.message.edit_text(
                f"🔄  مصرف ریست شد\n\n{await user_summary(user)}",
                reply_markup=user_detail_kb(username, user.get("status", "active")),
                parse_mode="HTML",
            )
        elif action == "changelink":
            user = await marzban.revoke_sub(username)
            if not user:
                user = await marzban.get_user(username)
            await call.message.edit_text(
                f"✅  لینک اشتراک <code>{username}</code> تغییر کرد.\n\n"
                f"{await user_summary(user)}",
                reply_markup=user_detail_kb(username, user.get("status", "active")),
                parse_mode="HTML",
            )
        elif action == "reduceday":
            days = int(extra)
            user = await marzban.subtract_days(username, days)
            await call.message.edit_text(
                f"✅  <b>−{days} روز</b> کم شد\n\n{await user_summary(user)}",
                reply_markup=user_detail_kb(username, user.get("status", "active")),
                parse_mode="HTML",
            )
        elif action == "reducedata":
            gb = float(extra)
            user = await marzban.subtract_data_gb(username, gb)
            await call.message.edit_text(
                f"✅  <b>−{gb:g} GB</b> کم شد\n\n{await user_summary(user)}",
                reply_markup=user_detail_kb(username, user.get("status", "active")),
                parse_mode="HTML",
            )
    except MarzbanError as e:
        await call.message.edit_text(f"❌ خطا:\n{e}", reply_markup=main_menu_kb())
    await call.answer()


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

    total_traffic = fmt_bytes(
        (stats.get("incoming_bandwidth", 0) or 0) + (stats.get("outgoing_bandwidth", 0) or 0)
    )
    mem_used = fmt_bytes(stats.get("mem_used", 0) or 0)
    mem_total = fmt_bytes(stats.get("mem_total", 0) or 0)

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
        "📊  <b>وضعیت سیستم</b>",
        "",
        f"👥  کاربران کل:  <code>{stats.get('total_user', '-')}</code>",
        f"✅  فعال:  <code>{stats.get('users_active', '-')}</code>",
        f"📶  ترافیک کل:  <code>{total_traffic}</code>",
        f"💾  رم:  <code>{mem_used} / {mem_total}</code>",
        f"⚙️  CPU:  <code>{stats.get('cpu_cores', '-')}</code> هسته  ·  <code>{stats.get('cpu_usage', '-')}%</code>",
        "",
        "📍  <b>به تفکیک لوکیشن</b>",
    ]

    for loc_name, data in loc_stats.items():
        lines.append(f"  {loc_name}:  {data['online']} آنلاین از {data['total']}")

    await call.message.edit_text("\n".join(lines), reply_markup=main_menu_kb(), parse_mode="HTML")
    await call.answer()


async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    finally:
        await marzban.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
