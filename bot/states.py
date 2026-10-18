from aiogram.fsm.state import State, StatesGroup


class Form(StatesGroup):
    age = State()
    country = State()
    city = State()
    nickname = State()
    bio = State()
    promo = State()
    appeal = State()
    qotd = State()
