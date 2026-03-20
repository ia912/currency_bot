
import asyncio
import logging
import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from io import BytesIO
from typing import Any, Dict, Literal, Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from PIL import Image, ImageDraw, ImageFont


CURRENCIES = ["RUB", "USD", "USN", "USDT", "AED", "AEN", "CNY"]

# Direct pairs for the original inversion rule:
# if send_currency -> receive_currency is one of these pairs, rate is used as entered;
# for the reverse direction, the bot uses 1 / rate in the formula.
DIRECT_PAIRS = {
    ("RUB", "USD"),
    ("RUB", "USN"),
    ("RUB", "USDT"),
    ("RUB", "AED"),
    ("RUB", "AEN"),
    ("RUB", "CNY"),
}

CURRENCY_LABELS = {
    "RUB": "🇷🇺 RUB",
    "USD": "🇺🇸 USD",
    "USN": "🪙 USN",
    "USDT": "₮ USDT",
    "AED": "🇦🇪 AED",
    "AEN": "🇦🇪 AEN",
    "CNY": "🇨🇳 CNY",
}

# Table colors
TABLE_OUTER_BG = (246, 248, 250)
ROW_WHITE = (255, 255, 255)
ROW_BLUE = (212, 236, 255)
GRID = (214, 214, 214)
TEXT = (18, 18, 18)

RateMode = Literal["direct", "reverse", "custom"]

dp = Dispatcher(storage=MemoryStorage())


class CalcStates(StatesGroup):
    waiting_currency_in = State()
    waiting_currency_out = State()
    waiting_amount_side = State()
    waiting_amount = State()
    waiting_commission = State()
    waiting_rate = State()


def restart_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔄 Restart", callback_data="restart")]]
    )


def build_currency_keyboard(prefix: str, exclude: Optional[str] = None) -> InlineKeyboardMarkup:
    buttons = []
    for currency in CURRENCIES:
        if currency == exclude:
            continue
        buttons.append(
            InlineKeyboardButton(
                text=CURRENCY_LABELS[currency],
                callback_data=f"{prefix}:{currency}",
            )
        )

    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="🔄 Restart", callback_data="restart")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_amount_side_keyboard(currency_in: str, currency_out: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Amount in {currency_in}",
                    callback_data="amount_side:in",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"Amount in {currency_out}",
                    callback_data="amount_side:out",
                )
            ],
            [InlineKeyboardButton(text="🔄 Restart", callback_data="restart")],
        ]
    )


async def start_dialog(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CalcStates.waiting_currency_in)
    await message.answer(
        "💱 <b>Currency IN</b>\n\nCurrency that we receive:",
        reply_markup=build_currency_keyboard("currency_in"),
        parse_mode="HTML",
    )


def parse_decimal(text: str) -> Decimal:
    normalized = text.strip().replace(" ", "").replace(",", ".")
    if not normalized:
        raise ValueError("Empty input")

    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError("Invalid number") from exc


def quantize_pattern(places: int) -> Decimal:
    return Decimal("1").scaleb(-places)


def format_decimal(value: Decimal, places: int = 2, strip_trailing: bool = False) -> str:
    quantized = value.quantize(quantize_pattern(places), rounding=ROUND_HALF_UP)
    text = f"{quantized:,.{places}f}"
    if strip_trailing and "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def format_rate_value(value: Decimal) -> str:
    if abs(value) >= Decimal("1"):
        return format_decimal(value, 4, strip_trailing=False)
    return format_decimal(value, 8, strip_trailing=True)


def load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            "DejaVuSans-Bold.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            "DejaVuSans.ttf",
        ]
    )

    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def get_rate_mode(send_currency: str, receive_currency: str) -> RateMode:
    if (send_currency, receive_currency) in DIRECT_PAIRS:
        return "direct"
    if (receive_currency, send_currency) in DIRECT_PAIRS:
        return "reverse"
    return "custom"


def resolve_rate(send_currency: str, receive_currency: str, entered_rate: Decimal) -> Tuple[Decimal, RateMode]:
    if entered_rate <= 0:
        raise ValueError("Exchange rate must be greater than 0")

    rate_mode = get_rate_mode(send_currency, receive_currency)
    if rate_mode == "direct":
        return entered_rate, "direct"
    if rate_mode == "reverse":
        return Decimal("1") / entered_rate, "reverse"

    return entered_rate, "custom"


def calculate_internal_result(
    amount_value: Decimal,
    send_currency: str,
    receive_currency: str,
    entered_rate: Decimal,
    commission_pct: Decimal,
    input_is_send_amount: bool,
) -> Dict[str, Any]:
    if amount_value <= 0:
        raise ValueError("Amount must be greater than 0")
    if commission_pct < 0 or commission_pct >= 100:
        raise ValueError("Commission must be between 0 and 100")

    effective_rate, rate_mode = resolve_rate(send_currency, receive_currency, entered_rate)
    commission_factor = Decimal("1") - (commission_pct / Decimal("100"))

    if commission_factor <= 0:
        raise ValueError("Commission leaves nothing to calculate")

    # Main equality, keeping the original calculation logic internally:
    # send_amount = receive_amount * effective_rate / (1 - commission)
    # receive_amount = send_amount * (1 - commission) / effective_rate
    if input_is_send_amount:
        send_amount = amount_value
        receive_amount = send_amount * commission_factor / effective_rate
    else:
        receive_amount = amount_value
        send_amount = receive_amount * effective_rate / commission_factor

    before_margin = receive_amount * effective_rate

    return {
        "send_currency": send_currency,
        "receive_currency": receive_currency,
        "send_amount": send_amount,
        "receive_amount": receive_amount,
        "before_margin": before_margin,
        "entered_rate": entered_rate,
        "effective_rate": effective_rate,
        "commission_pct": commission_pct,
        "rate_mode": rate_mode,
    }


def get_fitted_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    max_height: int,
    start_size: int,
    bold: bool = False,
) -> ImageFont.ImageFont:
    size = start_size
    while size >= 20:
        font = load_font(size, bold=bold)
        bbox = draw.textbbox((0, 0), text, font=font)
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        if width <= max_width and height <= max_height:
            return font
        size -= 4
    return load_font(20, bold=bold)


def draw_centered_text(
    draw: ImageDraw.ImageDraw,
    box: Tuple[int, int, int, int],
    text: str,
    bold: bool = False,
    start_size: int = 120,
    fill: Tuple[int, int, int] = TEXT,
) -> None:
    left, top, right, bottom = box
    max_width = max(1, right - left - 40)
    max_height = max(1, bottom - top - 30)
    font = get_fitted_font(draw, text, max_width, max_height, start_size, bold=bold)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = left + ((right - left - text_w) // 2)
    y = top + ((bottom - top - text_h) // 2) - 6
    draw.text((x, y), text, font=font, fill=fill)


def create_result_image(result: Dict[str, Any]) -> bytes:
    width, height = 3600, 1350
    margin = 55
    table_left = margin
    table_top = margin
    table_right = width - margin
    table_bottom = height - margin

    label_col_width = 1380
    ccy_col_width = 760
    value_col_width = (table_right - table_left) - label_col_width - ccy_col_width

    x0 = table_left
    x1 = x0 + label_col_width
    x2 = x1 + ccy_col_width
    x3 = x2 + value_col_width

    row_count = 5
    row_height = (table_bottom - table_top) // row_count

    img = Image.new("RGB", (width, height), TABLE_OUTER_BG)
    draw = ImageDraw.Draw(img)

    draw.rectangle((table_left, table_top, table_right, table_top + row_count * row_height), fill=ROW_WHITE)

    rows = [
        ("TO AMOUNT IN", result["ui_currency_in"], format_decimal(result["receive_amount"], 2), True),
        ("FX RATE", result["fx_rate_label"], format_rate_value(result["entered_rate"]), False),
        ("BEFORE MARGIN", result["ui_currency_out"], format_decimal(result["before_margin"], 2), False),
        ("CONTRACT MARGIN", "%", f"{format_decimal(result['commission_pct'], 2)}%", False),
        ("FROM AMOUNT IN", result["ui_currency_out"], format_decimal(result["send_amount"], 2), True),
    ]

    for row_index, (label, ccy, value, is_amount_row) in enumerate(rows):
        top = table_top + row_index * row_height
        bottom = top + row_height
        row_fill = ROW_BLUE if row_index in {1, 3} else ROW_WHITE
        draw.rectangle((table_left, top, table_right, bottom), fill=row_fill)

        # Cell borders
        draw.rectangle((x0, top, x1, bottom), outline=GRID, width=3)
        draw.rectangle((x1, top, x2, bottom), outline=GRID, width=3)
        draw.rectangle((x2, top, x3, bottom), outline=GRID, width=3)

        # Much larger text
        label_size = 128 if is_amount_row else 114
        ccy_size = 128 if is_amount_row else 114
        value_size = 136 if is_amount_row else 118

        draw_centered_text(draw, (x0, top, x1, bottom), label, bold=is_amount_row, start_size=label_size)
        draw_centered_text(draw, (x1, top, x2, bottom), ccy, bold=is_amount_row, start_size=ccy_size)
        draw_centered_text(draw, (x2, top, x3, bottom), value, bold=is_amount_row, start_size=value_size)

    # Outer border
    draw.rectangle((table_left, table_top, table_right, table_top + row_count * row_height), outline=GRID, width=4)

    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await start_dialog(message, state)


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Calculation cancelled. Send /start to begin again.")


@dp.callback_query(F.data == "restart")
async def restart_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message:
        await start_dialog(callback.message, state)


@dp.callback_query(CalcStates.waiting_currency_in, F.data.startswith("currency_in:"))
async def currency_in_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    ui_currency_in = callback.data.split(":", maxsplit=1)[1]

    if ui_currency_in not in CURRENCIES:
        await callback.message.answer("❌ Unsupported currency. Send /start to try again.")
        await state.clear()
        return

    await state.update_data(ui_currency_in=ui_currency_in)
    await state.set_state(CalcStates.waiting_currency_out)

    await callback.message.edit_text(
        f"💸 <b>Currency OUT</b>\n\n"
        f"Selected IN: <code>{ui_currency_in}</code>\n"
        f"Currency that we send:",
        reply_markup=build_currency_keyboard("currency_out", exclude=ui_currency_in),
        parse_mode="HTML",
    )


@dp.callback_query(CalcStates.waiting_currency_out, F.data.startswith("currency_out:"))
async def currency_out_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    ui_currency_out = callback.data.split(":", maxsplit=1)[1]
    data = await state.get_data()
    ui_currency_in = data.get("ui_currency_in")

    if ui_currency_out not in CURRENCIES or not ui_currency_in:
        await callback.message.answer("❌ Session expired. Send /start to begin again.")
        await state.clear()
        return

    if ui_currency_out == ui_currency_in:
        await callback.answer("Currency OUT must be different", show_alert=True)
        return

    await state.update_data(ui_currency_out=ui_currency_out)
    await state.set_state(CalcStates.waiting_amount_side)

    await callback.message.edit_text(
        f"↕️ <b>Select the amount you want to enter</b>\n\n"
        f"<code>{ui_currency_in} ↔ {ui_currency_out}</code>",
        reply_markup=build_amount_side_keyboard(ui_currency_in, ui_currency_out),
        parse_mode="HTML",
    )


@dp.callback_query(CalcStates.waiting_amount_side, F.data.startswith("amount_side:"))
async def amount_side_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    side = callback.data.split(":", maxsplit=1)[1]
    data = await state.get_data()

    if side not in {"in", "out"}:
        await callback.message.answer("❌ Invalid option. Send /start to begin again.")
        await state.clear()
        return

    input_currency = data["ui_currency_in"] if side == "in" else data["ui_currency_out"]
    await state.update_data(input_side=side)
    await state.set_state(CalcStates.waiting_amount)

    await callback.message.edit_text(
        f"💵 <b>Enter the amount in {input_currency}</b>\n\n"
        f"Examples: <code>2100000</code> or <code>1000.50</code>",
        reply_markup=restart_keyboard(),
        parse_mode="HTML",
    )


@dp.message(CalcStates.waiting_amount)
async def process_amount(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("❌ Send the amount as text. Example: 1000 or 1000.50")
        return

    try:
        amount = parse_decimal(message.text)
    except ValueError:
        await message.answer("❌ Invalid amount. Example: 1000 or 1000.50")
        return

    if amount <= 0:
        await message.answer("❌ Amount must be greater than 0.")
        return

    await state.update_data(amount=str(amount))
    await state.set_state(CalcStates.waiting_commission)

    await message.answer(
        "💳 <b>Enter commission in %</b>\n\n"
        "Examples: <code>0.1</code> or <code>0.35</code>\n"
        "<i>0.1 means 0.1%</i>",
        reply_markup=restart_keyboard(),
        parse_mode="HTML",
    )


@dp.message(CalcStates.waiting_commission)
async def process_commission(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("❌ Send the commission as text. Example: 0.1 or 0.35")
        return

    try:
        commission_pct = parse_decimal(message.text)
    except ValueError:
        await message.answer("❌ Invalid commission. Example: 0.1 or 0.35")
        return

    if commission_pct < 0 or commission_pct >= 100:
        await message.answer("❌ Commission must be from 0 to less than 100.")
        return

    await state.update_data(commission_pct=str(commission_pct))
    await state.set_state(CalcStates.waiting_rate)

    data = await state.get_data()
    ui_currency_in = data["ui_currency_in"]
    ui_currency_out = data["ui_currency_out"]
    rate_pair = f"{ui_currency_in}{ui_currency_out}"

    await message.answer(
        f"📈 <b>Enter exchange rate for {rate_pair}</b>\n\n"
        f"Examples: <code>0.0112</code> or <code>90</code>",
        reply_markup=restart_keyboard(),
        parse_mode="HTML",
    )


@dp.message(CalcStates.waiting_rate)
async def process_rate(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("❌ Send the exchange rate as text. Example: 90")
        return

    try:
        entered_rate = parse_decimal(message.text)
    except ValueError:
        await message.answer("❌ Invalid exchange rate. Example: 90")
        return

    if entered_rate <= 0:
        await message.answer("❌ Exchange rate must be greater than 0.")
        return

    data = await state.get_data()

    ui_currency_in = data["ui_currency_in"]   # currency that we receive
    ui_currency_out = data["ui_currency_out"] # currency that we send

    send_currency = ui_currency_out
    receive_currency = ui_currency_in
    input_is_send_amount = data["input_side"] == "out"

    try:
        calc = calculate_internal_result(
            amount_value=Decimal(data["amount"]),
            send_currency=send_currency,
            receive_currency=receive_currency,
            entered_rate=entered_rate,
            commission_pct=Decimal(data["commission_pct"]),
            input_is_send_amount=input_is_send_amount,
        )
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return

    image_payload = {
        **calc,
        "ui_currency_in": ui_currency_in,
        "ui_currency_out": ui_currency_out,
        "fx_rate_label": f"{ui_currency_in}{ui_currency_out}",
    }

    image_bytes = create_result_image(image_payload)

    await message.answer_photo(
        BufferedInputFile(image_bytes, filename="calculation.png"),
        caption="✅ <b>Calculation complete</b>",
        parse_mode="HTML",
        reply_markup=restart_keyboard(),
    )
    await state.clear()


@dp.callback_query(
    F.data.startswith("currency_in:") | F.data.startswith("currency_out:") | F.data.startswith("amount_side:")
)
async def stale_callback(callback: CallbackQuery) -> None:
    await callback.answer("Session expired. Press Restart or send /start.", show_alert=False)


@dp.message()
async def fallback_message(message: Message) -> None:
    await message.answer("Send /start to begin a new calculation.")


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is not set")

    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=token)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
