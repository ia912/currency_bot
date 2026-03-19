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

@dp.message(F.text)
async def process_amount(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_states or user_states[user_id]["step"] != "amount":
        return
        
    try:
        amount = float(message.text.replace(",", "."))
        user_states[user_id]["amount"] = amount
        
        await message.answer(
            "📊 <b>Commission in %</b>\n\n"
            "Example: <code>0.35</code>", parse_mode="HTML"
        )
        user_states[user_id]["step"] = "commission"
    except ValueError:
        await message.answer("❌ Enter a number! Example: 2100000")

@dp.message(lambda m: m.from_user.id in user_states and user_states[m.from_user.id]["step"] == "commission")
async def process_commission(message: types.Message):
    user_id = message.from_user.id
    try:
        commission = float(message.text.replace(",", "."))
        user_states[user_id]["commission"] = commission
        
        await message.answer(
            "💹 <b>Exchange rate</b>\n\n"
            "Example: <code>77.2733</code>", parse_mode="HTML"
        )
        user_states[user_id]["step"] = "exchange_rate"
    except ValueError:
        await message.answer("❌ Enter a number! Example: 0.35")

@dp.
message(lambda m: m.from_user.id in user_states and user_states[m.from_user.id]["step"] == "exchange_rate")
async def process_exchange_rate(message: types.Message):
    user_id = message.from_user.id
    state = user_states[user_id]
    
    try:
        exchange_rate = float(message.text.replace(",", "."))
        
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
        
    except Exception as e:
        await message.answer(f"❌ Error: {e}\nTry again with /start")

async def main():
    print("Bot started!")
    await dp.start_polling(bot)

if name == "__main__":
    asyncio.run(main())
