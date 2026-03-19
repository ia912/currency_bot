import asyncio
import os
from io import BytesIO
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from PIL import Image, ImageDraw, ImageFont

bot = Bot(token=os.getenv("BOT_TOKEN"))
dp = Dispatcher()
user_states = {}

CURRENCIES = ["RUB", "USD", "USN", "USDT", "AED", "AEN", "CNY"]

def is_direct_pair(currency_in, currency_out):
    """Проверяет прямую пару RUB→другая или обратную"""
    direct_pairs = [("RUB", "USD"), ("RUB", "USDT"), ("RUB", "USN"), 
                   ("RUB", "AED"), ("RUB", "AEN"), ("RUB", "CNY")]
    return (currency_in, currency_out) in direct_pairs

def calculate_exchange(amount_in, currency_in, currency_out, exchange_rate, commission_pct, input_is_currency_in):
    """Основная формула расчета"""
    commission_factor = 1 - commission_pct / 100
    
    if input_is_currency_in:  # Amount in Currency IN → считаем Currency OUT
        if is_direct_pair(currency_in, currency_out):
            result = amount_in * exchange_rate * commission_factor
        else:  # Обратная пара
            effective_rate = 1 / exchange_rate
            result = amount_in * effective_rate * commission_factor
    else:  # Amount in Currency OUT → считаем Currency IN
        if is_direct_pair(currency_in, currency_out):
            result = amount_out / exchange_rate / commission_factor
        else:  # Обратная пара  
            effective_rate = 1 / exchange_rate
            result = amount_out / effective_rate / commission_factor
    
    return round(result, 2)

def create_table_image(amount1, currency1, amount2, currency2, exchange_rate, commission_pct, input_is_currency_in):
    """Таблица со светло-желтым фоном"""
    w, h = 800, 500
    img = Image.new("RGB", (w, h), (255, 240, 180))  # Светло-желтый фон
    draw = ImageDraw.Draw(img)
    
    # Белая карточка
    draw.rounded_rectangle([20, 20, w-20, h-20], radius=25, fill="white")
    
    # Шрифты
    try:
        font = ImageFont.truetype("arial.ttf", 20)
        font_b = ImageFont.truetype("arialbd.ttf", 24)
    except:
        font = ImageFont.load_default()
        font_b = ImageFont.load_default()
    
    x1, x2, x3 = 40, 280, 500
    y = 45
    
    # Заголовки
    draw.text((x1, y), " ", font=font_b, fill="black")
    draw.text((x2, y), "CCY", font=font_b, fill="black")
    draw.text((x3, y), "VALUE", font=font_b, fill="black")
    y += 60
    
    # 4 строки таблицы
    if input_is_currency_in:
        rows = [
            ("Amount in", currency1, f"{amount1:,.2f}"),
            ("Exchange rate", f"{currency1}{currency2}", f"{exchange_rate:.4f}"),
            ("Commission", "%", f"{commission_pct:.2f}%"),
            ("Amount out", currency2, f"{amount2:,.2f}")
        ]
    else:
        rows = [
            ("Amount out", currency2, f"{amount1:,.2f}"),
            ("Exchange rate", f"{currency1}{currency2}", f"{exchange_rate:.4f}"),
            ("Commission", "%", f"{commission_pct:.2f}%"),
            ("Amount in", currency1, f"{amount2:,.2f}")
        ]
    
    for i, (label, ccy, value) in enumerate(rows):
        if i % 2 == 1:
            draw.rectangle([30, y-5, w-30, y+55], fill=(255, 255, 220))  # Светло-зеленый
        
        draw.text((x1, y), label, font=font, fill="black")
        draw.text((x2, y), ccy, font=font, fill="black")
        draw.text((x3, y), value, font=font_b, fill="black")
        y += 70
    
    bio = BytesIO()
    img.save(bio, "PNG")
    bio.seek(0)
    return bio

@dp.message(Command("start"))
async def start(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 RUB", callback_data="currency_in:RUB"),
         InlineKeyboardButton(text="🇺🇸 USD", callback_data="currency_in:USD")],
        [InlineKeyboardButton(text="🪙 USN", callback_data="currency_in:USN"),
         InlineKeyboardButton(text="₿ USDT", callback_data="currency_in:USDT")],
        [InlineKeyboardButton(text="🇦🇪 AED", callback_data="currency_in:AED"),
InlineKeyboardButton(text="🇦🇪 AEN", callback_data="currency_in:AEN")],
        [InlineKeyboardButton(text="🇨🇳 CNY", callback_data="currency_in:CNY")]
    ])
    await message.answer("💱 <b>Currency IN</b>", reply_markup=kb, parse_mode="HTML")
    user_states[message.from_user.id] = {"step": "currency_in"}

@dp.callback_query(F.data.startswith("currency_in:"))
async def currency_in_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user_states[user_id]["currency_in"] = callback.data.split(":")[1]
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 RUB", callback_data="currency_out:RUB"),
         InlineKeyboardButton(text="🇺🇸 USD", callback_data="currency_out:USD")],
        [InlineKeyboardButton(text="🪙 USN", callback_data="currency_out:USN"),
         InlineKeyboardButton(text="₿ USDT", callback_data="currency_out:USDT")],
        [InlineKeyboardButton(text="🇦🇪 AED", callback_data="currency_out:AED"),
         InlineKeyboardButton(text="🇦🇪 AEN", callback_data="currency_out:AEN")],
        [InlineKeyboardButton(text="🇨🇳 CNY", callback_data="currency_out:CNY")]
    ])
    await callback.message.edit_text(
        f"💰 <b>Currency OUT</b>\n\n"
        f"<code>{user_states[user_id]['currency_in']} → ?</code>", 
        reply_markup=kb, parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("currency_out:"))
async def currency_out_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user_states[user_id]["currency_out"] = callback.data.split(":")[1]
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📥 Amount in {user_states[user_id]['currency_in']}", 
                             callback_data="amount:in")],
        [InlineKeyboardButton(text=f"📤 Amount in {user_states[user_id]['currency_out']}", 
                             callback_data="amount:out")]
    ])
    await callback.message.edit_text(
        f"💱 <b>Выберите вводимую сумму</b>\n\n"
        f"<code>{user_states[user_id]['currency_in']} ↔ {user_states[user_id]['currency_out']}</code>",
        reply_markup=kb, parse_mode="HTML"
    )
    user_states[user_id]["step"] = "amount_choice"

@dp.callback_query(F.data.startswith("amount:"))
async def amount_choice_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user_states[user_id]["input_is_currency_in"] = callback.data.split(":")[1] == "in"
    
    currency_name = user_states[user_id]["currency_in"] if user_states[user_id]["input_is_currency_in"] else user_states[user_id]["currency_out"]
    await callback.message.edit_text(
        f"💵 <b>Enter the amount in {currency_name}</b>\n\n"
        f"Example: <code>2100000</code>", parse_mode="HTML"
    )
    user_states[user_id]["step"] = "amount"

# ГЛОБАЛЬНЫЙ ОБРАБОТЧИК СОСТОЯНИЙ (заменяет все!)
@dp.message(F.text)
async def handle_states(message: types.Message):
    user_id = message.from_user.id
    
    # ШАГ 1: Если нет состояния = ждем сумму
    if user_id not in user_states:
        try:
            amount = float(message.text.replace(',', '.'))
            if amount <= 0:
                await message.answer("❌ Amount > 0 (1000.50)")
                return
            user_states[user_id] = {"amount": amount, "step": "exchange_rate"}
            await message.answer("💱 Enter EXCHANGE RATE (1.2345):")
        except:
            await message.answer("❌ Enter amount (1000.50)")
        return
    
    # ШАГ 2: Курс
    if user_states[user_id]["step"] == "exchange_rate":
        try:
            rate = float(message.text.replace(',', '.'))
            user_states[user_id]["rate"] = rate
            user_states[user_id]["step"] = "commission"
            await message.answer("💳 Enter COMMISSION (0.35):")
        except:
            await message.answer("❌ Enter rate (1.2345)")
        return
    
    # ШАГ 3: Комиссия
    if user_states[user_id]["step"] == "commission":
        try:
            commission = float(message.text.replace(',', '.'))
            state = user_states[user_id]
            total = state["amount"] * state["rate"] - commission
            
            await message.answer(
                f"💰 TOTAL TO WITHDRAW:\n"
                f"Amount: ${state['amount']:,.2f}\n"
                f"Rate: 1 = {state['rate']:.4f}\n"
                f"Fee: -${commission:,.2f}\n"
                f"📥 You receive: ${total:,.2f}"
            )
            del user_states[user_id]  # СБРОС
        except:
            await message.answer("❌ Enter commission (0.35)")
        return

# УДАЛИТЕ ВСЕ другие @dp.message() handlers!
@dp.message()
async def catch_all(message: types.Message):
    if message.text == '/start':
        user_states.clear()  # СБРОС всех состояний
        await message.answer("📊 /start - Enter amount:")
    else:
        await message.answer("❌ /start first")
        
        # Расчет
        amount_in = state["amount"] if state["input_is_currency_in"] else 0
        amount_out = state["amount"] if not state["input_is_currency_in"] else 0
        
        currency_in = state["currency_in"]
        currency_out = state["currency_out"]
        
        if state["input_is_currency_in"]:
            result_amount = calculate_exchange(amount_in, currency_in, currency_out, 
                                             exchange_rate, state["commission"], True)
            display_amount1, display_amount2 = amount_in, result_amount
        else:
            result_amount = calculate_exchange(amount_out, currency_in, currency_out, 
                                             exchange_rate, state["commission"], False)
            display_amount1, display_amount2 = result_amount, amount_out
        
        # Создание картинки
        img = create_table_image(display_amount1, currency_in, display_amount2, 
                               currency_out, exchange_rate, state["commission"], 
                               state["input_is_currency_in"])
        
        await message.answer_photo(img, caption="✅ <b>Calculation complete!</b>", parse_mode="HTML")
        del user_states[user_id]
        
        await message.answer(f"❌ Error: {e}\nTry again with /start")

async def main():
    print("Bot started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
@dp.message(lambda m: m.from_user.id in user_states and user_states[m.from_user.id]["step"] == "commission")
async def process_commission(message: types.Message):
    user_id = message.from_user.id
    state = user_states[user_id]
    
    try:
        commission = float(message.text.replace(',', '.'))
        state["commission"] = commission
        
        amount = state["amount"]
        rate = state["rate"]
        total = amount * rate - commission
        
        await message.answer(
            f"💰 TOTAL TO WITHDRAW:\n"
            f"Amount: ${amount:,.2f}\n"
            f"Rate: 1 = {rate:.4f}\n"
            f"Fee: -${commission:,.2f}\n"
            f"📥 You receive: ${total:,.2f}"
        )
        
        del user_states[user_id]
        
    except ValueError:
        await message.answer("❌ Enter a number like 0.35")
