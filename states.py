from aiogram.fsm.state import State, StatesGroup


class AddUser(StatesGroup):
    username = State()
    data_limit = State()
    expire_days = State()
    locations = State()          # انتخاب لوکیشن موقع ساخت


class ExtendUser(StatesGroup):
    days = State()


class AddDataUser(StatesGroup):
    gb = State()


class EditLocation(StatesGroup):
    select = State()             # ویرایش لوکیشن کاربر موجود
