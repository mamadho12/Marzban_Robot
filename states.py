from aiogram.fsm.state import State, StatesGroup


class AddUser(StatesGroup):
    username = State()
    data_limit = State()
    expire_days = State()
    locations = State()


class ExtendUser(StatesGroup):
    days = State()


class AddDataUser(StatesGroup):
    gb = State()


class EditLocation(StatesGroup):
    select = State()


class CustomerRegister(StatesGroup):
    username = State()
