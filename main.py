"""Telegram inventory, sales and weekly payroll bot for Voroa background workers.

Fresh-database deployment: SQLAlchemy creates the initial schema on startup.
Do not point this first release at an existing production database without a migration.
"""
from __future__ import annotations

import logging
import os
import secrets
from string import Formatter
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String,
    Text, UniqueConstraint, create_engine, func, or_, select, text
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy.pool import NullPool
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)

load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("inventory_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID_RAW = os.getenv("OWNER_TELEGRAM_ID", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")
# ZoneInfo keys are case-sensitive; normalize common dashboard casing.
_timezone_raw = os.getenv("TIMEZONE", "Asia/Manila").strip()
_timezone_key = {
    "asia/manila": "Asia/Manila",
    "utc": "UTC",
    "etc/utc": "Etc/UTC",
}.get(_timezone_raw.casefold(), _timezone_raw)

try:
    TIMEZONE = ZoneInfo(_timezone_key)
except ZoneInfoNotFoundError:
    if _timezone_key == "Asia/Manila":
        # The Philippines uses UTC+08:00 year-round; this fallback works without tzdata.
        TIMEZONE = timezone(timedelta(hours=8), name="Asia/Manila")
        log.warning("IANA timezone data unavailable; using fixed UTC+08:00 for Asia/Manila")
    else:
        raise RuntimeError(
            f"Timezone '{_timezone_raw}' is unavailable. "
            "Use a valid IANA timezone such as Asia/Manila."
        )
RESERVATION_MINUTES = max(2, min(60, int(os.getenv("RESERVATION_MINUTES", "10"))))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add it in Voroa Environment variables.")
if not OWNER_ID_RAW.isdigit():
    raise RuntimeError("OWNER_TELEGRAM_ID must be your numeric Telegram user ID.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing. Add your PostgreSQL connection URL.")
if not ENCRYPTION_KEY:
    raise RuntimeError("ENCRYPTION_KEY is missing. Generate a Fernet key before starting.")

OWNER_ID = int(OWNER_ID_RAW)
try:
    cipher = Fernet(ENCRYPTION_KEY.encode("utf-8"))
except (ValueError, TypeError) as exc:
    raise RuntimeError("ENCRYPTION_KEY is invalid. Generate it with Fernet.generate_key().") from exc

# Voroa PostgreSQL URLs are accepted in either postgres:// or postgresql:// form.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    poolclass=NullPool,  # Close idle connections so a serverless free Postgres can scale down.
    connect_args={"connect_timeout": 10} if DATABASE_URL.startswith("postgresql") else {},
)
Session = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    username: Mapped[str] = mapped_column(String(100), default="")
    role: Mapped[str] = mapped_column(String(20), default="pending")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    inactivity_alert_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: now())


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    mode: Mapped[str] = mapped_column(String(20))  # solo/shared/both/invite
    solo_account_capital: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    shared_account_capital: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    solo_profile_capital: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    shared_profile_capital: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    invite_capital: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    solo_account_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    shared_account_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    solo_profile_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    shared_profile_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    invite_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=2)
    expiry_notice_days: Mapped[int] = mapped_column(Integer, default=3)
    subscription_duration_days: Mapped[int] = mapped_column(Integer, default=30)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    low_stock_alert_active: Mapped[bool] = mapped_column(Boolean, default=False)


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    email_enc: Mapped[str] = mapped_column(Text)
    password_enc: Mapped[str] = mapped_column(Text)
    solo_buyer_limit: Mapped[int] = mapped_column(Integer, default=1)
    shared_buyer_limit: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="available", index=True)
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiry_alert_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: now())
    product = relationship("Product")


class ProfileSlot(Base):
    __tablename__ = "profile_slots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    profile_enc: Mapped[str] = mapped_column(Text)
    pin_enc: Mapped[str] = mapped_column(Text, default="")
    solo_limit: Mapped[int] = mapped_column(Integer, default=1)
    shared_limit: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    account = relationship("Account")


class DeliveryTemplate(Base):
    __tablename__ = "delivery_templates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    delivery_type: Mapped[str] = mapped_column(String(30))
    template_text: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (UniqueConstraint("product_id", "delivery_type"),)


class Sale(Base):
    __tablename__ = "sales"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_code: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    profile_slot_id: Mapped[int | None] = mapped_column(ForeignKey("profile_slots.id"), nullable=True)
    admin_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), index=True)
    buyer: Mapped[str] = mapped_column(String(200))
    buyer_email: Mapped[str] = mapped_column(String(254), default="")
    sale_type: Mapped[str] = mapped_column(String(30))
    selling_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    capital: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    profit: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(24), default="reserved", index=True)
    reserved_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refunded_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    inventory_released: Mapped[bool] = mapped_column(Boolean, default=False)
    buyer_duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: now())
    product = relationship("Product")
    account = relationship("Account")
    profile_slot = relationship("ProfileSlot")


class BuyerSubscription(Base):
    """Buyer-facing subscription term; independent from supplier account expiry."""
    __tablename__ = "buyer_subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id"), unique=True, index=True)
    buyer: Mapped[str] = mapped_column(String(200), index=True)
    buyer_email: Mapped[str] = mapped_column(String(254), default="", index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    duration_days: Mapped[int] = mapped_column(Integer)
    starts_on: Mapped[date] = mapped_column(Date, index=True)
    expires_on: Mapped[date] = mapped_column(Date, index=True)
    expiry_notice_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    expired_notice_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: now())
    sale = relationship("Sale")
    product = relationship("Product")


class CancellationRequest(Base):
    __tablename__ = "cancellation_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id"), index=True)
    requester_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: now())


class PayrollPeriod(Base):
    __tablename__ = "payroll_periods"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_start: Mapped[date] = mapped_column(Date, index=True)
    period_end: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(30), default="pending_owner_approval")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: now())
    __table_args__ = (UniqueConstraint("period_start", "period_end"),)


class PayrollLine(Base):
    __tablename__ = "payroll_lines"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("payroll_periods.id"), index=True)
    admin_id: Mapped[int] = mapped_column(BigInteger)
    total_sales: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    total_capital: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    total_profit: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    commission_rate: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=0)
    salary: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    bonus: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    bonus_note: Mapped[str] = mapped_column(Text, default="")


class CommissionTier(Base):
    __tablename__ = "commission_tiers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sales_threshold: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    commission_rate: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)


class ActivityLog(Base):
    __tablename__ = "activity_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(100), index=True)
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: now())


def now() -> datetime:
    return datetime.now(TIMEZONE)


def money(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Amount must be a finite number")
    return result.quantize(Decimal("0.01"))


def enc(value: str) -> str:
    return cipher.encrypt((value or "").encode("utf-8")).decode("ascii")


def dec(value: str) -> str:
    if not value:
        return ""
    try:
        return cipher.decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("Credential could not be decrypted. Check that ENCRYPTION_KEY has not changed.") from exc


def log_action(db, actor_id: int, action: str, details: str = "") -> None:
    db.add(ActivityLog(actor_id=actor_id, action=action, details=details[:4000]))


def role_of(db, uid: int) -> str | None:
    if uid == OWNER_ID:
        return "owner"
    user = db.get(User, uid)
    return "admin" if user and user.active and user.role == "admin" else None


def require_owner(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == OWNER_ID)


def tier_rate(total_sales: Decimal, db) -> Decimal:
    tiers = db.scalars(
        select(CommissionTier).order_by(CommissionTier.sales_threshold.desc())
    ).all()
    for tier in tiers:
        if total_sales >= tier.sales_threshold:
            return Decimal(tier.commission_rate)
    return Decimal("0")


COMMISSION_SELECT, COMMISSION_THRESHOLD, COMMISSION_RATE = range(50, 53)


def commission_settings_text(db) -> str:
    tiers = db.scalars(
        select(CommissionTier).order_by(CommissionTier.sales_threshold)
    ).all()
    lines = ["💸 COMMISSION TIER SETTINGS", "Weekly sales minimum → commission rate", ""]
    for index, tier in enumerate(tiers, 1):
        lines.append(
            f"Tier {index}: ₱{tier.sales_threshold:,.2f} → "
            f"{Decimal(tier.commission_rate) * 100:.2f}%"
        )
    lines.extend([
        "",
        "Choose a tier below to edit its weekly sales minimum and commission percentage.",
        "Changes affect current estimates and future payroll calculations. Already closed payroll periods stay unchanged.",
    ])
    return "\n".join(lines)


async def commission_settings_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return ConversationHandler.END
    if not update.effective_chat or update.effective_chat.type != "private":
        await q.edit_message_text("For security, use the bot in a private chat.")
        return ConversationHandler.END
    with Session() as db:
        tiers = db.scalars(select(CommissionTier).order_by(CommissionTier.sales_threshold)).all()
        body = commission_settings_text(db)
        buttons = [
            [InlineKeyboardButton(
                f"Edit Tier {index}",
                callback_data=f"commission:edit:{tier.id}",
            )]
            for index, tier in enumerate(tiers, 1)
        ]
    buttons.append([InlineKeyboardButton("🏠 Main Menu", callback_data="owner:home")])
    await q.edit_message_text(body, reply_markup=InlineKeyboardMarkup(buttons))
    return COMMISSION_SELECT


async def commission_tier_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return ConversationHandler.END
    try:
        tier_id = int(q.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await q.edit_message_text("Invalid commission tier. Open Commission Settings again.")
        return ConversationHandler.END
    with Session() as db:
        tier = db.get(CommissionTier, tier_id)
        if not tier:
            await q.edit_message_text("Commission tier not found. Open Commission Settings again.")
            return ConversationHandler.END
        context.user_data["commission_edit"] = {"tier_id": tier_id}
        threshold = Decimal(tier.sales_threshold)
    await q.edit_message_text(
        f"Editing commission tier.\nCurrent weekly sales minimum: ₱{threshold:,.2f}\n\n"
        "Send the new minimum weekly sales amount in pesos (numbers only, at least 1).",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="commission:cancel")]]),
    )
    return COMMISSION_THRESHOLD


async def commission_threshold_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not require_owner(update):
        await update.effective_message.reply_text("Owner permission required.")
        return ConversationHandler.END
    wizard = context.user_data.get("commission_edit")
    if not wizard:
        await update.effective_message.reply_text("Commission setup expired. Open Commission Settings again.")
        return ConversationHandler.END
    try:
        threshold = Decimal((update.effective_message.text or "").strip().replace(",", "").replace("₱", ""))
        if not threshold.is_finite() or threshold < Decimal("1") or threshold > Decimal("1000000000"):
            raise InvalidOperation
        threshold = threshold.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        await update.effective_message.reply_text("Enter a valid amount of at least ₱1. Example: 1500")
        return COMMISSION_THRESHOLD
    with Session() as db:
        duplicate = db.scalar(select(CommissionTier).where(
            CommissionTier.sales_threshold == threshold,
            CommissionTier.id != wizard["tier_id"],
        ))
        if duplicate:
            await update.effective_message.reply_text(
                "Another tier already uses that sales minimum. Enter a different amount."
            )
            return COMMISSION_THRESHOLD
        tier = db.get(CommissionTier, wizard["tier_id"])
        if not tier:
            await update.effective_message.reply_text("Commission tier not found. Start again.")
            context.user_data.pop("commission_edit", None)
            return ConversationHandler.END
        wizard["sales_threshold"] = str(threshold)
        current_rate = Decimal(tier.commission_rate) * 100
    await update.effective_message.reply_text(
        f"Sales minimum saved for this edit: ₱{threshold:,.2f}\n"
        f"Current commission: {current_rate:.2f}%\n\n"
        "Now send the commission percentage (0–100). Example: 35",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="commission:cancel")]]),
    )
    return COMMISSION_RATE


async def commission_rate_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not require_owner(update):
        await update.effective_message.reply_text("Owner permission required.")
        return ConversationHandler.END
    wizard = context.user_data.get("commission_edit")
    if not wizard or "sales_threshold" not in wizard:
        await update.effective_message.reply_text("Commission setup expired. Open Commission Settings again.")
        context.user_data.pop("commission_edit", None)
        return ConversationHandler.END
    try:
        percent = Decimal((update.effective_message.text or "").strip().replace("%", ""))
        if not percent.is_finite() or percent < Decimal("0") or percent > Decimal("100"):
            raise InvalidOperation
        rate = (percent / Decimal("100")).quantize(Decimal("0.0001"))
    except (InvalidOperation, ValueError):
        await update.effective_message.reply_text("Enter a percentage from 0 to 100. Example: 35")
        return COMMISSION_RATE
    with Session() as db:
        tier = db.get(CommissionTier, wizard["tier_id"])
        if not tier:
            await update.effective_message.reply_text("Commission tier not found. Start again.")
            context.user_data.pop("commission_edit", None)
            return ConversationHandler.END
        tier.sales_threshold = Decimal(wizard["sales_threshold"])
        tier.commission_rate = rate
        log_action(
            db, OWNER_ID, "commission_tier_updated",
            f"tier_id={tier.id}; sales_threshold={tier.sales_threshold}; commission_rate={rate}",
        )
        db.commit()
        body = commission_settings_text(db)
        tiers = db.scalars(select(CommissionTier).order_by(CommissionTier.sales_threshold)).all()
        buttons = [
            [InlineKeyboardButton(f"Edit Tier {index}", callback_data=f"commission:edit:{row.id}")]
            for index, row in enumerate(tiers, 1)
        ]
    context.user_data.pop("commission_edit", None)
    buttons.append([InlineKeyboardButton("🏠 Main Menu", callback_data="owner:home")])
    await update.effective_message.reply_text(
        f"✅ Commission tier updated.\n\n{body}",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ConversationHandler.END


async def commission_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return ConversationHandler.END
    context.user_data.pop("commission_edit", None)
    await q.edit_message_text(
        "Commission tier editing cancelled. No changes were saved.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="owner:home")]]),
    )
    return ConversationHandler.END


async def commission_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("commission_edit", None)
    await update.effective_message.reply_text("Commission tier editing cancelled. No changes were saved.")
    return ConversationHandler.END


def current_week_bounds(day: date | None = None) -> tuple[date, date]:
    """Saturday through Friday, inclusive, using Manila calendar dates."""
    day = day or now().date()
    days_since_saturday = (day.weekday() - 5) % 7
    start = day - timedelta(days=days_since_saturday)
    return start, start + timedelta(days=6)


def period_datetimes(start: date, end: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(start, time.min, tzinfo=TIMEZONE),
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=TIMEZONE),
    )


def sale_fields(product: Product, sale_type: str) -> tuple[Decimal, Decimal]:
    mapping = {
        "solo_account": (product.solo_account_price, product.solo_account_capital),
        "shared_account": (product.shared_account_price, product.shared_account_capital),
        "solo_profile": (product.solo_profile_price, product.solo_profile_capital),
        "shared_profile": (product.shared_profile_price, product.shared_profile_capital),
        "invite": (product.invite_price, product.invite_capital),
    }
    return mapping[sale_type]


def allowed_types(mode: str) -> list[str]:
    # Both intentionally means Solo Account + Shared Account only.
    modes = {
        "solo_profile": ["solo_profile"],
        "shared_profile": ["shared_profile"],
        "solo_account": ["solo_account"],
        "shared_account": ["shared_account"],
        "both": ["solo_account", "shared_account"],
        "invite": ["invite"],
        "solo": ["solo_account", "solo_profile"],
        "shared": ["shared_account", "shared_profile"],
    }
    return modes.get(mode, [])


def nice_type(value: str) -> str:
    return value.replace("_", " ").title()


def week_sales_query(db, admin_id: int, start: date, end: date):
    start_dt, end_dt = period_datetimes(start, end)
    return db.scalars(
        select(Sale).where(
            Sale.admin_id == admin_id,
            Sale.status.in_(["completed", "partially_refunded", "refunded"]),
            Sale.completed_at >= start_dt,
            Sale.completed_at < end_dt,
        )
    ).all()


def net_sale_total(sales: list[Sale]) -> Decimal:
    return sum((max(Decimal("0"), s.selling_price - s.refunded_amount) for s in sales), Decimal("0"))


def count_used(db, model_id: int, sale_type: str, field: str) -> int:
    col = Sale.account_id if field == "account" else Sale.profile_slot_id
    return db.scalar(select(func.count(Sale.id)).where(
        col == model_id,
        Sale.sale_type == sale_type,
        Sale.status.in_(["reserved", "completed", "partially_refunded", "refunded", "cancelled"]),
        Sale.inventory_released.is_(False),
        ((Sale.reserved_until.is_(None)) | (Sale.reserved_until > now()) | (Sale.status != "reserved")),
    )) or 0


def make_code() -> str:
    return "INV-" + secrets.token_hex(6).upper()


# ----- Telegram access and menus -----
async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Setup cancelled. Nothing was saved.")
    return ConversationHandler.END


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    if not tg or not update.message:
        return
    with Session() as db:
        if tg.id == OWNER_ID:
            user = db.get(User, tg.id)
            if not user:
                db.add(User(telegram_id=tg.id, display_name=tg.full_name, username=tg.username or "", role="owner", active=True))
            else:
                user.role, user.active = "owner", True
                user.display_name, user.username = tg.full_name, tg.username or ""
            db.commit()
            await owner_menu(update, context)
            return
        user = db.get(User, tg.id)
        if user and user.active and user.role == "admin":
            user.display_name, user.username = tg.full_name, tg.username or ""
            db.commit()
            await admin_menu(update, context)
            return
        if not user:
            db.add(User(telegram_id=tg.id, display_name=tg.full_name, username=tg.username or "", role="pending", active=True))
            log_action(db, tg.id, "access_requested", tg.full_name)
            db.commit()
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Approve", callback_data=f"access:approve:{tg.id}"),
                InlineKeyboardButton("Reject", callback_data=f"access:reject:{tg.id}"),
            ]])
            try:
                await context.bot.send_message(
                    OWNER_ID,
                    f"New access request\nName: {tg.full_name}\nUsername: @{tg.username or 'none'}\nTelegram ID: {tg.id}",
                    reply_markup=keyboard,
                )
            except Exception:
                log.exception("Could not notify owner of access request")
        status = "Your access request is pending owner approval." if user is None or user.role == "pending" else "Your access is not active. Contact the owner."
        await update.message.reply_text(status + " You cannot use admin commands until approved.")


async def owner_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("♡ Add Product", callback_data="owner:newproduct"),
         InlineKeyboardButton("Edit Product", callback_data="owner:editproduct")],
        [InlineKeyboardButton("Add Stock", callback_data="owner:stock"),
         InlineKeyboardButton("Manage Profiles", callback_data="owner:addprofile")],
        [InlineKeyboardButton("Delivery Templates", callback_data="owner:settemplate"),
         InlineKeyboardButton("View Products", callback_data="owner:products")],
        [InlineKeyboardButton("Stock Overview", callback_data="owner:stockview"),
         InlineKeyboardButton("Admin Team", callback_data="owner:admins")],
        [InlineKeyboardButton("Sales", callback_data="owner:sales"),
         InlineKeyboardButton("Buyer Monitoring", callback_data="owner:buyers")],
        [InlineKeyboardButton("Payroll", callback_data="owner:payroll")],
        [InlineKeyboardButton("Commission Settings", callback_data="owner:commission")],
        [InlineKeyboardButton("Reports & Ranking", callback_data="owner:reports"),
         InlineKeyboardButton("Activity Log", callback_data="owner:audit")],
    ])
    display_name = update.effective_user.full_name if update.effective_user else "there"
    await update.effective_message.reply_text(
        f"♡ {display_name}  |  OWNER\nA little space to manage everything.",
        reply_markup=keyboard,
    )


async def owner_dashboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        await q.edit_message_text("For security, use the bot in a private chat.")
        return
    action = q.data.split(":", 1)[1]
    pages = {
        "stock": ("📦 ADD STOCK", "Use /stock to add an account. Required fields: product ID, email, password, profile, PIN, solo limit, shared limit and expiry date. Use - for an empty profile, PIN or expiry."),
        "addprofile": ("🗂 ADD PROFILE", "Use /addprofile to add a profile to an existing account. Required: account ID, profile name, PIN, solo limit (1), shared limit."),
        "sales": ("🛒 SALES", "Use /sale to start a sale. The bot will guide you through product, sale type and buyer details."),
        "payroll": ("💰 PAYROLL", "Use /payroll to view the weekly payroll. Use /approvepayroll and /markpaid for owner approval and payment marking."),
        "reports": ("📈 REPORTS & RANKING", "Use /report for the weekly report and /ranking for admin sales ranking."),
        "audit": ("🧾 ACTIVITY LOG", "Use /audit to review recent owner/admin activity."),
    }
    if action == "buyers":
        await send_buyer_monitoring(q, context)
        return
    if action in {"products", "stockview", "admins"}:
        if action == "products":
            with Session() as db:
                rows = db.scalars(select(Product).where(Product.active.is_(True)).order_by(Product.name)).all()
                body = "\n".join([f"#{p.id} {p.name} — {p.mode}" for p in rows]) or "No products yet."
                product_buttons = [[InlineKeyboardButton(f"🗑 Delete {p.name} (#{p.id})", callback_data=f"productdelete:ask:{p.id}")] for p in rows]
            title = "📋 PRODUCTS"
        elif action == "stockview":
            with Session() as db:
                rows = db.scalars(select(Product).where(Product.active.is_(True)).order_by(Product.name)).all()
                buttons = [[InlineKeyboardButton(f"📦 {p.name}", callback_data=f"stockdetail:product:{p.id}")] for p in rows]
                buttons.append([InlineKeyboardButton("🏠 Main Menu", callback_data="owner:home")])
            await q.edit_message_text(
                "📊 STOCK DETAILS\\nChoose a product to inspect available, reserved and sold stock, stock records, and sales history.",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return
        else:
            with Session() as db:
                users = db.scalars(select(User).where(User.role.in_(["admin", "pending"])).order_by(User.role, User.display_name)).all()
                body = "\n".join([f"{u.role.upper()} — {u.display_name} — ID {u.telegram_id}" for u in users]) or "No pending users or admins."
            title = "👥 ADMINS"
        if action == "products":
            product_buttons.append([InlineKeyboardButton("🏠 Main Menu", callback_data="owner:home")])
            keyboard = InlineKeyboardMarkup(product_buttons)
        else:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="owner:home")]])
        await q.edit_message_text(f"{title}\n\n{body}", reply_markup=keyboard)
        return
    if action == "home":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("♡ Add Product", callback_data="owner:newproduct"), InlineKeyboardButton("Edit Product", callback_data="owner:editproduct")],
            [InlineKeyboardButton("Add Stock", callback_data="owner:stock"), InlineKeyboardButton("Manage Profiles", callback_data="owner:addprofile")],
            [InlineKeyboardButton("Delivery Templates", callback_data="owner:settemplate"), InlineKeyboardButton("View Products", callback_data="owner:products")],
            [InlineKeyboardButton("Stock Overview", callback_data="owner:stockview"), InlineKeyboardButton("Admin Team", callback_data="owner:admins")],
            [InlineKeyboardButton("Sales", callback_data="owner:sales"), InlineKeyboardButton("Buyer Monitoring", callback_data="owner:buyers")],
            [InlineKeyboardButton("Payroll", callback_data="owner:payroll")],
         [InlineKeyboardButton("Commission Settings", callback_data="owner:commission")],
            [InlineKeyboardButton("Reports & Ranking", callback_data="owner:reports"), InlineKeyboardButton("Activity Log", callback_data="owner:audit")],
        ])
        display_name = update.effective_user.full_name if update.effective_user else "there"
        await q.edit_message_text(f"♡ {display_name}  |  OWNER\nA little space to manage everything.", reply_markup=keyboard)
        return
    title, body = pages.get(action, ("OWNER DASHBOARD", "Choose an option."))
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="owner:home")]])
    await q.edit_message_text(f"{title}\n\n{body}", reply_markup=keyboard)

async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("♡ New Sale", callback_data="admin:sale")],
        [InlineKeyboardButton("Products & Prices", callback_data="admin:products"),
         InlineKeyboardButton("Available Stock", callback_data="admin:stockview")],
        [InlineKeyboardButton("My Report", callback_data="admin:report"),
         InlineKeyboardButton("Sales Ranking", callback_data="admin:ranking")],
        [InlineKeyboardButton("My Sales", callback_data="admin:mysales"),
         InlineKeyboardButton("My Payroll", callback_data="admin:payroll")],
    ])
    display_name = update.effective_user.full_name if update.effective_user else "there"
    await update.effective_message.reply_text(
        f"♡ {display_name}  |  TEAM\nYour sales workspace, kept simple.",
        reply_markup=keyboard,
    )


async def admin_dashboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    with Session() as db:
        role = role_of(db, update.effective_user.id)
    if role != "admin":
        await q.edit_message_text("Your admin access is not active.")
        return
    action = q.data.split(":", 1)[1]
    if action == "sale":
        await sale_start(update, context)
        return
    if action == "home":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("♡ New Sale", callback_data="admin:sale")],
            [InlineKeyboardButton("Products & Prices", callback_data="admin:products"),
             InlineKeyboardButton("Available Stock", callback_data="admin:stockview")],
            [InlineKeyboardButton("My Report", callback_data="admin:report"),
             InlineKeyboardButton("Sales Ranking", callback_data="admin:ranking")],
            [InlineKeyboardButton("My Sales", callback_data="admin:mysales"),
             InlineKeyboardButton("My Payroll", callback_data="admin:payroll")],
        ])
        display_name = update.effective_user.full_name if update.effective_user else "there"
        await q.edit_message_text(f"♡ {display_name}  |  TEAM\nYour sales workspace, kept simple.", reply_markup=keyboard)
        return
    if action == "payroll":
        uid = update.effective_user.id
        start, end = current_week_bounds()
        with Session() as db:
            current_sales = week_sales_query(db, uid, start, end)
            current_total = net_sale_total(current_sales)
            current_capital = sum((sale.capital for sale in current_sales), Decimal("0"))
            current_profit = current_total - current_capital
            current_rate = tier_rate(current_total, db)
            current_salary = max(Decimal("0"), current_profit * current_rate)
            periods = db.scalars(
                select(PayrollPeriod).order_by(PayrollPeriod.id.desc()).limit(5)
            ).all()
            lines = [
                "💰 MY PAYROLL OVERVIEW",
                f"Current week: {start} to {end}",
                f"Completed transactions: {len(current_sales)}",
                f"Sales: ₱{current_total:.2f}",
                f"Capital: ₱{current_capital:.2f}",
                f"Profit: ₱{current_profit:.2f}",
                f"Commission tier: {current_rate * 100:.0f}%",
                f"Estimated salary (commission only): ₱{current_salary:.2f}",
                "Final payroll is calculated on Friday and requires owner approval.",
                "",
                "PREVIOUS PAYROLL PERIODS",
            ]
            found = False
            for period in periods:
                payroll_line = db.scalar(select(PayrollLine).where(
                    PayrollLine.period_id == period.id,
                    PayrollLine.admin_id == uid,
                ))
                if not payroll_line:
                    continue
                found = True
                payout = payroll_line.salary + payroll_line.bonus
                lines.append(
                    f"#{period.id} | {period.period_start} to {period.period_end}\n"
                    f"Status: {period.status.replace('_', ' ').upper()}\n"
                    f"Sales: ₱{payroll_line.total_sales:.2f} | Profit: ₱{payroll_line.total_profit:.2f}\n"
                    f"Commission: {payroll_line.commission_rate * 100:.0f}% | "
                    f"Salary: ₱{payroll_line.salary:.2f} | Bonus: ₱{payroll_line.bonus:.2f}\n"
                    f"Total: ₱{payout:.2f}"
                )
            if not found:
                lines.append("No previous payroll record yet.")
        await q.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh", callback_data="admin:payroll"),
                InlineKeyboardButton("🏠 Main Menu", callback_data="admin:home"),
            ]]),
        )
        return
    back = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="admin:home")]])
    uid = update.effective_user.id
    if action == "products":
        with Session() as db:
            rows = db.scalars(select(Product).where(Product.active.is_(True)).order_by(Product.name)).all()
            lines = ["📦 PRODUCTS & SELLING PRICES"]
            for p in rows:
                lines.append(f"\n#{p.id} {p.name} [{p.mode}]")
                lines.append(f"Solo account: ₱{p.solo_account_price:.2f} | Shared account: ₱{p.shared_account_price:.2f}")
                lines.append(f"Solo profile: ₱{p.solo_profile_price:.2f} | Shared profile: ₱{p.shared_profile_price:.2f} | Invite: ₱{p.invite_price:.2f}")
            if not rows:
                lines.append("No active products yet.")
        await q.edit_message_text("\n".join(lines), reply_markup=back)
        return
    if action == "stockview":
        with Session() as db:
            rows = db.scalars(select(Product).where(Product.active.is_(True)).order_by(Product.name)).all()
            lines = ["📊 AVAILABLE STOCK"]
            for p in rows:
                accounts = db.scalar(select(func.count(Account.id)).where(
                    Account.product_id == p.id, Account.status == "available",
                    or_(Account.expires_on.is_(None), Account.expires_on >= now().date())
                )) or 0
                profiles = db.scalar(select(func.count(ProfileSlot.id)).join(Account).where(
                    Account.product_id == p.id, ProfileSlot.active.is_(True)
                )) or 0
                if p.mode == "invite":
                    invite_accounts = db.scalars(select(Account).where(
                        Account.product_id == p.id, Account.status == "available",
                        or_(Account.expires_on.is_(None), Account.expires_on >= now().date())
                    )).all()
                    invite_units = sum(max(0, a.shared_buyer_limit - count_used(db, a.id, "invite", "account")) for a in invite_accounts)
                    lines.append(f"{p.name}: {invite_units} invite(s) available across {len(invite_accounts)} source account(s)")
                else:
                    lines.append(f"{p.name}: {accounts} account(s), {profiles} profile slot(s)")
            if len(lines) == 1:
                lines.append("No active products.")
        await q.edit_message_text("\n".join(lines), reply_markup=back)
        return
    if action == "report":
        start, end = current_week_bounds()
        with Session() as db:
            sales = week_sales_query(db, uid, start, end)
            total = net_sale_total(sales)
            capital = sum((sale.capital for sale in sales), Decimal("0"))
            profit = total - capital
            rate = tier_rate(total, db)
            salary = max(Decimal("0"), profit * rate)
            today_start = datetime.combine(now().date(), time.min, tzinfo=TIMEZONE)
            today_end = today_start + timedelta(days=1)
            today_sales = [sale for sale in sales if sale.completed_at and today_start <= sale.completed_at < today_end]
            today_total = net_sale_total(today_sales)
        report_text = (
            f"📈 MY WEEKLY REPORT ({start:%b %d}–{end:%b %d})\n"
            f"Completed transactions: {len(sales)}\nSales: ₱{total:.2f}\n"
            f"Capital: ₱{capital:.2f}\nProfit: ₱{profit:.2f}\n"
            f"Commission tier: {rate*100:.0f}%\nEstimated salary: ₱{salary:.2f}\n"
            f"Weekly quota: ₱{total:.2f} / ₱500.00\n\nToday's sales: ₱{today_total:.2f}"
        )
        await q.edit_message_text(report_text, reply_markup=back)
        return
    if action == "mysales":
        with Session() as db:
            query = select(Sale).order_by(Sale.created_at.desc()).limit(15)
            if uid != OWNER_ID:
                query = query.where(Sale.admin_id == uid)
            rows = db.scalars(query).all()
            lines = ["🧾 MY RECENT SALES"]
            for sale in rows:
                lines.append(
                    f"{sale.transaction_code} | {sale.status} | {sale.product.name} | "
                    f"{nice_type(sale.sale_type)} | ₱{sale.selling_price:.2f} | {sale.buyer}"
                )
            if not rows:
                lines.append("No transactions found.")
        await q.edit_message_text("\n".join(lines), reply_markup=back)
        return
    if action == "ranking":
        start, end = current_week_bounds()
        with Session() as db:
            users = db.scalars(select(User).where(User.role == "admin", User.active.is_(True))).all()
            values = []
            for user in users:
                user_sales = week_sales_query(db, user.telegram_id, start, end)
                values.append((user.display_name or str(user.telegram_id), net_sale_total(user_sales), len(user_sales)))
            values.sort(key=lambda item: item[1], reverse=True)
        lines = [f"🏆 WEEKLY ADMIN RANKING ({start:%b %d}–{end:%b %d})"]
        lines.extend(f"{i}. {name}: ₱{amount:.2f} ({count} transaction(s))" for i, (name, amount, count) in enumerate(values, 1))
        await q.edit_message_text("\n".join(lines) if len(lines) > 1 else "No active admins yet.", reply_markup=back)
        return
    await q.edit_message_text("Choose an option, lovely. 💗", reply_markup=back)


async def owner_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not require_owner(update):
        await update.effective_message.reply_text("Owner permission required.")
        return False
    if not update.effective_chat or update.effective_chat.type != "private":
        await update.effective_message.reply_text("For security, use the bot in a private chat. Owner commands are disabled in groups.")
        return False
    return True


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_chat or update.effective_chat.type != "private":
        await update.effective_message.reply_text("For security, use the bot in a private chat. Admin commands are disabled in groups.")
        return False
    with Session() as db:
        if not role_of(db, update.effective_user.id):
            await update.effective_message.reply_text("You are not authorized. Ask the owner to approve your Telegram ID.")
            return False
    return True


async def approve_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_command(update, context): return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /approve TELEGRAM_USER_ID")
        return
    uid = int(context.args[0])
    with Session() as db:
        user = db.get(User, uid)
        if not user:
            await update.message.reply_text("User not found. Ask them to send /start first.")
            return
        user.role, user.active = "admin", True
        log_action(db, OWNER_ID, "user_approved", str(uid))
        db.commit()
    await update.message.reply_text(f"Approved {uid} as Admin.")
    try: await context.bot.send_message(uid, "Your access has been approved. Send /start to open the dashboard.")
    except Exception: pass


async def reject_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_command(update, context): return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /reject TELEGRAM_USER_ID")
        return
    uid = int(context.args[0])
    with Session() as db:
        user = db.get(User, uid)
        if user:
            user.role, user.active = "rejected", False
            log_action(db, OWNER_ID, "user_rejected", str(uid))
            db.commit()
    await update.message.reply_text(f"Rejected request from {uid}.")


async def make_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_command(update, context): return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /makeadmin TELEGRAM_USER_ID")
        return
    uid = int(context.args[0])
    with Session() as db:
        user = db.get(User, uid)
        if not user:
            await update.message.reply_text("User must send /start first.")
            return
        if uid == OWNER_ID:
            await update.message.reply_text("Owner already has full access.")
            return
        user.role, user.active = "admin", True
        log_action(db, OWNER_ID, "admin_assigned", str(uid))
        db.commit()
    await update.message.reply_text(f"User {uid} is now an admin.")


async def revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_command(update, context): return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /revoke TELEGRAM_USER_ID")
        return
    uid = int(context.args[0])
    if uid == OWNER_ID:
        await update.message.reply_text("Owner access cannot be revoked.")
        return
    with Session() as db:
        user = db.get(User, uid)
        if user:
            user.role, user.active = "revoked", False
            log_action(db, OWNER_ID, "access_revoked", str(uid))
            db.commit()
    await update.message.reply_text(f"Access revoked for {uid}.")


async def access_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != OWNER_ID:
        await q.edit_message_text("Only the owner can approve access requests.")
        return
    _, action, uid_text = q.data.split(":")
    uid = int(uid_text)
    with Session() as db:
        user = db.get(User, uid)
        if not user or user.role != "pending":
            await q.edit_message_text("This access request is no longer pending.")
            return
        if action == "approve":
            user.role, user.active = "admin", True
            notice = "Your access was approved. Send /start."
        else:
            user.role, user.active = "rejected", False
            notice = "Your access request was declined."
        log_action(db, OWNER_ID, "user_approved" if action == "approve" else "user_rejected", str(uid))
        db.commit()
    await q.edit_message_text(("Approved" if action == "approve" else "Rejected") + f" user {uid}.")
    try: await context.bot.send_message(uid, notice)
    except Exception: pass


# ----- Owner inventory setup -----
# ----- Guided product creation wizard (button-based, one question at a time) -----
PRODUCT_NAME, PRODUCT_MODE, PRODUCT_VALUES, PRODUCT_DURATION, PRODUCT_CONFIRM = range(10, 15)
PRODUCT_VALUE_FIELDS = {
    "solo_profile": [
        ("Solo Profile capital", "solo_profile_capital"),
        ("Solo Profile selling price", "solo_profile_price"),
    ],
    "shared_profile": [
        ("Shared Profile capital", "shared_profile_capital"),
        ("Shared Profile selling price", "shared_profile_price"),
    ],
    "solo_account": [
        ("Solo Account capital", "solo_account_capital"),
        ("Solo Account selling price", "solo_account_price"),
    ],
    "shared_account": [
        ("Shared Account capital", "shared_account_capital"),
        ("Shared Account selling price", "shared_account_price"),
    ],
    "both": [
        ("Solo Account capital", "solo_account_capital"),
        ("Shared Account capital", "shared_account_capital"),
        ("Solo Account selling price", "solo_account_price"),
        ("Shared Account selling price", "shared_account_price"),
    ],
    "invite": [
        ("Invite capital", "invite_capital"),
        ("Invite selling price", "invite_price"),
    ],
}

async def newproduct_wizard_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    if not await owner_command(update, context): return ConversationHandler.END
    wizard = {"values": [], "message_ids": [], "chat_id": update.effective_chat.id}
    track_wizard_message(wizard, update.effective_message)
    context.user_data["product_wizard"] = wizard
    msg = await update.effective_chat.send_message(
        "🛍️ NEW PRODUCT\n\nFirst, what is the product name?\nExample: CapCut Pro",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="prod:cancel")]]),
    )
    wizard["message_ids"].append(msg.message_id)
    return PRODUCT_NAME


def track_wizard_message(wizard: dict, message) -> None:
    if wizard is not None and message is not None:
        wizard.setdefault("message_ids", []).append(message.message_id)


async def cleanup_wizard_messages(context: ContextTypes.DEFAULT_TYPE, wizard: dict):
    chat_id = wizard.get("chat_id") if wizard else None
    if not chat_id:
        return
    for message_id in dict.fromkeys(wizard.get("message_ids", [])):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            # Telegram may reject deletion of older messages or messages the bot cannot delete.
            pass


async def cleanup_product_wizard(context: ContextTypes.DEFAULT_TYPE, wizard: dict):
    await cleanup_wizard_messages(context, wizard)

async def product_name_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wizard = context.user_data.get("product_wizard", {})
    track_wizard_message(wizard, update.effective_message)
    name = update.message.text.strip()
    if not name or len(name) > 120 or "|" in name:
        await update.message.reply_text("Please enter a product name (1–120 characters, without |).")
        return PRODUCT_NAME
    with Session() as db:
        exists = db.scalar(select(Product.id).where(func.lower(Product.name) == name.lower()))
    if exists:
        await update.message.reply_text("That product already exists. Please enter a different name.")
        return PRODUCT_NAME
    context.user_data["product_wizard"]["name"] = name
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Solo Profile", callback_data="prodmode:solo_profile"),
         InlineKeyboardButton("Shared Profile", callback_data="prodmode:shared_profile")],
        [InlineKeyboardButton("Solo Account", callback_data="prodmode:solo_account"),
         InlineKeyboardButton("Shared Account", callback_data="prodmode:shared_account")],
        [InlineKeyboardButton("Both (Solo + Shared Account)", callback_data="prodmode:both")],
        [InlineKeyboardButton("Invite", callback_data="prodmode:invite")],
        [InlineKeyboardButton("❌ Cancel", callback_data="prod:cancel")],
    ])
    msg = await update.message.reply_text(f"Product: {name}\n\nChoose which sale mode this product supports:", reply_markup=keyboard)
    wizard.setdefault("message_ids", []).append(msg.message_id)
    return PRODUCT_MODE

async def product_mode_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    wizard = context.user_data.get("product_wizard", {})
    if q.message:
        wizard.setdefault("message_ids", []).append(q.message.message_id)
    mode = q.data.split(":")[1]
    context.user_data["product_wizard"]["mode"] = mode
    context.user_data["product_wizard"]["values"] = []
    context.user_data["product_wizard"]["value_index"] = 0
    fields = PRODUCT_VALUE_FIELDS[mode]
    context.user_data["product_wizard"]["fields"] = fields
    label = fields[0][0]
    await q.edit_message_text(
        f"✅ Mode: {mode.replace('_', ' ').title()}\n\nOnly the selected mode's costs and prices are needed.\n💰 Enter {label} in PHP.\nExample: 100",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="prod:cancel")]]),
    )
    return PRODUCT_VALUES

async def product_value_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wizard = context.user_data.get("product_wizard", {})
    if update.message:
        wizard.setdefault("message_ids", []).append(update.message.message_id)
    index = wizard.get("value_index", 0)
    try:
        amount = money(update.message.text.strip().replace(",", ""))
        if amount < 0: raise ValueError
    except Exception:
        await update.message.reply_text("Please enter a valid amount, e.g. 100 or 49.50.")
        return PRODUCT_VALUES
    wizard["values"].append(amount)
    index += 1
    wizard["value_index"] = index
    fields = wizard["fields"]
    if index < len(fields):
        label = fields[index][0]
        msg = await update.message.reply_text(
            f"Saved: ₱{amount:.2f}\n\n💰 Enter {label} in PHP.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="prod:cancel")]]),
        )
        wizard.setdefault("message_ids", []).append(msg.message_id)
        return PRODUCT_VALUES
    wizard["low_stock_threshold"] = 2
    wizard["expiry_notice_days"] = 3
    wizard["field_values"] = {field_key: value for (_, field_key), value in zip(fields, wizard["values"])}
    msg = await update.message.reply_text(
        "⏳ Choose the subscription duration for this product:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("7 days", callback_data="prodduration:7"),
             InlineKeyboardButton("14 days", callback_data="prodduration:14")],
            [InlineKeyboardButton("30 days (1 month)", callback_data="prodduration:30")],
            [InlineKeyboardButton("❌ Cancel", callback_data="prod:cancel")],
        ]),
    )
    track_wizard_message(wizard, msg)
    return PRODUCT_DURATION


async def product_duration_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return ConversationHandler.END
    days = int(q.data.rsplit(":", 1)[1])
    if days not in {7, 14, 30}:
        await q.edit_message_text("Invalid duration. Start product setup again.")
        return ConversationHandler.END
    wizard = context.user_data.get("product_wizard", {})
    wizard["subscription_duration_days"] = days
    if q.message:
        wizard.setdefault("message_ids", []).append(q.message.message_id)
    fields = wizard["fields"]
    summary = [
        f"🛍️ {wizard['name']}",
        f"Mode: {wizard['mode'].title()}",
        f"Subscription duration: {days} days",
        "",
        "CAPITAL",
        *[f"• {label}: ₱{wizard['field_values'].get(key, Decimal('0')):.2f}" for label, key in fields if key.endswith("_capital")],
        "",
        "SELLING PRICES",
        *[f"• {label}: ₱{wizard['field_values'].get(key, Decimal('0')):.2f}" for label, key in fields if key.endswith("_price")],
        "",
        "Other modes: Not enabled (₱0.00)",
        "Low-stock alert: 2 units",
        "Expiry notice: 3 days before expiry",
        "",
        "Save this product?",
    ]
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Save product", callback_data="prodconfirm:save"),
        InlineKeyboardButton("❌ Cancel", callback_data="prodconfirm:cancel"),
    ]])
    msg = await q.edit_message_text("\n".join(summary), reply_markup=keyboard)
    track_wizard_message(wizard, msg)
    return PRODUCT_CONFIRM

async def product_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return ConversationHandler.END
    context.user_data.pop("product_wizard", None)
    await q.edit_message_text(
        "Product setup cancelled. Nothing was saved.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Owner Menu", callback_data="owner:home")]]),
    )
    return ConversationHandler.END

async def product_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("product_wizard", None)
    await update.effective_message.reply_text("Product setup cancelled. Nothing was saved.")
    return ConversationHandler.END

async def product_confirm_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    wizard = context.user_data.get("product_wizard")
    if wizard and q.message:
        wizard.setdefault("message_ids", []).append(q.message.message_id)
    if q.data.endswith("cancel") or not wizard:
        context.user_data.pop("product_wizard", None)
        await q.edit_message_text("Product setup cancelled. Nothing was saved.")
        return ConversationHandler.END
    with Session() as db:
        if db.scalar(select(Product.id).where(func.lower(Product.name) == wizard["name"].lower())):
            await q.edit_message_text("That product name already exists. Nothing was saved.")
            context.user_data.pop("product_wizard", None)
            return ConversationHandler.END
        v = wizard["field_values"]
        product = Product(
            name=wizard["name"], mode=wizard["mode"],
            solo_account_capital=v.get("solo_account_capital", Decimal("0")),
            shared_account_capital=v.get("shared_account_capital", Decimal("0")),
            solo_profile_capital=v.get("solo_profile_capital", Decimal("0")),
            shared_profile_capital=v.get("shared_profile_capital", Decimal("0")),
            invite_capital=v.get("invite_capital", Decimal("0")),
            solo_account_price=v.get("solo_account_price", Decimal("0")),
            shared_account_price=v.get("shared_account_price", Decimal("0")),
            solo_profile_price=v.get("solo_profile_price", Decimal("0")),
            shared_profile_price=v.get("shared_profile_price", Decimal("0")),
            invite_price=v.get("invite_price", Decimal("0")),
            low_stock_threshold=2, expiry_notice_days=3,
            subscription_duration_days=wizard["subscription_duration_days"],
        )
        db.add(product)
        db.flush()
        for kind in ["solo_account", "solo_profile", "shared_profile", "shared_account", "invite"]:
            db.add(DeliveryTemplate(product_id=product.id, delivery_type=kind, template_text=""))
        log_action(db, OWNER_ID, "product_created", f"product_id={product.id}; name={product.name}; mode={product.mode}")
        db.commit()
        pid, name = product.id, product.name
    await cleanup_product_wizard(context, wizard)
    context.user_data.pop("product_wizard", None)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Set Delivery Template", callback_data="owner:settemplate")],
        [InlineKeyboardButton("➕ Add Another Product", callback_data="owner:newproduct")],
        [InlineKeyboardButton("🏠 Owner Menu", callback_data="owner:home")],
    ])
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ Product saved: {name} (ID #{pid})\n\nSetup messages have been cleaned up.",
        reply_markup=keyboard,
    )
    return ConversationHandler.END

EDIT_PRODUCT_SELECT = 30
EDIT_PRODUCT_FIELD = 31
EDIT_PRODUCT_VALUE = 32

EDIT_PRODUCT_FIELDS = {
    "name": ("Product Name", "text"),
    "solo_account_capital": ("Solo Account Capital", "money"),
    "shared_account_capital": ("Shared Account Capital", "money"),
    "solo_profile_capital": ("Solo Profile Capital", "money"),
    "shared_profile_capital": ("Shared Profile Capital", "money"),
    "invite_capital": ("Invite Capital", "money"),
    "solo_account_price": ("Solo Account Selling Price", "money"),
    "shared_account_price": ("Shared Account Selling Price", "money"),
    "solo_profile_price": ("Solo Profile Selling Price", "money"),
    "shared_profile_price": ("Shared Profile Selling Price", "money"),
    "invite_price": ("Invite Selling Price", "money"),
    "low_stock_threshold": ("Low-stock Alert Threshold", "int"),
    "expiry_notice_days": ("Expiry Notice Days", "int"),
    "subscription_duration_days": ("Buyer Subscription Duration (7, 14 or 30 days)", "int"),
}
EDIT_PRODUCT_MODES = (
    ("Solo Profile", "solo_profile"),
    ("Shared Profile", "shared_profile"),
    ("Solo Account", "solo_account"),
    ("Shared Account", "shared_account"),
    ("Both (Solo + Shared Account)", "both"),
    ("Invite", "invite"),
)

def edit_product_field_keyboard():
    rows = []
    for key, (label, _kind) in EDIT_PRODUCT_FIELDS.items():
        rows.append([InlineKeyboardButton(label, callback_data=f"editprod:field:{key}")])
    rows.append([InlineKeyboardButton("🔄 Change Sale Mode", callback_data="editprod:field:mode")])
    rows.append([InlineKeyboardButton("⬅️ Choose Another Product", callback_data="editprod:back")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="editprod:cancel")])
    return InlineKeyboardMarkup(rows)

def edit_product_value_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Settings", callback_data="editprod:backfields")],
        [InlineKeyboardButton("❌ Cancel", callback_data="editprod:cancel")],
    ])

async def edit_product_command_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_command(update, context):
        return ConversationHandler.END
    with Session() as db:
        products = db.scalars(select(Product).where(Product.active.is_(True)).order_by(Product.name)).all()
        buttons = [[InlineKeyboardButton(f"{p.name} (#{p.id})", callback_data=f"editprod:product:{p.id}")] for p in products]
    if not buttons:
        await update.effective_message.reply_text("There are no active products to edit.")
        return ConversationHandler.END
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="editprod:cancel")])
    await update.effective_message.reply_text("✏️ EDIT PRODUCT\nChoose the product you want to update:", reply_markup=InlineKeyboardMarkup(buttons))
    return EDIT_PRODUCT_SELECT

async def edit_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return ConversationHandler.END
    if not update.effective_chat or update.effective_chat.type != "private":
        await q.edit_message_text("For security, use the bot in a private chat.")
        return ConversationHandler.END
    with Session() as db:
        products = db.scalars(select(Product).where(Product.active.is_(True)).order_by(Product.name)).all()
        buttons = [[InlineKeyboardButton(f"{p.name} (#{p.id})", callback_data=f"editprod:product:{p.id}")] for p in products]
    if not buttons:
        await q.edit_message_text("There are no active products to edit.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="owner:home")]]))
        return ConversationHandler.END
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="editprod:cancel")])
    await q.edit_message_text("✏️ EDIT PRODUCT\nChoose the product you want to update:", reply_markup=InlineKeyboardMarkup(buttons))
    return EDIT_PRODUCT_SELECT

async def edit_product_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return ConversationHandler.END
    if q.data == "editprod:back":
        return await edit_product_start(update, context)
    pid = int(q.data.rsplit(":", 1)[1])
    with Session() as db:
        product = db.get(Product, pid)
        if not product or not product.active:
            await q.edit_message_text("Product not found or inactive.")
            return ConversationHandler.END
        context.user_data["editing_product_id"] = pid
        summary = f"✏️ EDITING: {product.name} (#{product.id})\nCurrent sale mode: {product.mode}\n\nChoose which setting to change:"
    await q.edit_message_text(summary, reply_markup=edit_product_field_keyboard())
    return EDIT_PRODUCT_FIELD

async def edit_product_choose_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return ConversationHandler.END
    if q.data == "editprod:back":
        return await edit_product_start(update, context)
    field = q.data.split(":", 2)[2]
    if field == "mode":
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(label, callback_data=f"editprod:mode:{value}")] for label, value in EDIT_PRODUCT_MODES]
            + [[InlineKeyboardButton("⬅️ Back to Settings", callback_data="editprod:backfields")], [InlineKeyboardButton("❌ Cancel", callback_data="editprod:cancel")]]
        )
        await q.edit_message_text("Choose the new sale mode:", reply_markup=keyboard)
        return EDIT_PRODUCT_FIELD
    if field not in EDIT_PRODUCT_FIELDS:
        await q.edit_message_text("Invalid setting. Please open Edit Product again.")
        return ConversationHandler.END
    label, kind = EDIT_PRODUCT_FIELDS[field]
    context.user_data["editing_product_field"] = field
    await q.edit_message_text(f"Enter the new value for {label}.\n" + ("Enter a number (example: 250 or 250.50)." if kind == "money" else "Enter a whole number." if kind == "int" else "Send the new product name."), reply_markup=edit_product_value_keyboard())
    return EDIT_PRODUCT_VALUE

async def edit_product_set_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return ConversationHandler.END
    mode = q.data.rsplit(":", 1)[1]
    if mode not in {value for _label, value in EDIT_PRODUCT_MODES}:
        await q.edit_message_text("Invalid sale mode.")
        return EDIT_PRODUCT_FIELD
    with Session() as db:
        product = db.get(Product, context.user_data.get("editing_product_id"))
        if not product:
            await q.edit_message_text("Product not found.")
            return ConversationHandler.END
        product.mode = mode
        log_action(db, OWNER_ID, "product_settings_updated", f"product_id={product.id}; mode={mode}")
        db.commit()
        name, pid = product.name, product.id
    await q.edit_message_text(f"✅ Sale mode for {name} updated to {mode}.\nChoose another setting:", reply_markup=edit_product_field_keyboard())
    return EDIT_PRODUCT_FIELD

async def edit_product_back_fields(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return ConversationHandler.END
    pid = context.user_data.get("editing_product_id")
    with Session() as db:
        product = db.get(Product, pid)
        if not product:
            await q.edit_message_text("Product not found.")
            return ConversationHandler.END
        await q.edit_message_text(f"✏️ EDITING: {product.name} (#{product.id})\nChoose which setting to change:", reply_markup=edit_product_field_keyboard())
    return EDIT_PRODUCT_FIELD


async def edit_product_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return ConversationHandler.END
    context.user_data.pop("editing_product_id", None)
    context.user_data.pop("editing_product_field", None)
    await q.edit_message_text(
        "Product editing cancelled. No further changes will be made.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Owner Menu", callback_data="owner:home")]]),
    )
    return ConversationHandler.END

async def edit_product_save_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not require_owner(update):
        await update.effective_message.reply_text("Owner permission required.")
        return ConversationHandler.END
    field = context.user_data.get("editing_product_field")
    if field not in EDIT_PRODUCT_FIELDS:
        await update.effective_message.reply_text("Edit session expired. Please tap Edit Product again.")
        return ConversationHandler.END
    label, kind = EDIT_PRODUCT_FIELDS[field]
    raw = (update.effective_message.text or "").strip()
    try:
        if kind == "money":
            value = money(raw)
            if value < 0: raise ValueError
        elif kind == "int":
            value = int(raw)
            if value < 0: raise ValueError
            if field == "subscription_duration_days" and value not in {7, 14, 30}:
                raise ValueError
        else:
            value = raw
            if not value or len(value) > 120: raise ValueError
    except Exception:
        await update.effective_message.reply_text(
            f"Invalid {label}. " + (
                "Choose 7, 14 or 30 days." if field == "subscription_duration_days"
                else f"Please enter a valid {'amount' if kind == 'money' else 'whole number' if kind == 'int' else 'name'}."
            )
        )
        return EDIT_PRODUCT_VALUE
    pid = context.user_data.get("editing_product_id")
    with Session() as db:
        product = db.get(Product, pid)
        if not product:
            await update.effective_message.reply_text("Product not found. Please start again.")
            return ConversationHandler.END
        if field == "name":
            duplicate = db.scalar(select(Product).where(Product.name == value, Product.id != pid))
            if duplicate:
                await update.effective_message.reply_text("That product name is already in use. Enter a different name.")
                return EDIT_PRODUCT_VALUE
        setattr(product, field, value)
        log_action(db, OWNER_ID, "product_settings_updated", f"product_id={pid}; field={field}; value={value}")
        db.commit()
        name = product.name
    await update.effective_message.reply_text(f"✅ {label} updated for {name}.\nChoose another setting:", reply_markup=edit_product_field_keyboard())
    return EDIT_PRODUCT_FIELD

async def editproduct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_command(update, context): return
    parts = [p.strip() for p in " ".join(context.args).split("|")]
    if len(parts) != 14:
        await update.message.reply_text(
            "Usage:\n/editproduct PRODUCT_ID|MODE|SOLO_ACCOUNT_CAPITAL|SHARED_ACCOUNT_CAPITAL|SOLO_PROFILE_CAPITAL|SHARED_PROFILE_CAPITAL|INVITE_CAPITAL|SOLO_ACCOUNT_PRICE|SHARED_ACCOUNT_PRICE|SOLO_PROFILE_PRICE|SHARED_PROFILE_PRICE|INVITE_PRICE|LOW_STOCK_THRESHOLD|EXPIRY_NOTICE_DAYS\n"
            "MODE: solo_profile, shared_profile, solo_account, shared_account, both (solo_account + shared_account), invite"
        )
        return
    try:
        pid = int(parts[0])
        mode = parts[1].lower()
        nums = [money(v) for v in parts[2:12]]
        threshold, expiry_days = int(parts[12]), int(parts[13])
        if mode not in {"solo_profile", "shared_profile", "solo_account", "shared_account", "both", "invite", "solo", "shared"} or any(v < 0 for v in nums) or threshold < 0 or expiry_days < 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("Invalid product ID, mode, capital, price, or alert threshold.")
        return
    with Session() as db:
        product = db.get(Product, pid)
        if not product:
            await update.message.reply_text("Product not found.")
            return
        product.mode = mode
        (product.solo_account_capital, product.shared_account_capital,
         product.solo_profile_capital, product.shared_profile_capital, product.invite_capital,
         product.solo_account_price, product.shared_account_price,
         product.solo_profile_price, product.shared_profile_price, product.invite_price) = nums
        product.low_stock_threshold = threshold
        product.expiry_notice_days = expiry_days
        log_action(db, OWNER_ID, "product_settings_updated", f"product_id={pid}; mode={mode}")
        db.commit()
    await update.message.reply_text(f"Product #{pid} settings updated.")


# ----- Guided Add Stock wizard -----
STOCK_PRODUCT, STOCK_EMAIL, STOCK_PASSWORD, STOCK_PROFILE, STOCK_PIN, STOCK_LIMIT, STOCK_EXPIRY, STOCK_CONFIRM = range(30, 38)

async def stock_wizard_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    if not await owner_command(update, context):
        return ConversationHandler.END
    stock_wizard = {"chat_id": update.effective_chat.id, "message_ids": []}
    track_wizard_message(stock_wizard, update.effective_message)
    context.user_data["stock_wizard"] = stock_wizard
    with Session() as db:
        products_rows = db.scalars(
            select(Product).where(Product.active.is_(True)).order_by(Product.name)
        ).all()
        buttons = [
            [InlineKeyboardButton(f"{p.name} (#{p.id})", callback_data=f"stockprod:{p.id}")]
            for p in products_rows
        ]
    if not buttons:
        await update.effective_message.reply_text(
            "No active products yet. Add a product first.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Main Menu", callback_data="owner:home")
            ]]),
        )
        return ConversationHandler.END
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="stock:cancel")])
    msg = await update.effective_message.reply_text(
        "📦 ADD STOCK\n\nChoose the product for this account:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    track_wizard_message(stock_wizard, msg)
    return STOCK_PRODUCT


async def stock_product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return ConversationHandler.END
    try:
        pid = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await q.edit_message_text("Invalid product selection. Start again.")
        return ConversationHandler.END
    with Session() as db:
        product = db.get(Product, pid)
        if not product or not product.active:
            await q.edit_message_text("Product not found or inactive. Start again.")
            return ConversationHandler.END
        name = product.name
        mode = product.mode
    wizard = context.user_data.get("stock_wizard") or {"chat_id": update.effective_chat.id, "message_ids": []}
    track_wizard_message(wizard, q.message)
    wizard.update({"product_id": pid, "product_name": name, "product_mode": mode, "solo_limit": 1})
    context.user_data["stock_wizard"] = wizard
    await q.edit_message_text(
        f"Product: {name}\n\nEnter the account email or username:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Back to Products", callback_data=f"stock:back:{STOCK_PRODUCT}"),
            InlineKeyboardButton("❌ Cancel", callback_data="stock:cancel")
        ]]),
    )
    return STOCK_EMAIL


async def stock_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str, back_state: int):
    wizard = context.user_data.get("stock_wizard")
    track_wizard_message(wizard, update.effective_message)
    msg = await update.effective_message.reply_text(
        prompt,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Back", callback_data=f"stock:back:{back_state}"),
            InlineKeyboardButton("❌ Cancel", callback_data="stock:cancel"),
        ]]),
    )
    track_wizard_message(wizard, msg)


async def stock_email_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_wizard_message(context.user_data.get("stock_wizard"), update.effective_message)
    value = update.effective_message.text.strip()
    if not value or len(value) > 254:
        await update.effective_message.reply_text("Enter a valid account email or username.")
        return STOCK_EMAIL
    context.user_data["stock_wizard"]["email"] = value
    await stock_prompt(update, context, "Enter the account password:", STOCK_EMAIL)
    return STOCK_PASSWORD


async def stock_password_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_wizard_message(context.user_data.get("stock_wizard"), update.effective_message)
    value = update.effective_message.text.strip()
    if not value or len(value) > 2000:
        await update.effective_message.reply_text("Password cannot be empty.")
        return STOCK_PASSWORD
    wizard = context.user_data["stock_wizard"]
    wizard["password"] = value
    if wizard.get("product_mode") == "invite":
        await stock_prompt(
            update, context,
            "How many invitations are available from this stock account? Enter a whole number from 1 to 1000:",
            STOCK_PASSWORD,
        )
        return STOCK_LIMIT
    await stock_prompt(update, context, "Enter the first profile name, or send - if this account has no profile:", STOCK_PASSWORD)
    return STOCK_PROFILE


async def stock_profile_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_wizard_message(context.user_data.get("stock_wizard"), update.effective_message)
    value = update.effective_message.text.strip()
    if not value or len(value) > 200:
        await update.effective_message.reply_text("Enter a profile name or -.")
        return STOCK_PROFILE
    context.user_data["stock_wizard"]["profile"] = value
    await stock_prompt(update, context, "Enter the profile PIN, or send - if there is no PIN:", STOCK_PROFILE)
    return STOCK_PIN


async def stock_pin_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_wizard_message(context.user_data.get("stock_wizard"), update.effective_message)
    value = update.effective_message.text.strip()
    if not value or len(value) > 100:
        await update.effective_message.reply_text("Enter a PIN or -.")
        return STOCK_PIN
    context.user_data["stock_wizard"]["pin"] = value
    await stock_prompt(
        update, context,
        "Enter the shared-account buyer limit (a whole number of 1 or more). Solo-account limit stays at 1:",
        STOCK_PIN,
    )
    return STOCK_LIMIT


async def stock_limit_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_wizard_message(context.user_data.get("stock_wizard"), update.effective_message)
    try:
        limit = int(update.effective_message.text.strip())
        if limit < 1 or limit > 1000:
            raise ValueError
    except (TypeError, ValueError):
        await update.effective_message.reply_text("Enter a whole number from 1 to 1000.")
        return STOCK_LIMIT
    wizard = context.user_data["stock_wizard"]
    if wizard.get("product_mode") == "invite":
        wizard["invite_limit"] = limit
        back_state = STOCK_PASSWORD
    else:
        wizard["shared_limit"] = limit
        back_state = STOCK_PIN
    await stock_prompt(
        update, context,
        "Enter the expiry date as YYYY-MM-DD, or send - if it does not expire:",
        back_state,
    )
    return STOCK_EXPIRY


async def stock_expiry_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_wizard_message(context.user_data.get("stock_wizard"), update.effective_message)
    raw = update.effective_message.text.strip()
    try:
        expiry = None if raw == "-" else date.fromisoformat(raw)
        if expiry and expiry < now().date():
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Enter a valid date in YYYY-MM-DD format that is today or later, or send -.")
        return STOCK_EXPIRY
    wizard = context.user_data["stock_wizard"]
    wizard["expires_on"] = expiry
    if wizard.get("product_mode") == "invite":
        summary = (
            "📨 CONFIRM INVITE STOCK\n\n"
            f"Product: {wizard['product_name']}\n"
            f"Account email: {wizard['email']}\n"
            "Password: Provided (stored securely)\n"
            f"Available invites: {wizard['invite_limit']}\n"
            f"Expiry: {expiry.isoformat() if expiry else 'No expiry'}\n\n"
            "Save this invite stock?"
        )
    else:
        profile = wizard["profile"]
        summary = (
            "📦 CONFIRM NEW STOCK\n\n"
            f"Product: {wizard['product_name']}\n"
            f"Email: {wizard['email']}\n"
            f"First profile: {'None' if profile == '-' else profile}\n"
            f"PIN: {'None' if wizard['pin'] == '-' else 'Provided'}\n"
            "Solo buyer limit: 1\n"
            f"Shared buyer limit: {wizard['shared_limit']}\n"
            f"Expiry: {expiry.isoformat() if expiry else 'No expiry'}\n\n"
            "Save this account to inventory?"
        )
    msg = await update.effective_message.reply_text(
        summary,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Back", callback_data=f"stock:back:{STOCK_EXPIRY}"),
            InlineKeyboardButton("❌ Cancel", callback_data="stock:cancel"),
        ], [
            InlineKeyboardButton("✅ Save Stock", callback_data="stock:save"),
        ]]),
    )
    track_wizard_message(wizard, msg)
    return STOCK_CONFIRM


async def stock_confirm_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return ConversationHandler.END
    wizard = context.user_data.get("stock_wizard")
    if not wizard:
        await q.edit_message_text("Stock setup expired. Start again from Add Stock.")
        return ConversationHandler.END
    try:
        with Session() as db:
            product = db.get(Product, wizard["product_id"])
            if not product or not product.active:
                await q.edit_message_text("The selected product is no longer active. Nothing was saved.")
                context.user_data.pop("stock_wizard", None)
                return ConversationHandler.END
            account = Account(
                product_id=product.id,
                email_enc=enc(wizard["email"]),
                password_enc=enc(wizard["password"]),
                solo_buyer_limit=1,
                shared_buyer_limit=(
                    wizard["invite_limit"] if wizard.get("product_mode") == "invite"
                    else wizard["shared_limit"]
                ),
                expires_on=wizard["expires_on"],
            )
            db.add(account)
            db.flush()
            if wizard.get("product_mode") != "invite" and wizard["profile"] != "-":
                db.add(ProfileSlot(
                    account_id=account.id,
                    profile_enc=enc(wizard["profile"]),
                    pin_enc=enc("" if wizard["pin"] == "-" else wizard["pin"]),
                    solo_limit=1,
                    shared_limit=wizard["shared_limit"],
                ))
            log_action(db, OWNER_ID, "stock_added", f"product_id={product.id}; account_id={account.id}")
            db.commit()
            account_id = account.id
    except Exception:
        log.exception("Guided stock save failed")
        await q.edit_message_text(
            "Could not save this stock item because of a database error. Nothing was confirmed as saved. "
            "Please try again or contact the owner if the problem continues.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Retry Save", callback_data="stock:save"),
                InlineKeyboardButton("❌ Cancel", callback_data="stock:cancel")
            ]]),
        )
        return STOCK_CONFIRM
    await cleanup_wizard_messages(context, wizard)
    context.user_data.pop("stock_wizard", None)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ Stock saved successfully. Account ID: {account_id}.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Add Another", callback_data="owner:stock"),
            InlineKeyboardButton("🏠 Owner Menu", callback_data="owner:home"),
        ]]),
    )
    return ConversationHandler.END


async def stock_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wizard = context.user_data.get("stock_wizard", {})
    track_wizard_message(wizard, update.effective_message)
    await cleanup_wizard_messages(context, wizard)
    context.user_data.pop("stock_wizard", None)
    await context.bot.send_message(chat_id=update.effective_chat.id, text="Stock setup cancelled. Nothing was saved.")
    return ConversationHandler.END


async def stock_navigation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return ConversationHandler.END
    if q.data == "stock:cancel":
        wizard = context.user_data.get("stock_wizard", {})
        await cleanup_wizard_messages(context, wizard)
        context.user_data.pop("stock_wizard", None)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Stock setup cancelled. Nothing was saved.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Owner Menu", callback_data="owner:home")
            ]]),
        )
        return ConversationHandler.END
    target = int(q.data.rsplit(":", 1)[1])
    wizard = context.user_data.get("stock_wizard")
    if not wizard:
        await q.edit_message_text("Stock setup expired. Start again.")
        return ConversationHandler.END
    if target == STOCK_PRODUCT:
        with Session() as db:
            rows = db.scalars(
                select(Product).where(Product.active.is_(True)).order_by(Product.name)
            ).all()
            buttons = [
                [InlineKeyboardButton(f"{p.name} (#{p.id})", callback_data=f"stockprod:{p.id}")]
                for p in rows
            ]
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="stock:cancel")])
        await q.edit_message_text(
            "📦 ADD STOCK\n\nChoose the product for this account:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return STOCK_PRODUCT
    invite_mode = wizard.get("product_mode") == "invite"
    prompts = {
        STOCK_EMAIL: ("Enter the account email or username:", STOCK_PRODUCT),
        STOCK_PASSWORD: ("Enter the account password:", STOCK_EMAIL),
        STOCK_PROFILE: ("Enter the first profile name, or send - if this account has no profile:", STOCK_PASSWORD),
        STOCK_PIN: ("Enter the profile PIN, or send - if there is no PIN:", STOCK_PROFILE),
        STOCK_LIMIT: (
            "Enter the number of invitations available (1 to 1000):" if invite_mode
            else "Enter the shared-account buyer limit (a whole number of 1 or more). Solo-account limit stays at 1:",
            STOCK_PASSWORD if invite_mode else STOCK_PIN,
        ),
        STOCK_EXPIRY: ("Enter the expiry date as YYYY-MM-DD, or send - if it does not expire:", STOCK_LIMIT),
    }
    if target not in prompts:
        await q.edit_message_text("That navigation step is unavailable. Continue or cancel the setup.")
        return ConversationHandler.END
    prompt, _ = prompts[target]
    await q.edit_message_text(
        prompt,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Back", callback_data=f"stock:back:{prompts[target][1]}"),
            InlineKeyboardButton("❌ Cancel", callback_data="stock:cancel"),
        ]]),
    )
    return target


async def addstock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_command(update, context): return
    parts = [p.strip() for p in " ".join(context.args).split("|")]
    if len(parts) != 8:
        await update.message.reply_text(
            "Usage:\n/stock PRODUCT_ID|EMAIL|PASSWORD|PROFILE_OR_-|PIN_OR_-|1|SHARED_ACCOUNT_LIMIT|YYYY-MM-DD_OR_-\n"
            "If a profile is entered, it is also created as the first profile slot. Use /addprofile for more profiles."
        )
        return
    try:
        pid = int(parts[0])
        solo_limit, shared_limit = int(parts[5]), int(parts[6])
        expiry = None if parts[7] == "-" else date.fromisoformat(parts[7])
        if solo_limit != 1 or shared_limit < 1 or (expiry and expiry < now().date()): raise ValueError
        if not parts[1] or not parts[2]: raise ValueError
    except Exception:
        await update.message.reply_text("Invalid product ID, credentials, buyer limits, or date.")
        return
    with Session() as db:
        product = db.get(Product, pid)
        if not product:
            await update.message.reply_text("Product not found.")
            return
        account = Account(
            product_id=pid, email_enc=enc(parts[1]), password_enc=enc(parts[2]),
            solo_buyer_limit=solo_limit, shared_buyer_limit=shared_limit, expires_on=expiry,
        )
        db.add(account)
        db.flush()
        if parts[3] != "-" and parts[3]:
            db.add(ProfileSlot(
                account_id=account.id, profile_enc=enc(parts[3]),
                pin_enc=enc("" if parts[4] == "-" else parts[4]),
                solo_limit=1, shared_limit=shared_limit,
            ))
        log_action(db, OWNER_ID, "stock_added", f"product_id={pid}; account_id={account.id}")
        db.commit()
        aid = account.id
    await update.message.reply_text(f"Account stock added. Account ID: {aid}.")


# ----- Guided add-profile wizard launched from the owner dashboard -----
PROFILE_ACCOUNT, PROFILE_NAME, PROFILE_PIN, PROFILE_LIMIT, PROFILE_CONFIRM = range(30, 35)

async def profile_wizard_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        await q.answer()
        if not require_owner(update):
            await q.edit_message_text("Owner permission required.")
            return ConversationHandler.END
        target = q
    else:
        if not await owner_command(update, context):
            return ConversationHandler.END
        target = update.effective_message
    with Session() as db:
        rows = db.scalars(select(Account).where(Account.status == "available").order_by(Account.id)).all()
        buttons = [[InlineKeyboardButton(f"Account #{a.id} — {a.email} (Product #{a.product_id})", callback_data=f"profile:account:{a.id}")] for a in rows]
    if not buttons:
        if q:
            await target.edit_message_text("No available accounts found. Add stock first.")
        else:
            await target.reply_text("No available accounts found. Add stock first.")
        return ConversationHandler.END
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="profile:cancel")])
    wizard = {"chat_id": update.effective_chat.id, "message_ids": []}
    track_wizard_message(wizard, update.effective_message)
    if q:
        track_wizard_message(wizard, q.message)
        msg = await target.edit_message_text("🗂 ADD PROFILE\nChoose the account:", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        msg = await target.reply_text("🗂 ADD PROFILE\nChoose the account:", reply_markup=InlineKeyboardMarkup(buttons))
    track_wizard_message(wizard, msg)
    context.user_data["profile_wizard"] = wizard
    return PROFILE_ACCOUNT

async def profile_account_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return ConversationHandler.END
    aid = int(q.data.rsplit(":", 1)[1])
    track_wizard_message(context.user_data.get("profile_wizard"), q.message)
    with Session() as db:
        account = db.get(Account, aid)
        if not account or account.status != "available":
            await q.edit_message_text("That account is no longer available. Start again.")
            return ConversationHandler.END
        context.user_data["profile_account_id"] = aid
        label = f"Account #{account.id} — {account.email}"
    await q.edit_message_text(f"Selected {label}.\nEnter the profile name:")
    return PROFILE_NAME

async def profile_name_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_wizard_message(context.user_data.get("profile_wizard"), update.effective_message)
    value = (update.message.text or "").strip()
    if not value or len(value) > 100:
        await update.message.reply_text("Enter a profile name between 1 and 100 characters.")
        return PROFILE_NAME
    context.user_data["profile_name"] = value
    msg = await update.message.reply_text("Enter the profile PIN, or send - if there is no PIN:")
    track_wizard_message(context.user_data.get("profile_wizard"), msg)
    return PROFILE_PIN

async def profile_pin_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_wizard_message(context.user_data.get("profile_wizard"), update.effective_message)
    value = (update.message.text or "").strip()
    if not value or len(value) > 32:
        await update.message.reply_text("Enter a PIN up to 32 characters, or send - if there is no PIN.")
        return PROFILE_PIN
    context.user_data["profile_pin"] = value
    msg = await update.message.reply_text("How many shared buyers can use this profile? Enter a number from 1 to 1000:")
    track_wizard_message(context.user_data.get("profile_wizard"), msg)
    return PROFILE_LIMIT

async def profile_limit_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_wizard_message(context.user_data.get("profile_wizard"), update.effective_message)
    raw = (update.message.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 1000:
        await update.message.reply_text("Enter a whole number from 1 to 1000.")
        return PROFILE_LIMIT
    context.user_data["profile_shared_limit"] = int(raw)
    aid = context.user_data["profile_account_id"]
    pin_label = "Not set" if context.user_data["profile_pin"] == "-" else "Set"
    msg = await update.message.reply_text(
        f"CONFIRM NEW PROFILE\nAccount ID: {aid}\nProfile: {context.user_data['profile_name']}\n"
        f"PIN: {pin_label}\nSolo limit: 1\nShared buyer limit: {int(raw)}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Save Profile", callback_data="profile:save"),
            InlineKeyboardButton("❌ Cancel", callback_data="profile:cancel"),
        ]])
    )
    track_wizard_message(context.user_data.get("profile_wizard"), msg)
    return PROFILE_CONFIRM

async def profile_confirm_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return ConversationHandler.END
    keys = ("profile_account_id", "profile_name", "profile_pin", "profile_shared_limit")
    wizard = context.user_data.get("profile_wizard", {})
    track_wizard_message(wizard, q.message)
    if q.data == "profile:cancel":
        await cleanup_wizard_messages(context, wizard)
        for key in keys:
            context.user_data.pop(key, None)
        context.user_data.pop("profile_wizard", None)
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Profile creation cancelled.")
        return ConversationHandler.END
    aid = int(context.user_data["profile_account_id"])
    with Session() as db:
        account = db.get(Account, aid)
        if not account or account.status != "available":
            await q.edit_message_text("Account is no longer available. No profile was saved.")
            return ConversationHandler.END
        slot = ProfileSlot(
            account_id=aid,
            profile_enc=enc(context.user_data["profile_name"]),
            pin_enc=enc("" if context.user_data["profile_pin"] == "-" else context.user_data["profile_pin"]),
            solo_limit=1,
            shared_limit=int(context.user_data["profile_shared_limit"]),
        )
        db.add(slot)
        db.flush()
        log_action(db, OWNER_ID, "profile_added", f"account_id={aid}; profile_slot_id={slot.id}")
        db.commit()
        sid = slot.id
    await cleanup_wizard_messages(context, wizard)
    for key in keys:
        context.user_data.pop(key, None)
    context.user_data.pop("profile_wizard", None)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ Profile saved successfully.\nProfile ID: {sid}\nAccount ID: {aid}",
    )
    return ConversationHandler.END

async def profile_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wizard = context.user_data.get("profile_wizard", {})
    track_wizard_message(wizard, update.effective_message)
    await cleanup_wizard_messages(context, wizard)
    context.user_data.pop("profile_wizard", None)
    for key in ("profile_account_id", "profile_name", "profile_pin", "profile_shared_limit"):
        context.user_data.pop(key, None)
    await context.bot.send_message(chat_id=update.effective_chat.id, text="Profile creation cancelled.")
    return ConversationHandler.END

async def product_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return
    _, action, pid_text = q.data.split(":")
    pid = int(pid_text)
    with Session() as db:
        product = db.get(Product, pid, with_for_update=True)
        if not product or not product.active:
            await q.edit_message_text("Product not found or already deleted.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Products", callback_data="owner:products")]]))
            return
        if action == "ask":
            accounts = db.scalar(select(func.count(Account.id)).where(Account.product_id == pid)) or 0
            sales = db.scalar(select(func.count(Sale.id)).where(Sale.product_id == pid)) or 0
            templates = db.scalar(select(func.count(DeliveryTemplate.id)).where(DeliveryTemplate.product_id == pid)) or 0
            name = product.name
            detail = f"Stock accounts: {accounts}\nSales/history records: {sales}\nDelivery templates: {templates}"
        elif action == "confirm":
            name = product.name
            product.active = False
            log_action(db, OWNER_ID, "product_archived", f"product_id={pid}; name={name}")
            db.commit()
            await q.edit_message_text(
                f"Product {name} (#{pid}) has been deleted from active products.\n\n"
                "Its stock, sales history, buyer records and templates were preserved for reporting and audit purposes.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Products", callback_data="owner:products")]])
            )
            return
        else:
            await q.edit_message_text("Invalid action.")
            return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚠️ Confirm Delete Product", callback_data=f"productdelete:confirm:{pid}")],
        [InlineKeyboardButton("Cancel", callback_data="owner:products")],
    ])
    await q.edit_message_text(
        f"⚠️ DELETE PRODUCT?\n\n{name} (#{pid})\n{detail}\n\n"
        "This deactivates the product so it can no longer be selected for new sales or stock additions. "
        "Existing inventory and historical records will NOT be erased.",
        reply_markup=keyboard,
    )


async def payroll_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        await q.answer()
        if not require_owner(update):
            await q.edit_message_text("Owner permission required.")
            return
    with Session() as db:
        period = db.scalar(select(PayrollPeriod).order_by(PayrollPeriod.id.desc()).limit(1))
        if not period:
            message = "💰 PAYROLL\n\nNo payroll period has been closed yet. Payroll is generated every Friday at 11:59 PM Manila time."
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="owner:home")]])
        else:
            lines = db.scalars(select(PayrollLine).where(PayrollLine.period_id == period.id).order_by(PayrollLine.salary.desc())).all()
            line_by_admin = {line.admin_id: line for line in lines}
            admins = db.scalars(select(User).where(User.role == "admin", User.active.is_(True)).order_by(User.display_name)).all()
            parts = [f"💰 PAYROLL #{period.id}", f"Period: {period.period_start} to {period.period_end}", f"Overall payroll status: {period.status.replace('_', ' ').upper()}", "", "👥 ADMIN PAYROLL STATUS"]
            for user in admins:
                line = line_by_admin.get(user.telegram_id)
                if line:
                    parts.append(
                        f"{user.display_name} — {period.status.replace('_', ' ').upper()}\n"
                        f"Sales: ₱{line.total_sales:.2f} | Profit: ₱{line.total_profit:.2f}\n"
                        f"Commission: {line.commission_rate*100:.0f}% | Salary: ₱{line.salary:.2f} | Bonus: ₱{line.bonus:.2f}"
                    )
                else:
                    parts.append(f"{user.display_name} — NO PAYROLL ENTRY (no recorded sales for this period)")
            if not admins:
                parts.append("No active admins.")
            active_ids = {user.telegram_id for user in admins}
            for line in lines:
                if line.admin_id not in active_ids:
                    parts.append(f"Former/inactive admin ID {line.admin_id} — {period.status.replace('_', ' ').upper()} | Salary: ₱{line.salary:.2f}")
            rows = []
            if period.status == "pending_owner_approval":
                rows.append([InlineKeyboardButton("✅ Approve Payroll", callback_data=f"payroll:approve:{period.id}")])
            elif period.status == "approved":
                rows.append([InlineKeyboardButton("💸 Mark as Paid", callback_data=f"payroll:paid:{period.id}")])
            rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="owner:payroll"), InlineKeyboardButton("🏠 Main Menu", callback_data="owner:home")])
            keyboard = InlineKeyboardMarkup(rows)
            message = "\n".join(parts)
    if q:
        await q.edit_message_text(message, reply_markup=keyboard)
    else:
        await update.effective_message.reply_text(message, reply_markup=keyboard)

async def payroll_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return
    _, action, pid_text = q.data.split(":")
    pid = int(pid_text)
    with Session() as db:
        period = db.get(PayrollPeriod, pid)
        if not period:
            await q.edit_message_text("Payroll period not found.")
            return
        if action == "approve" and period.status == "pending_owner_approval":
            period.status = "approved"
            log_action(db, OWNER_ID, "payroll_approved", f"period_id={pid}")
            result = f"Payroll #{pid} approved. Confirm the actual payout before marking it paid."
        elif action == "paid" and period.status == "approved":
            period.status = "paid"
            log_action(db, OWNER_ID, "payroll_marked_paid", f"period_id={pid}")
            result = f"Payroll #{pid} marked as paid."
        else:
            result = f"No change made. Current payroll status: {period.status}."
        db.commit()
    await q.edit_message_text(result, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Payroll", callback_data="owner:payroll")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="owner:home")],
    ]))


async def addprofile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_command(update, context): return
    parts = [p.strip() for p in " ".join(context.args).split("|")]
    if len(parts) != 5:
        await update.message.reply_text("Usage: /addprofile ACCOUNT_ID|PROFILE_NAME|PIN_OR_-|1|SHARED_PROFILE_LIMIT (Solo Profile limit must be 1)")
        return
    try:
        aid, solo_limit, shared_limit = int(parts[0]), int(parts[3]), int(parts[4])
        if solo_limit != 1 or shared_limit < 1 or not parts[1]: raise ValueError
    except Exception:
        await update.message.reply_text("Invalid account, profile or capacity values.")
        return
    with Session() as db:
        account = db.get(Account, aid)
        if not account:
            await update.message.reply_text("Account not found.")
            return
        slot = ProfileSlot(
            account_id=aid, profile_enc=enc(parts[1]),
            pin_enc=enc("" if parts[2] == "-" else parts[2]),
            solo_limit=solo_limit, shared_limit=shared_limit,
        )
        db.add(slot)
        db.flush()
        log_action(db, OWNER_ID, "profile_added", f"account_id={aid}; profile_slot_id={slot.id}")
        db.commit()
        sid = slot.id
    await update.message.reply_text(f"Profile slot added. Profile ID: {sid}.")


# ----- Guided delivery-template wizard -----
TEMPLATE_PRODUCT, TEMPLATE_TYPE, TEMPLATE_TEXT = range(20, 23)

TEMPLATE_PLACEHOLDERS = {
    "product_name", "buyer", "buyer_email", "email", "password",
    "profile", "pin", "instructions", "transaction_code",
}

def validate_delivery_template(template: str) -> tuple[bool, str]:
    """Accept only documented placeholders and ordinary literal text."""
    try:
        for _literal, field_name, format_spec, conversion in Formatter().parse(template):
            if field_name is None:
                continue
            if field_name not in TEMPLATE_PLACEHOLDERS:
                return False, f"Unsupported placeholder: {{{field_name}}}"
            if format_spec or conversion:
                return False, "Use placeholders exactly as shown, without format modifiers."
    except ValueError:
        return False, "The braces are incomplete or malformed. Check every { and }."
    return True, ""

def template_type_keyboard(product: Product):
    kinds = allowed_types(product.mode)
    rows = [[InlineKeyboardButton(nice_type(kind), callback_data=f"tpltype:{kind}")] for kind in kinds]
    rows.append([
        InlineKeyboardButton("⬅️ Back to Products", callback_data="tpl:back_products"),
        InlineKeyboardButton("❌ Cancel", callback_data="tpl:cancel"),
    ])
    return InlineKeyboardMarkup(rows)

async def template_product_list_message(target, context: ContextTypes.DEFAULT_TYPE):
    with Session() as db:
        rows = db.scalars(select(Product).where(Product.active.is_(True)).order_by(Product.name)).all()
        buttons = [[InlineKeyboardButton(f"{p.name} (#{p.id})", callback_data=f"tplprod:{p.id}")] for p in rows]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="tpl:cancel")])
    await target.edit_message_text(
        "📝 DELIVERY MESSAGE SETUP\n\nChoose the product:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return TEMPLATE_PRODUCT

async def settemplate_wizard_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    if not await owner_command(update, context):
        return ConversationHandler.END
    with Session() as db:
        rows = db.scalars(select(Product).where(Product.active.is_(True)).order_by(Product.name)).all()
    if not rows:
        await update.effective_message.reply_text("Create a product first with /newproduct.")
        return ConversationHandler.END
    buttons = [[InlineKeyboardButton(f"{p.name} (#{p.id})", callback_data=f"tplprod:{p.id}")] for p in rows]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="tpl:cancel")])
    await update.effective_message.reply_text(
        "📝 DELIVERY MESSAGE SETUP\n\nChoose the product:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return TEMPLATE_PRODUCT

async def template_product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return ConversationHandler.END
    pid = int(q.data.split(":")[1])
    with Session() as db:
        product = db.get(Product, pid)
        if not product or not product.active:
            await q.edit_message_text("Product not found or inactive. Start again with /settemplate.")
            return ConversationHandler.END
        context.user_data["template_wizard"] = {"product_id": pid, "product_name": product.name}
        keyboard = template_type_keyboard(product)
        kinds = allowed_types(product.mode)
        product_name = product.name
    if not kinds:
        await q.edit_message_text(
            f"Product: {product_name}\n\nNo supported delivery types are configured for this product.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Products", callback_data="tpl:back_products")],
                [InlineKeyboardButton("❌ Cancel", callback_data="tpl:cancel")],
            ]),
        )
        return TEMPLATE_TYPE
    await q.edit_message_text(
        f"Product: {product_name}\n\nChoose an enabled delivery type:",
        reply_markup=keyboard,
    )
    return TEMPLATE_TYPE

async def template_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return ConversationHandler.END
    wizard = context.user_data.get("template_wizard")
    if not wizard:
        await q.edit_message_text("Setup expired. Start again with /settemplate.")
        return ConversationHandler.END
    kind = q.data.split(":", 1)[1]
    with Session() as db:
        product = db.get(Product, wizard["product_id"])
        if not product or not product.active:
            await q.edit_message_text("Product not found or inactive. Start again with /settemplate.")
            context.user_data.pop("template_wizard", None)
            return ConversationHandler.END
        if kind not in allowed_types(product.mode):
            await q.edit_message_text(
                "That delivery type is not enabled for this product. Choose one of the available types:",
                reply_markup=template_type_keyboard(product),
            )
            return TEMPLATE_TYPE
        product_name = product.name
    wizard["kind"] = kind
    await q.edit_message_text(
        f"Product: {product_name}\nType: {nice_type(kind)}\n\n"
        "Now send the delivery message as one message. Supported placeholders:\n"
        "{product_name}  {buyer}  {buyer_email}\n"
        "{email}  {password}  {profile}  {pin}\n"
        "{instructions}  {transaction_code}\n\n"
        "Example: Thanks {buyer}! Your {product_name} details: {email} / {password}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Delivery Types", callback_data="tpl:back_types")],
            [InlineKeyboardButton("❌ Cancel", callback_data="tpl:cancel")],
        ]),
    )
    return TEMPLATE_TEXT

async def template_back_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return ConversationHandler.END
    context.user_data.pop("template_wizard", None)
    return await template_product_list_message(q, context)

async def template_back_types(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return ConversationHandler.END
    wizard = context.user_data.get("template_wizard")
    if not wizard:
        return await template_product_list_message(q, context)
    with Session() as db:
        product = db.get(Product, wizard["product_id"])
        if not product or not product.active:
            context.user_data.pop("template_wizard", None)
            await q.edit_message_text("Product is no longer active. Start again with /settemplate.")
            return ConversationHandler.END
        product_name = product.name
        keyboard = template_type_keyboard(product)
    wizard.pop("kind", None)
    await q.edit_message_text(
        f"Product: {product_name}\n\nChoose an enabled delivery type:",
        reply_markup=keyboard,
    )
    return TEMPLATE_TYPE

async def template_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not require_owner(update):
        await q.edit_message_text("Owner permission required.")
        return ConversationHandler.END
    context.user_data.pop("template_wizard", None)
    await q.edit_message_text(
        "Delivery-template setup cancelled. No changes were saved.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Owner Menu", callback_data="owner:home")]]),
    )
    return ConversationHandler.END

async def template_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("template_wizard", None)
    await update.effective_message.reply_text("Delivery-template setup cancelled. No changes were saved.")
    return ConversationHandler.END

async def template_text_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    template = (update.effective_message.text or "").strip()
    if not template or len(template) > 4000:
        await update.effective_message.reply_text(
            "Please send a message between 1 and 4000 characters.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Delivery Types", callback_data="tpl:back_types")],
                [InlineKeyboardButton("❌ Cancel", callback_data="tpl:cancel")],
            ]),
        )
        return TEMPLATE_TEXT
    valid, reason = validate_delivery_template(template)
    if not valid:
        await update.effective_message.reply_text(
            f"❌ {reason}\nPlease correct the template and send it again.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Delivery Types", callback_data="tpl:back_types")],
                [InlineKeyboardButton("❌ Cancel", callback_data="tpl:cancel")],
            ]),
        )
        return TEMPLATE_TEXT
    wizard = context.user_data.get("template_wizard")
    if not wizard or not wizard.get("kind"):
        await update.effective_message.reply_text("Setup expired. Start again with /settemplate.")
        context.user_data.pop("template_wizard", None)
        return ConversationHandler.END

    try:
        with Session() as db:
            product = db.get(Product, wizard["product_id"])
            if not product or not product.active:
                await update.effective_message.reply_text("Product not found or inactive. Start again with /settemplate.")
                context.user_data.pop("template_wizard", None)
                return ConversationHandler.END
            if wizard["kind"] not in allowed_types(product.mode):
                context.user_data.pop("template_wizard", None)
                await update.effective_message.reply_text(
                    "This delivery type is no longer enabled for that product. Please open Delivery Templates again."
                )
                return ConversationHandler.END
            row = db.scalar(select(DeliveryTemplate).where(
                DeliveryTemplate.product_id == wizard["product_id"],
                DeliveryTemplate.delivery_type == wizard["kind"],
            ))
            if row:
                row.template_text = template
            else:
                db.add(DeliveryTemplate(
                    product_id=wizard["product_id"],
                    delivery_type=wizard["kind"],
                    template_text=template,
                ))
            log_action(db, OWNER_ID, "delivery_template_updated", f"product_id={wizard['product_id']}; type={wizard['kind']}")
            db.commit()
    except Exception:
        log.exception("Failed to save delivery template")
        await update.effective_message.reply_text(
            "❌ I couldn't save the delivery message because the database returned an error. "
            "Your setup is still open; please try sending it again. No success was recorded.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Delivery Types", callback_data="tpl:back_types")],
                [InlineKeyboardButton("❌ Cancel", callback_data="tpl:cancel")],
            ]),
        )
        return TEMPLATE_TEXT

    context.user_data.pop("template_wizard", None)
    await update.effective_message.reply_text(
        f"✅ Delivery message saved for {wizard['product_name']} — {nice_type(wizard['kind'])}."
    )
    return ConversationHandler.END

async def templates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_command(update, context): return
    await update.message.reply_text(
        "📝 DELIVERY MESSAGE SETUP\n"
        "Use /settemplate to open the guided menu. Choose a product, choose one of its enabled delivery types, then send your message.\n\n"
        "Available placeholders: " + ", ".join("{" + name + "}" for name in sorted(TEMPLATE_PLACEHOLDERS))
    )

# ----- Inventory display -----
async def products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_command(update, context): return
    with Session() as db:
        rows = db.scalars(select(Product).where(Product.active.is_(True)).order_by(Product.name)).all()
        if not rows:
            await update.message.reply_text("No active products yet.")
            return
        lines = ["PRODUCTS & SELLING PRICES"]
        for p in rows:
            if update.effective_user.id == OWNER_ID:
                lines.append(f"#{p.id} {p.name} [{p.mode}] | capital SA/SH/SpA/SpH/Invite: " + "/".join(str(v) for v in [p.solo_account_capital,p.shared_account_capital,p.solo_profile_capital,p.shared_profile_capital,p.invite_capital]))
            lines.append(f"  Solo account ₱{p.solo_account_price:.2f} | Shared account ₱{p.shared_account_price:.2f}")
            lines.append(f"  Solo profile ₱{p.solo_profile_price:.2f} | Shared profile ₱{p.shared_profile_price:.2f} | Invite ₱{p.invite_price:.2f}")
        await update.message.reply_text("\n".join(lines))


async def stockview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_command(update, context): return
    with Session() as db:
        rows = db.scalars(select(Product).where(Product.active.is_(True)).order_by(Product.name)).all()
        lines = ["AVAILABLE STOCK"]
        for p in rows:
            accounts = db.scalar(select(func.count(Account.id)).where(Account.product_id == p.id, Account.status == "available", or_(Account.expires_on.is_(None), Account.expires_on >= now().date()))) or 0
            profiles = db.scalar(select(func.count(ProfileSlot.id)).join(Account).where(Account.product_id == p.id, ProfileSlot.active.is_(True))) or 0
            if p.mode == "invite":
                invite_accounts = db.scalars(select(Account).where(
                    Account.product_id == p.id,
                    Account.status == "available",
                    or_(Account.expires_on.is_(None), Account.expires_on >= now().date()),
                )).all()
                invites = sum(
                    max(0, account.shared_buyer_limit - count_used(db, account.id, "invite", "account"))
                    for account in invite_accounts
                )
                lines.append(f"{p.name}: {invites} invite(s) available across {len(invite_accounts)} stock account(s)")
            else:
                lines.append(f"{p.name}: {accounts} account(s), {profiles} profile slot(s)")
        await update.message.reply_text("\n".join(lines))


async def stock_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != OWNER_ID:
        await q.edit_message_text("Only the owner can inspect detailed stock and reverse sales.")
        return
    parts = q.data.split(":")
    action = parts[1]
    if action == "product":
        pid = int(parts[2])
        with Session() as db:
            product = db.get(Product, pid)
            if not product:
                await q.edit_message_text("Product not found.")
                return
            today = now().date()
            available_accounts = db.scalar(select(func.count(Account.id)).where(
                Account.product_id == pid, Account.status == "available",
                or_(Account.expires_on.is_(None), Account.expires_on >= today)
            )) or 0
            reserved = db.scalar(select(func.count(Sale.id)).where(
                Sale.product_id == pid, Sale.status == "reserved",
                or_(Sale.reserved_until.is_(None), Sale.reserved_until > now())
            )) or 0
            sold_accounts = db.scalar(select(func.count(Account.id)).where(
                Account.product_id == pid, Account.status == "sold"
            )) or 0
            full_accounts = db.scalar(select(func.count(Account.id)).where(
                Account.product_id == pid, Account.status == "full"
            )) or 0
            disabled_accounts = db.scalar(select(func.count(Account.id)).where(
                Account.product_id == pid, Account.status == "disabled"
            )) or 0
            active_profiles = db.scalar(select(func.count(ProfileSlot.id)).join(Account).where(
                Account.product_id == pid, ProfileSlot.active.is_(True)
            )) or 0
            account_rows = db.scalars(select(Account).where(
                Account.product_id == pid
            ).order_by(Account.id.desc()).limit(10)).all()
            sold_rows = db.scalars(select(Sale).where(
                Sale.product_id == pid,
                Sale.status.in_(["completed", "partially_refunded", "refunded"])
            ).order_by(Sale.completed_at.desc(), Sale.id.desc()).limit(12)).all()
            sold_by_type = {}
            for row in db.scalars(select(Sale).where(
                Sale.product_id == pid,
                Sale.status.in_(["completed", "partially_refunded", "refunded"])
            )).all():
                sold_by_type[row.sale_type] = sold_by_type.get(row.sale_type, 0) + 1
            lines = [
                f"📦 {product.name} — STOCK DETAILS",
                f"Product ID: #{product.id} | Mode: {product.mode}",
                "",
                f"Available accounts: {available_accounts}",
                f"Reserved sales: {reserved}",
                f"Sold accounts: {sold_accounts}",
                f"Full accounts: {full_accounts}",
                f"Active profile slots: {active_profiles}",
                f"Disabled accounts: {disabled_accounts}",
                "",
                "INDIVIDUAL STOCK RECORDS (latest 10)",
            ]
            deletable_account_ids = []
            if account_rows:
                for account in account_rows:
                    slots = db.scalars(select(ProfileSlot).where(ProfileSlot.account_id == account.id).order_by(ProfileSlot.id)).all()
                    active_slot_count = sum(1 for slot in slots if slot.active)
                    email = dec(account.email_enc)
                    expiry = account.expires_on.isoformat() if account.expires_on else "No expiry"
                    invite_left = max(0, account.shared_buyer_limit - count_used(db, account.id, "invite", "account"))
                    lines.append(
                        f"Account #{account.id} | {email} | {account.status} | Expires: {expiry} | "
                        f"Profiles: {active_slot_count}/{len(slots)} active | Invite units left: {invite_left}"
                    )
                    for slot in slots[:3]:
                        lines.append(
                            f"  Profile #{slot.id}: {dec(slot.profile_enc)} — "
                            f"{'active' if slot.active else 'inactive'}"
                        )
                    account_sales = db.scalar(select(func.count(Sale.id)).where(
                        or_(Sale.account_id == account.id, Sale.profile_slot_id.in_(
                            select(ProfileSlot.id).where(ProfileSlot.account_id == account.id)
                        ))
                    )) or 0
                    if account_sales == 0:
                        deletable_account_ids.append(account.id)
            else:
                lines.append("No stock records added yet.")
            lines.extend(["", "COMPLETED SALES BY TYPE"])
            lines.extend([f"• {nice_type(k)}: {v}" for k, v in sorted(sold_by_type.items())] or ["No completed sales yet."])
            lines.append("")
            lines.append("RECENT SALES — ADMIN ATTRIBUTION")
            for sale in sold_rows:
                admin = db.get(User, sale.admin_id)
                lines.append(
                    f"{sale.transaction_code} | {sale.status} | {sale.buyer} | "
                    f"{nice_type(sale.sale_type)} | ₱{sale.selling_price:.2f} | "
                    f"Sold by: {admin.display_name if admin else sale.admin_id}"
                )
            buttons = [[InlineKeyboardButton("📜 Full Sales History", callback_data=f"stockdetail:sales:{pid}")]]
            for account_id in deletable_account_ids:
                buttons.append([InlineKeyboardButton(
                    f"🗑 Delete Account #{account_id}",
                    callback_data=f"stockdetail:delete:{account_id}"
                )])
            for sale in sold_rows:
                buttons.append([InlineKeyboardButton(
                    f"↩️ Reverse {sale.transaction_code}",
                    callback_data=f"stockdetail:reverse:{sale.id}"
                )])
            buttons.append([InlineKeyboardButton("⬅️ Products", callback_data="owner:stockview")])
        await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))
        return
    if action == "sales":
        pid = int(parts[2])
        with Session() as db:
            product = db.get(Product, pid)
            if not product:
                await q.edit_message_text("Product not found.")
                return
            rows = db.scalars(select(Sale).where(Sale.product_id == pid).order_by(Sale.created_at.desc()).limit(15)).all()
            lines = [f"📜 SALES HISTORY — {product.name}"]
            for sale in rows:
                admin = db.get(User, sale.admin_id)
                lines.append(
                    f"#{sale.id} {sale.transaction_code} | {sale.status} | {sale.buyer} | "
                    f"{nice_type(sale.sale_type)} | ₱{sale.selling_price:.2f} | "
                    f"Admin: {admin.display_name if admin else sale.admin_id} | "
                    f"{sale.completed_at.strftime('%Y-%m-%d %H:%M') if sale.completed_at else 'Not completed'}"
                )
            buttons = []
            for sale in rows:
                if sale.status in {"completed", "partially_refunded", "refunded"}:
                    buttons.append([InlineKeyboardButton(
                        f"↩️ Reverse {sale.transaction_code}",
                        callback_data=f"stockdetail:reverse:{sale.id}"
                    )])
            buttons.append([InlineKeyboardButton("⬅️ Product Details", callback_data=f"stockdetail:product:{pid}")])
        await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))
        return
    if action == "delete":
        account_id = int(parts[2])
        with Session() as db:
            account = db.get(Account, account_id)
            if not account:
                await q.edit_message_text("Stock account not found. Refresh Stock Overview.")
                return
            product = db.get(Product, account.product_id)
            slots = db.scalars(select(ProfileSlot).where(ProfileSlot.account_id == account_id)).all()
            slot_ids = [slot.id for slot in slots]
            linked_sales = db.scalar(select(func.count(Sale.id)).where(
                or_(Sale.account_id == account_id, Sale.profile_slot_id.in_(slot_ids) if slot_ids else False)
            )) or 0
            active_reservations = db.scalar(select(func.count(Sale.id)).where(
                Sale.account_id == account_id, Sale.status == "reserved",
                or_(Sale.reserved_until.is_(None), Sale.reserved_until > now())
            )) or 0
            if linked_sales or active_reservations:
                await q.edit_message_text(
                    f"Cannot delete Account #{account_id}. It is linked to {linked_sales} sale record(s) "
                    f"and {active_reservations} active reservation(s). Sales history is protected."
                )
                return
            email = dec(account.email_enc)
            product_id = account.product_id
            profile_count = len(slots)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠️ Yes, permanently delete", callback_data=f"stockdetail:deleteconfirm:{account_id}")],
            [InlineKeyboardButton("Cancel", callback_data=f"stockdetail:product:{product_id}")],
        ])
        await q.edit_message_text(
            f"⚠️ DELETE STOCK ACCOUNT?\n\nProduct: {product.name if product else 'Unknown'}\n"
            f"Account ID: #{account_id}\nEmail: {email}\nProfile slots: {profile_count}\n\n"
            "This permanently removes this stock account and its profile slots. It cannot be undone. "
            "Existing sale-linked stock is not eligible.",
            reply_markup=keyboard,
        )
        return
    if action == "deleteconfirm":
        account_id = int(parts[2])
        with Session() as db:
            account = db.get(Account, account_id, with_for_update=True)
            if not account:
                await q.edit_message_text("Stock account was already deleted or no longer exists.")
                return
            slots = db.scalars(select(ProfileSlot).where(ProfileSlot.account_id == account_id).with_for_update()).all()
            slot_ids = [slot.id for slot in slots]
            linked_sales = db.scalar(select(func.count(Sale.id)).where(
                or_(Sale.account_id == account_id, Sale.profile_slot_id.in_(slot_ids) if slot_ids else False)
            )) or 0
            active_reservations = db.scalar(select(func.count(Sale.id)).where(
                Sale.account_id == account_id, Sale.status == "reserved",
                or_(Sale.reserved_until.is_(None), Sale.reserved_until > now())
            )) or 0
            if linked_sales or active_reservations:
                await q.edit_message_text("Deletion stopped: this stock is now linked to a sale or active reservation. No records were deleted.")
                return
            product_id = account.product_id
            email = dec(account.email_enc)
            for slot in slots:
                db.delete(slot)
            db.delete(account)
            log_action(db, OWNER_ID, "stock_account_deleted",
                f"account_id={account_id}; product_id={product_id}; email={email}; deleted_profiles={len(slots)}")
            db.commit()
        await q.edit_message_text(
            f"Stock Account #{account_id} was deleted, including its {len(slot_ids)} profile slot(s).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Stock Details", callback_data=f"stockdetail:product:{product_id}")]])
        )
        return
    if action == "reverse":
        sid = int(parts[2])
        with Session() as db:
            sale = db.get(Sale, sid)
            if not sale or sale.status not in {"completed", "partially_refunded", "refunded"}:
                await q.edit_message_text("Only a completed sale can be reversed. This sale is already reversed or cannot be changed.")
                return
            admin = db.get(User, sale.admin_id)
            product_id = sale.product_id
            message = (
                f"⚠️ REVERSE SALE?\n{sale.transaction_code} — {sale.product.name}\n"
                f"Buyer: {sale.buyer}\nSold by: {admin.display_name if admin else sale.admin_id}\n"
                f"Amount: ₱{sale.selling_price:.2f}\n\n"
                "Choose whether the stock is safe to resell. This action will mark the sale reversed "
                "and exclude it from future sales/payroll totals."
            )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Reverse + restore stock", callback_data=f"stockdetail:confirm:{sid}:restore")],
            [InlineKeyboardButton("Reverse, do NOT restore stock", callback_data=f"stockdetail:confirm:{sid}:consume")],
            [InlineKeyboardButton("Cancel", callback_data=f"stockdetail:product:{product_id}")],
        ])
        await q.edit_message_text(message, reply_markup=keyboard)
        return
    if action == "confirm":
        sid = int(parts[2])
        restore = parts[3] == "restore"
        with Session() as db:
            sale = db.get(Sale, sid, with_for_update=True)
            if not sale or sale.status not in {"completed", "partially_refunded", "refunded"}:
                await q.edit_message_text("Sale was already reversed or is no longer eligible.")
                return
            original_status = sale.status
            sale.status = "cancelled"
            sale.reserved_until = None
            sale.inventory_released = restore
            if sale.account_id:
                account = db.get(Account, sale.account_id, with_for_update=True)
                if account:
                    account.status = "available" if restore else "disabled"
            if sale.profile_slot_id:
                slot = db.get(ProfileSlot, sale.profile_slot_id, with_for_update=True)
                if slot:
                    slot.active = restore
            subscription = db.scalar(select(BuyerSubscription).where(BuyerSubscription.sale_id == sid))
            if subscription:
                db.delete(subscription)
            log_action(
                db, OWNER_ID, "completed_sale_reversed",
                f"sale_id={sid}; code={sale.transaction_code}; previous_status={original_status}; "
                f"inventory_restored={restore}; original_admin_id={sale.admin_id}"
            )
            code = sale.transaction_code
            db.commit()
        await q.edit_message_text(
            f"Sale {code} reversed. Inventory restored: {restore}. "
            "The original sale record and admin attribution remain in sales history."
        )
        return


async def admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_command(update, context): return
    with Session() as db:
        users = db.scalars(select(User).where(User.role.in_(["admin", "pending"])).order_by(User.role, User.display_name)).all()
        lines = ["PENDING USERS & ADMINS"]
        for u in users:
            lines.append(f"{u.role.upper()} — {u.display_name} (@{u.username or 'none'}) — ID {u.telegram_id}")
        await update.message.reply_text("\n".join(lines) if len(lines) > 1 else "No pending users or admins.")


# ----- Sale wizard and reservation-safe inventory assignment -----
SALE_PRODUCT, SALE_TYPE, SALE_BUYER = range(3)

async def sale_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_command(update, context):
        return ConversationHandler.END
    with Session() as db:
        rows = db.scalars(select(Product).where(Product.active.is_(True)).order_by(Product.name)).all()
        if not rows:
            await update.effective_message.reply_text("No active products available.")
            return ConversationHandler.END
        buttons = [[InlineKeyboardButton(p.name, callback_data=f"sale:p:{p.id}")] for p in rows]
    wizard = {"chat_id": update.effective_chat.id, "message_ids": []}
    track_wizard_message(wizard, update.effective_message)
    msg = await update.effective_message.reply_text("Choose the product:", reply_markup=InlineKeyboardMarkup(buttons))
    track_wizard_message(wizard, msg)
    context.user_data["sale_wizard"] = wizard
    return SALE_PRODUCT


async def sale_product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    track_wizard_message(context.user_data.get("sale_wizard"), q.message)
    pid = int(q.data.split(":")[2])
    with Session() as db:
        if not role_of(db, q.from_user.id):
            await q.edit_message_text("You are not authorized.")
            return ConversationHandler.END
        product = db.get(Product, pid)
        if not product or not product.active:
            await q.edit_message_text("Product is unavailable.")
            return ConversationHandler.END
        kinds = allowed_types(product.mode)
        context.user_data["sale_product_id"] = pid
        if len(kinds) == 1:
            context.user_data["sale_type"] = kinds[0]
            prompt = "Enter the buyer's email for the invitation:"
            await q.edit_message_text(prompt)
            return SALE_BUYER
        buttons = [[InlineKeyboardButton(nice_type(k), callback_data=f"sale:t:{k}")] for k in kinds]
    await q.edit_message_text("Choose the selling type:", reply_markup=InlineKeyboardMarkup(buttons))
    return SALE_TYPE


async def sale_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    track_wizard_message(context.user_data.get("sale_wizard"), q.message)
    sale_type = q.data.split(":", 2)[2]
    context.user_data["sale_type"] = sale_type
    if sale_type == "invite":
        await q.edit_message_text("Enter the buyer's email for the invitation:")
    else:
        await q.edit_message_text("Enter the buyer's username or name (example: @juan123 or Juan Dela Cruz):")
    return SALE_BUYER


async def sale_buyer_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_wizard_message(context.user_data.get("sale_wizard"), update.effective_message)
    buyer_input = (update.message.text or "").strip()
    sale_type = context.user_data.get("sale_type")
    if not buyer_input or len(buyer_input) > 254:
        await update.message.reply_text("Please enter a valid buyer identifier.")
        return SALE_BUYER
    if sale_type == "invite" and ("@" not in buyer_input or "." not in buyer_input.split("@")[-1]):
        await update.message.reply_text("Please enter a valid email address.")
        return SALE_BUYER
    context.user_data["sale_buyer"] = buyer_input
    return await sale_create_reservation(update, context)

async def sale_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wizard = context.user_data.get("sale_wizard", {})
    track_wizard_message(wizard, update.effective_message)
    await cleanup_wizard_messages(context, wizard)
    context.user_data.pop("sale_wizard", None)
    for key in ("sale_product_id", "sale_type", "sale_buyer", "sale_duration_days"):
        context.user_data.pop(key, None)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Sale setup cancelled. No sale was reserved.",
    )
    return ConversationHandler.END


async def sale_create_reservation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buyer = context.user_data.get("sale_buyer", "")
    sale_type = context.user_data.get("sale_type")
    buyer_email = buyer if sale_type == "invite" else ""
    pid = int(context.user_data["sale_product_id"])
    uid = update.effective_user.id
    try:
        with Session() as db:
            product = db.get(Product, pid)
            if not product or not product.active:
                await update.effective_message.reply_text("Product is no longer available. Start /sale again.")
                return ConversationHandler.END
            if sale_type not in allowed_types(product.mode):
                await update.effective_message.reply_text("That selling type is not enabled for this product. Start /sale again.")
                return ConversationHandler.END
            price, capital = sale_fields(product, sale_type)
            account = None
            slot = None
            if sale_type in {"solo_account", "shared_account"}:
                candidates = db.scalars(select(Account).where(Account.product_id == pid, Account.status == "available").order_by(Account.id).with_for_update(skip_locked=True)).all()
                for candidate in candidates:
                    used = count_used(db, candidate.id, sale_type, "account")
                    limit = 1 if sale_type == "solo_account" else candidate.shared_buyer_limit
                    if used < limit:
                        account = candidate
                        break
                if not account:
                    await update.effective_message.reply_text("No account capacity is available for this selling type.")
                    return ConversationHandler.END
            elif sale_type == "invite":
                candidates = db.scalars(
                    select(Account).where(
                        Account.product_id == pid,
                        Account.status == "available",
                        or_(Account.expires_on.is_(None), Account.expires_on >= now().date()),
                    ).order_by(Account.id).with_for_update(skip_locked=True)
                ).all()
                for candidate in candidates:
                    used = count_used(db, candidate.id, "invite", "account")
                    if used < candidate.shared_buyer_limit:
                        account = candidate
                        break
                if not account:
                    await update.effective_message.reply_text("No invitation capacity is available. Add invite stock or check its expiry.")
                    return ConversationHandler.END
            elif sale_type in {"solo_profile", "shared_profile"}:
                candidates = db.scalars(
                    select(ProfileSlot).join(Account).where(
                        Account.product_id == pid, Account.status == "available", or_(Account.expires_on.is_(None), Account.expires_on >= now().date()), ProfileSlot.active.is_(True)
                    ).order_by(ProfileSlot.id).with_for_update(skip_locked=True)
                ).all()
                for candidate in candidates:
                    used = count_used(db, candidate.id, sale_type, "profile")
                    limit = candidate.solo_limit if sale_type == "solo_profile" else candidate.shared_limit
                    if used < limit:
                        slot = candidate
                        account = candidate.account
                        break
                if not slot:
                    await update.effective_message.reply_text("No profile capacity is available. Add profile stock or increase its capacity.")
                    return ConversationHandler.END
            sale = Sale(
                transaction_code=make_code(), product_id=pid,
                account_id=account.id if account else None,
                profile_slot_id=slot.id if slot else None,
                admin_id=uid, buyer=buyer[:200], buyer_email=buyer_email,
                sale_type=sale_type, selling_price=price, capital=capital,
                profit=price-capital, status="reserved",
                buyer_duration_days=int(product.subscription_duration_days or 30),
                reserved_until=now()+timedelta(minutes=RESERVATION_MINUTES),
            )
            db.add(sale)
            db.flush()
            log_action(db, uid, "sale_reserved", f"sale_id={sale.id}; code={sale.transaction_code}; type={sale_type}")
            db.commit()
            sale_id = sale.id
            code = sale.transaction_code
            product_name = product.name
            template_row = db.scalar(select(DeliveryTemplate).where(DeliveryTemplate.product_id == pid, DeliveryTemplate.delivery_type == sale_type))
            template = template_row.template_text if template_row else ""
            credentials = {"email": "", "password": "", "profile": "", "pin": ""}
            if account:
                credentials["email"] = dec(account.email_enc)
                credentials["password"] = dec(account.password_enc)
            if slot:
                credentials["profile"] = dec(slot.profile_enc)
                credentials["pin"] = dec(slot.pin_enc)
            log_action(db, uid, "assigned_credentials_viewed", f"sale_id={sale_id}; code={code}")
            db.commit()
        values = {
            "product_name": product_name, "buyer": buyer, "buyer_email": buyer_email,
            "email": credentials["email"], "password": credentials["password"],
            "profile": credentials["profile"], "pin": credentials["pin"],
            "instructions": "Send this privately to the buyer using the approved service workflow.",
            "transaction_code": code,
        }
        if not template:
            if sale_type == "invite":
                template = (
                    "INVITATION SOURCE ACCOUNT\n"
                    "Email: {email}\nPassword: {password}\n\n"
                    "Prepare the invitation for: {buyer_email}\n"
                    "Send it manually through the official service, then confirm below.\n"
                    "Ref: {transaction_code}"
                )
            else:
                template = "Product: {product_name}\nBuyer: {buyer}\nEmail: {email}\nPassword: {password}\nProfile: {profile}\nPIN: {pin}\nRef: {transaction_code}\n{instructions}"
        try:
            delivery = template.format_map({k: str(v) for k, v in values.items()})
        except (KeyError, ValueError, AttributeError, IndexError):
            delivery = "Template error: ask the owner to fix this template. Your reservation is still active."
        button_label = "Confirm invitation sent" if sale_type == "invite" else "Confirm delivered"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(button_label, callback_data=f"sale_done:{sale_id}"),
            InlineKeyboardButton("Cancel reservation", callback_data=f"sale_cancel:{sale_id}"),
        ]])
        sale_wizard = context.user_data.get("sale_wizard", {})
        await cleanup_wizard_messages(context, sale_wizard)
        context.user_data.pop("sale_wizard", None)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"RESERVED — {code}\nProduct: {product_name}\nType: {nice_type(sale_type)}\nBuyer: {buyer}\n"
            f"Price: ₱{price:.2f}\nReservation expires in {RESERVATION_MINUTES} minutes.\n\n"
            f"Delivery details (send privately):\n\n{delivery}\n\n"
            + ("The bot does not send invitation emails. Send it manually through the official service, then confirm the delivery."),
            reply_markup=keyboard,
        )
    except Exception:
        log.exception("Sale reservation failed")
        await update.effective_message.reply_text("Could not reserve stock. Please retry or contact the owner.")
    context.user_data.pop("sale_wizard", None)
    for key in ("sale_product_id", "sale_type", "sale_buyer", "sale_duration_days"):
        context.user_data.pop(key, None)
    return ConversationHandler.END


async def sale_finish_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action, sale_id_text = q.data.split(":", 1)
    sale_id = int(sale_id_text)
    uid = q.from_user.id
    with Session() as db:
        if not role_of(db, uid):
            await q.edit_message_text("Your bot access is no longer active.")
            return
        sale = db.get(Sale, sale_id, with_for_update=True)
        if not sale:
            await q.edit_message_text("Sale reservation not found.")
            return
        if uid != OWNER_ID and uid != sale.admin_id:
            await q.edit_message_text("Only the assigned admin or owner can update this sale.")
            return
        if sale.status != "reserved":
            await q.edit_message_text(f"This sale is already {sale.status}.")
            return
        if sale.reserved_until and sale.reserved_until < now():
            sale.status = "cancelled"
            sale.reserved_until = None
            log_action(db, uid, "reservation_expired", f"sale_id={sale_id}")
            db.commit()
            await q.edit_message_text("Reservation expired. Stock has been released.")
            return
        if action == "sale_done":
            sale.status = "completed"
            sale.completed_at = now()
            sale.reserved_until = None
            duration_days = int(sale.buyer_duration_days or 0)
            if duration_days in {7, 14, 30}:
                start_day = now().date()
                db.add(BuyerSubscription(
                    sale_id=sale.id, buyer=sale.buyer, buyer_email=sale.buyer_email,
                    product_id=sale.product_id, duration_days=duration_days,
                    starts_on=start_day, expires_on=start_day + timedelta(days=duration_days),
                ))
            if sale.account_id:
                account = db.get(Account, sale.account_id)
                if account and sale.sale_type == "solo_account":
                    account.status = "sold"
                elif account and sale.sale_type in {"shared_account", "invite"}:
                    used = count_used(db, account.id, sale.sale_type, "account")
                    if used >= account.shared_buyer_limit:
                        account.status = "full"
            log_action(db, uid, "sale_completed", f"sale_id={sale_id}; code={sale.transaction_code}")
            db.commit()
            await q.edit_message_text(
                f"Sale completed: {sale.transaction_code}\nPrice: ₱{sale.selling_price:.2f}\n"
                f"Buyer term: {duration_days} days\nBuyer expiry: "
                f"{(now().date() + timedelta(days=duration_days)).isoformat() if duration_days in {7, 14, 30} else 'Not recorded'}\n"
            )
        else:
            if uid != OWNER_ID:
                pending = db.scalar(select(CancellationRequest).where(CancellationRequest.sale_id == sale_id, CancellationRequest.status == "pending"))
                if pending:
                    await q.edit_message_text("A cancellation request is already waiting for the owner.")
                    return
                request = CancellationRequest(sale_id=sale_id, requester_id=uid, status="pending")
                db.add(request); db.flush()
                log_action(db, uid, "sale_cancellation_requested", f"sale_id={sale_id}; request_id={request.id}")
                db.commit()
                rid = request.id
                code = sale.transaction_code
                await q.edit_message_text(f"Cancellation request for {code} sent to the owner. The reservation remains held until the owner decides or it expires.")
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("Approve + restore stock", callback_data=f"cancelreq:restore:{rid}"),
                    InlineKeyboardButton("Approve, do not restore", callback_data=f"cancelreq:consume:{rid}"),
                ], [InlineKeyboardButton("Decline", callback_data=f"cancelreq:decline:{rid}")]])
                try:
                    await context.bot.send_message(OWNER_ID, f"SALE CANCELLATION REQUEST #{rid}\nSale: {code}\nAdmin ID: {uid}\nChoose whether stock can be resold.", reply_markup=keyboard)
                except Exception:
                    log.exception("Could not notify owner about cancellation request")
                return
            sale.status = "cancelled"
            sale.reserved_until = None
            sale.inventory_released = True
            if sale.account_id:
                account = db.get(Account, sale.account_id)
                if account: account.status = "available"
            if sale.profile_slot_id:
                slot = db.get(ProfileSlot, sale.profile_slot_id)
                if slot: slot.active = True
            log_action(db, OWNER_ID, "sale_reservation_cancelled_by_owner", f"sale_id={sale_id}")
            db.commit()
            await q.edit_message_text(f"Reservation {sale.transaction_code} cancelled. Stock capacity released.")


# ----- Sales reports, refunds, payroll -----
async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_command(update, context): return
    uid = update.effective_user.id
    start, end = current_week_bounds()
    with Session() as db:
        if uid == OWNER_ID:
            start_dt, end_dt = period_datetimes(start, end)
            sales = db.scalars(select(Sale).where(
                Sale.status.in_(["completed", "partially_refunded", "refunded"]),
                Sale.completed_at >= start_dt, Sale.completed_at < end_dt,
            )).all()
        else:
            sales = week_sales_query(db, uid, start, end)
        total = net_sale_total(sales)
        capital = sum((s.capital for s in sales if s.status != "refunded" or not s.inventory_released), Decimal("0"))
        profit = total - capital
        rate = tier_rate(total, db)
        salary = max(Decimal("0"), profit * rate)
        today_start = datetime.combine(now().date(), time.min, tzinfo=TIMEZONE)
        today_end = today_start + timedelta(days=1)
        today_sales = [s for s in sales if s.completed_at and today_start <= s.completed_at < today_end]
        today_total = net_sale_total(today_sales)
        text = (
            f"WEEKLY SALES ({start:%b %d}–{end:%b %d})\n"
            f"Transactions: {len(sales)}\nNet sales: ₱{total:.2f}\nCapital: ₱{capital:.2f}\nProfit: ₱{profit:.2f}\n"
            f"Commission tier: {rate*100:.0f}%\nEstimated salary: ₱{salary:.2f}\n"
            f"Weekly quota progress: ₱{total:.2f} / ₱500.00\n\nToday net sales: ₱{today_total:.2f}"
        )
        if uid == OWNER_ID:
            text += "\n\nOwner view: total active-admin sales combined. Use /payroll for Friday payroll status."
        await update.message.reply_text(text)


async def mysales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_command(update, context): return
    uid = update.effective_user.id
    with Session() as db:
        q = select(Sale).order_by(Sale.created_at.desc()).limit(15)
        if uid != OWNER_ID:
            q = q.where(Sale.admin_id == uid)
        rows = db.scalars(q).all()
        if not rows:
            await update.message.reply_text("No transactions found.")
            return
        lines = ["RECENT TRANSACTIONS"]
        for s in rows:
            lines.append(f"{s.transaction_code} | {s.status} | {s.product.name} | {nice_type(s.sale_type)} | ₱{s.selling_price:.2f} | {s.buyer}")
        await update.message.reply_text("\n".join(lines))


async def ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_command(update, context): return
    start, end = current_week_bounds()
    with Session() as db:
        users = db.scalars(select(User).where(User.role == "admin", User.active.is_(True))).all()
        values = []
        for user in users:
            sales = week_sales_query(db, user.telegram_id, start, end)
            values.append((user.display_name or str(user.telegram_id), net_sale_total(sales), len(sales)))
        values.sort(key=lambda row: row[1], reverse=True)
        lines = [f"WEEKLY ADMIN RANKING ({start:%b %d}–{end:%b %d})"]
        for index, (name, total, count) in enumerate(values, 1):
            lines.append(f"{index}. {name}: ₱{total:.2f} ({count} transaction(s))")
        await update.message.reply_text("\n".join(lines) if len(lines) > 1 else "No active admins yet.")


async def cancel_sale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_command(update, context): return
    parts = [p.strip() for p in " ".join(context.args).split("|", 1)]
    if len(parts) != 2 or not parts[0].isdigit() or parts[1].lower() not in {"yes", "no"}:
        await update.message.reply_text("Usage: /cancel SALE_ID|yes_or_no_resellable")
        return
    sid, resellable = int(parts[0]), parts[1].lower() == "yes"
    with Session() as db:
        sale = db.get(Sale, sid, with_for_update=True)
        if not sale or sale.status != "reserved":
            await update.message.reply_text("Only a reserved sale can be cancelled here. For a completed sale, use the owner's Stock Overview to reverse it.")
            return
        sale.status = "cancelled"
        sale.reserved_until = None
        sale.inventory_released = resellable
        if sale.account_id:
            account = db.get(Account, sale.account_id)
            if account: account.status = "available" if resellable else "disabled"
        if sale.profile_slot_id:
            slot = db.get(ProfileSlot, sale.profile_slot_id)
            if slot: slot.active = resellable
        log_action(db, OWNER_ID, "sale_cancelled_by_owner", f"sale_id={sid}; resellable={resellable}")
        db.commit()
    await update.message.reply_text(f"Sale #{sid} cancelled. Inventory restored: {resellable}.")


async def cancellation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != OWNER_ID:
        await q.edit_message_text("Only the owner can approve sale cancellations.")
        return
    _, action, rid_text = q.data.split(":")
    rid = int(rid_text)
    notify_id = None
    notify_text = ""
    with Session() as db:
        request = db.get(CancellationRequest, rid, with_for_update=True)
        if not request or request.status != "pending":
            await q.edit_message_text("This cancellation request is no longer pending.")
            return
        sale = db.get(Sale, request.sale_id, with_for_update=True)
        if not sale or sale.status != "reserved":
            request.status = "declined"
            response = "Sale is no longer reserved; request closed."
        elif action == "decline":
            request.status = "declined"
            response = f"Cancellation request #{rid} declined. The reservation remains active until it expires."
        else:
            sale.status = "cancelled"
            sale.reserved_until = None
            restore = action == "restore"
            sale.inventory_released = restore
            if sale.account_id:
                account = db.get(Account, sale.account_id)
                if account: account.status = "available" if restore else "disabled"
            if sale.profile_slot_id:
                slot = db.get(ProfileSlot, sale.profile_slot_id)
                if slot: slot.active = restore
            request.status = "approved_restore" if restore else "approved_consume"
            response = f"Cancellation approved. Inventory restored: {restore}."
            log_action(db, OWNER_ID, "sale_cancellation_approved", f"request_id={rid}; sale_id={sale.id}; inventory_restored={restore}")
        notify_id = request.requester_id
        notify_text = response
        db.commit()
    await q.edit_message_text(response)
    try: await context.bot.send_message(notify_id, notify_text)
    except Exception: pass


async def payroll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_command(update, context): return
    with Session() as db:
        period = db.scalar(select(PayrollPeriod).order_by(PayrollPeriod.id.desc()).limit(1))
        if not period:
            await update.message.reply_text("No payroll period has been closed yet. Payroll closes every Friday at 11:59 PM Manila time.")
            return
        lines = db.scalars(select(PayrollLine).where(PayrollLine.period_id == period.id).order_by(PayrollLine.salary.desc())).all()
        text = [f"PAYROLL #{period.id} — {period.period_start} to {period.period_end}", f"Status: {period.status}"]
        for line in lines:
            user = db.get(User, line.admin_id)
            text.append(f"{user.display_name if user else line.admin_id}: sales ₱{line.total_sales:.2f}; capital ₱{line.total_capital:.2f}; profit ₱{line.total_profit:.2f}; rate {line.commission_rate*100:.0f}%; salary ₱{line.salary:.2f}; bonus ₱{line.bonus:.2f}")
        await update.message.reply_text("\n".join(text))


async def approve_payroll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_command(update, context): return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /approvepayroll PERIOD_ID")
        return
    pid = int(context.args[0])
    with Session() as db:
        p = db.get(PayrollPeriod, pid)
        if not p or p.status != "pending_owner_approval":
            await update.message.reply_text("Payroll period not found or not awaiting approval.")
            return
        p.status = "approved"
        log_action(db, OWNER_ID, "payroll_approved", f"period_id={pid}")
        db.commit()
    await update.message.reply_text(f"Payroll #{pid} approved. Use /markpaid {pid} after payout.")


async def mark_paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_command(update, context): return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /markpaid PERIOD_ID")
        return
    pid = int(context.args[0])
    with Session() as db:
        p = db.get(PayrollPeriod, pid)
        if not p or p.status != "approved":
            await update.message.reply_text("Payroll must be approved before it can be marked paid.")
            return
        p.status = "paid"
        log_action(db, OWNER_ID, "payroll_marked_paid", f"period_id={pid}")
        db.commit()
    await update.message.reply_text(f"Payroll #{pid} marked paid.")


async def add_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_command(update, context): return
    parts = [p.strip() for p in " ".join(context.args).split("|", 3)]
    if len(parts) != 4:
        await update.message.reply_text("Usage: /bonus PERIOD_ID|ADMIN_ID|AMOUNT|NOTE")
        return
    try:
        pid, aid, amount = int(parts[0]), int(parts[1]), money(parts[2])
        if amount < 0: raise ValueError
    except Exception:
        await update.message.reply_text("Invalid period, admin ID or bonus amount.")
        return
    with Session() as db:
        line = db.scalar(select(PayrollLine).where(PayrollLine.period_id == pid, PayrollLine.admin_id == aid))
        if not line:
            await update.message.reply_text("Payroll line not found for that admin and period.")
            return
        line.bonus = amount
        line.bonus_note = parts[3]
        log_action(db, OWNER_ID, "payroll_bonus_added", f"period_id={pid}; admin_id={aid}; amount={amount}")
        db.commit()
    await update.message.reply_text("Bonus saved. It will appear in the payroll summary.")


async def audit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_command(update, context): return
    with Session() as db:
        rows = db.scalars(select(ActivityLog).order_by(ActivityLog.id.desc()).limit(20)).all()
        lines = ["LATEST ACTIVITY"]
        for row in rows:
            lines.append(f"{row.created_at:%Y-%m-%d %H:%M} | {row.actor_id} | {row.action} | {row.details[:120]}")
        await update.message.reply_text("\n".join(lines))


# ----- Scheduled jobs: expiry, alerts, daily summary, Friday payroll -----
async def expire_reservations(context: ContextTypes.DEFAULT_TYPE):
    with Session() as db:
        rows = db.scalars(select(Sale).where(Sale.status == "reserved", Sale.reserved_until < now())).all()
        for sale in rows:
            sale.status = "cancelled"
            sale.reserved_until = None
            sale.inventory_released = True
            log_action(db, OWNER_ID, "reservation_auto_expired", f"sale_id={sale.id}")
        db.commit()


async def send_daily_alerts(context: ContextTypes.DEFAULT_TYPE):
    today = now().date()
    with Session() as db:
        products_rows = db.scalars(select(Product).where(Product.active.is_(True))).all()
        for product in products_rows:
            stock = db.scalar(select(func.count(Account.id)).where(Account.product_id == product.id, Account.status == "available", or_(Account.expires_on.is_(None), Account.expires_on >= today))) or 0
            if stock <= product.low_stock_threshold and not product.low_stock_alert_active:
                try: await context.bot.send_message(OWNER_ID, f"LOW STOCK: {product.name}\nAvailable accounts: {stock}\nThreshold: {product.low_stock_threshold}")
                except Exception: log.exception("Low stock alert failed")
                product.low_stock_alert_active = True
                log_action(db, OWNER_ID, "low_stock_alert", f"product_id={product.id}; stock={stock}")
            elif stock > product.low_stock_threshold and product.low_stock_alert_active:
                try: await context.bot.send_message(OWNER_ID, f"RESTOCKED: {product.name}\nAvailable accounts: {stock}\nStock is now above the threshold of {product.low_stock_threshold}.")
                except Exception: log.exception("Restock notification failed")
                product.low_stock_alert_active = False
                log_action(db, OWNER_ID, "stock_recovered", f"product_id={product.id}; stock={stock}")
        accounts = db.scalars(select(Account).where(Account.expires_on.is_not(None))).all()
        for account in accounts:
            if account.expires_on and 0 <= (account.expires_on - today).days <= account.product.expiry_notice_days and not account.expiry_alert_sent:
                try: await context.bot.send_message(OWNER_ID, f"EXPIRING STOCK: {account.product.name}\nAccount ID: {account.id}\nExpires: {account.expires_on}")
                except Exception: log.exception("Expiry alert failed")
                account.expiry_alert_sent = True
                log_action(db, OWNER_ID, "expiry_alert", f"account_id={account.id}; expires={account.expires_on}")
        db.commit()


async def send_buyer_monitoring(target, context: ContextTypes.DEFAULT_TYPE, query_text: str = ""):
    today = now().date()
    with Session() as db:
        query = select(BuyerSubscription).order_by(BuyerSubscription.expires_on.asc()).limit(50)
        if query_text:
            pattern = f"%{query_text}%"
            query = query.where(or_(
                BuyerSubscription.buyer.ilike(pattern),
                BuyerSubscription.buyer_email.ilike(pattern),
                BuyerSubscription.product.has(Product.name.ilike(pattern)),
                BuyerSubscription.sale.has(Sale.transaction_code.ilike(pattern)),
            ))
        rows = db.scalars(query).all()
        lines = ["BUYER SUBSCRIPTION MONITORING", f"Today: {today}", ""]
        if not rows:
            lines.append("No buyer subscriptions found.")
        for sub in rows:
            remaining = (sub.expires_on - today).days
            status = "EXPIRED" if remaining <= 0 else ("EXPIRING SOON" if remaining <= 3 else "ACTIVE")
            time_left = f"Expired {abs(remaining)} day(s) ago" if remaining <= 0 else f"{remaining} day(s) left"
            lines.append(
                f"{status} | {sub.buyer}\nProduct: {sub.product.name} | Term: {sub.duration_days} days\n"
                f"Started: {sub.starts_on} | Expires: {sub.expires_on} | {time_left}\n"
                f"Sale: {sub.sale.transaction_code} | ID: {sub.sale_id}"
            )
        lines.append("\nSearch: /buyers name, email, product or transaction code")
        output = "\n\n".join(lines)
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(output, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Main Menu", callback_data="owner:home")]
        ]))
    else:
        await target.reply_text(output)


async def buyers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_command(update, context):
        return
    await send_buyer_monitoring(update.effective_message, context, " ".join(context.args).strip())


async def buyer_expiry_alerts(context: ContextTypes.DEFAULT_TYPE):
    today = now().date()
    with Session() as db:
        rows = db.scalars(select(BuyerSubscription).where(
            BuyerSubscription.expired_notice_sent.is_(False)
        )).all()
        for sub in rows:
            remaining = (sub.expires_on - today).days
            if 1 <= remaining <= 3 and not sub.expiry_notice_sent:
                try:
                    await context.bot.send_message(
                        OWNER_ID,
                        f"BUYER SUBSCRIPTION EXPIRING\nBuyer: {sub.buyer}\nProduct: {sub.product.name}\n"
                        f"Expires: {sub.expires_on} ({remaining} day(s) remaining)\nSale: {sub.sale.transaction_code}",
                    )
                except Exception:
                    log.exception("Buyer subscription expiry alert failed")
                sub.expiry_notice_sent = True
                log_action(db, OWNER_ID, "buyer_subscription_expiry_alert", f"subscription_id={sub.id}; sale_id={sub.sale_id}")
            elif remaining <= 0:
                try:
                    await context.bot.send_message(
                        OWNER_ID,
                        f"BUYER SUBSCRIPTION EXPIRED\nBuyer: {sub.buyer}\nProduct: {sub.product.name}\n"
                        f"Expired: {sub.expires_on}\nSale: {sub.sale.transaction_code}",
                    )
                except Exception:
                    log.exception("Buyer subscription expired alert failed")
                sub.expired_notice_sent = True
                log_action(db, OWNER_ID, "buyer_subscription_expired", f"subscription_id={sub.id}; sale_id={sub.sale_id}")
        db.commit()


async def inactivity_alerts(context: ContextTypes.DEFAULT_TYPE):
    today = now().date()
    check_days = [today - timedelta(days=n) for n in (1, 2, 3)]
    start_dt = datetime.combine(check_days[-1], time.min, tzinfo=TIMEZONE)
    end_dt = datetime.combine(today, time.min, tzinfo=TIMEZONE)
    with Session() as db:
        admins_rows = db.scalars(select(User).where(User.role == "admin", User.active.is_(True))).all()
        for user in admins_rows:
            count = db.scalar(select(func.count(Sale.id)).where(
                Sale.admin_id == user.telegram_id, Sale.status.in_(["completed", "partially_refunded", "refunded"]),
                Sale.completed_at >= start_dt, Sale.completed_at < end_dt,
            )) or 0
            key = f"{user.telegram_id}:{today}"
            if count > 0:
                user.inactivity_alert_active = False
            if count == 0 and not user.inactivity_alert_active:
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("Keep admin active", callback_data=f"inactive:keep:{user.telegram_id}"),
                    InlineKeyboardButton("Terminate access", callback_data=f"inactive:terminate:{user.telegram_id}"),
                ]])
                try:
                    await context.bot.send_message(OWNER_ID, f"INACTIVITY ALERT\nAdmin: {user.display_name}\nTelegram ID: {user.telegram_id}\nNo sales for 3 consecutive days ({check_days[0]} to {check_days[-1]}).\nChoose whether to keep or terminate access.", reply_markup=keyboard)
                except Exception: log.exception("Inactivity alert failed")
                user.inactivity_alert_active = True
                log_action(db, OWNER_ID, "three_day_no_sales_alert", key)
        db.commit()


async def inactivity_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != OWNER_ID:
        await q.edit_message_text("Only the owner can decide admin access.")
        return
    _, action, uid_text = q.data.split(":")
    uid = int(uid_text)
    with Session() as db:
        user = db.get(User, uid)
        if not user or user.role != "admin":
            await q.edit_message_text("This admin is no longer active.")
            return
        if action == "terminate":
            user.role, user.active = "revoked", False
            log_action(db, OWNER_ID, "inactivity_termination_approved", str(uid))
            result = "Admin access terminated by owner decision."
            notice = "Your admin access was revoked by the owner."
        else:
            log_action(db, OWNER_ID, "inactivity_termination_declined", str(uid))
            result = "Admin remains active."
            notice = "The owner chose to keep your admin access active."
        db.commit()
    await q.edit_message_text(result)
    try: await context.bot.send_message(uid, notice)
    except Exception: pass


async def daily_summary(context: ContextTypes.DEFAULT_TYPE):
    day = now().date()
    start_dt = datetime.combine(day, time.min, tzinfo=TIMEZONE)
    end_dt = start_dt + timedelta(days=1)
    with Session() as db:
        sales = db.scalars(select(Sale).where(
            Sale.status.in_(["completed", "partially_refunded", "refunded"]),
            Sale.completed_at >= start_dt, Sale.completed_at < end_dt,
        )).all()
        total = net_sale_total(sales)
        capital = sum((s.capital for s in sales if s.status != "refunded" or not s.inventory_released), Decimal("0"))
        refunds = sum((s.refunded_amount for s in sales), Decimal("0"))
        low = []
        for p in db.scalars(select(Product).where(Product.active.is_(True))).all():
            qty = db.scalar(select(func.count(Account.id)).where(Account.product_id == p.id, Account.status == "available", or_(Account.expires_on.is_(None), Account.expires_on >= day))) or 0
            if qty <= p.low_stock_threshold: low.append(f"{p.name}: {qty}")
    await context.bot.send_message(
        OWNER_ID,
        f"DAILY OWNER SUMMARY — {day}\nTransactions: {len(sales)}\nNet sales: ₱{total:.2f}\nCapital: ₱{capital:.2f}\nProfit: ₱{total-capital:.2f}\nRefunds: ₱{refunds:.2f}\nLow stock: {', '.join(low) if low else 'None'}",
    )


async def close_weekly_payroll(context: ContextTypes.DEFAULT_TYPE):
    today = now().date()
    if today.weekday() != 4:  # Friday
        return
    start = today - timedelta(days=6)  # previous Saturday
    end = today
    with Session() as db:
        existing = db.scalar(select(PayrollPeriod).where(PayrollPeriod.period_start == start, PayrollPeriod.period_end == end))
        if existing:
            return
        period = PayrollPeriod(period_start=start, period_end=end, status="pending_owner_approval")
        db.add(period); db.flush()
        admins_rows = db.scalars(select(User).where(User.role.in_(["admin", "revoked"]))).all()
        summary = []
        for user in admins_rows:
            sales = week_sales_query(db, user.telegram_id, start, end)
            total_sales = net_sale_total(sales)
            total_capital = sum((s.capital for s in sales if s.status != "refunded" or not s.inventory_released), Decimal("0"))
            profit = total_sales - total_capital
            rate = tier_rate(total_sales, db)
            salary = max(Decimal("0"), profit * rate)
            db.add(PayrollLine(
                period_id=period.id, admin_id=user.telegram_id, total_sales=total_sales,
                total_capital=total_capital, total_profit=profit, commission_rate=rate, salary=salary,
            ))
            summary.append(f"{user.display_name}: sales ₱{total_sales:.2f}; capital ₱{total_capital:.2f}; commission {rate*100:.0f}%; salary ₱{salary:.2f}")
        log_action(db, OWNER_ID, "payroll_period_closed", f"period_id={period.id}; {start} to {end}")
        db.commit()
        pid = period.id
    await context.bot.send_message(
        OWNER_ID,
        f"FRIDAY PAYROLL REPORT #{pid}\nPeriod: {start} to {end}\nStatus: Pending owner approval\n\n" + ("\n".join(summary) if summary else "No active admins.") + f"\n\nReview with /payroll, then /approvepayroll {pid}. After payout, /markpaid {pid}.",
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled Telegram update error", exc_info=context.error)


def main():
    Base.metadata.create_all(engine)
    # Seed the default commission tiers only on a fresh settings table.
    with Session() as db:
        if not db.scalar(select(func.count(CommissionTier.id))):
            for tier_id, threshold, rate in [
                (1, "1.00", "0.3000"),
                (2, "600.00", "0.3500"),
                (3, "1300.00", "0.4000"),
                (4, "2000.00", "0.4500"),
                (5, "2800.00", "0.5000"),
            ]:
                db.add(CommissionTier(
                    id=tier_id,
                    sales_threshold=Decimal(threshold),
                    commission_rate=Decimal(rate),
                ))
            db.commit()
    # Additive migration: preserve all existing sales while adding the optional buyer term.
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE sales ADD COLUMN IF NOT EXISTS buyer_duration_days INTEGER"
        ))
        connection.execute(text(
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS subscription_duration_days INTEGER NOT NULL DEFAULT 30"
        ))
    app = Application.builder().token(BOT_TOKEN).build()

    sale_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("sale", sale_start),
            CallbackQueryHandler(sale_start, pattern=r"^(owner:sales|admin:sale)$"),
        ],
        states={
            SALE_PRODUCT: [CallbackQueryHandler(sale_product_selected, pattern=r"^sale:p:\d+$")],
            SALE_TYPE: [CallbackQueryHandler(sale_type_selected, pattern=r"^sale:t:(solo_account|shared_account|solo_profile|shared_profile|invite)$")],
            SALE_BUYER: [MessageHandler(filters.TEXT & ~filters.COMMAND, sale_buyer_entered)],
        },
        fallbacks=[CommandHandler("abort", sale_cancel_command)],
        allow_reentry=True,
    )
    app.add_handler(CommandHandler("start", start))
    # Register interactive workflows before generic dashboard callbacks.
    app.add_handler(sale_conversation)
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(profile_wizard_start, pattern=r"^owner:addprofile$")],
        states={
            PROFILE_ACCOUNT: [
                CallbackQueryHandler(profile_account_selected, pattern=r"^profile:account:\d+$"),
                CallbackQueryHandler(profile_confirm_selected, pattern=r"^profile:cancel$"),
            ],
            PROFILE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, profile_name_entered)],
            PROFILE_PIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, profile_pin_entered)],
            PROFILE_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, profile_limit_entered)],
            PROFILE_CONFIRM: [CallbackQueryHandler(profile_confirm_selected, pattern=r"^profile:(save|cancel)$")],
        },
        fallbacks=[CommandHandler("abort", profile_cancel_command)],
        allow_reentry=True,
    ))
    app.add_handler(CallbackQueryHandler(stock_detail_callback, pattern=r"^stockdetail:(product|sales|reverse|delete|deleteconfirm):\d+$|^stockdetail:confirm:\d+:(restore|consume)$"))
    app.add_handler(CallbackQueryHandler(payroll_action_callback, pattern=r"^payroll:(approve|paid):\d+$"))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(commission_settings_start, pattern=r"^owner:commission$")],
        states={
            COMMISSION_SELECT: [
                CallbackQueryHandler(commission_tier_selected, pattern=r"^commission:edit:\d+$"),
                CallbackQueryHandler(commission_cancel_callback, pattern=r"^commission:cancel$"),
            ],
            COMMISSION_THRESHOLD: [
                CallbackQueryHandler(commission_cancel_callback, pattern=r"^commission:cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, commission_threshold_entered),
            ],
            COMMISSION_RATE: [
                CallbackQueryHandler(commission_cancel_callback, pattern=r"^commission:cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, commission_rate_entered),
            ],
        },
        fallbacks=[CommandHandler("abort", commission_cancel_command)],
        allow_reentry=True,
    ))
    app.add_handler(CallbackQueryHandler(payroll_dashboard, pattern=r"^owner:payroll$"))
    app.add_handler(CallbackQueryHandler(product_delete_callback, pattern=r"^productdelete:(ask|confirm):\d+$"))
    app.add_handler(CallbackQueryHandler(owner_dashboard_callback, pattern=r"^owner:(products|stockview|admins|buyers|reports|audit|home)$"))
    app.add_handler(CallbackQueryHandler(admin_dashboard_callback, pattern=r"^admin:(products|stockview|report|ranking|mysales|payroll|home)$"))
    app.add_handler(CommandHandler("approve", approve_user))
    app.add_handler(CommandHandler("reject", reject_user))
    app.add_handler(CommandHandler("makeadmin", make_admin))
    app.add_handler(CommandHandler("revoke", revoke))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("newproduct", newproduct_wizard_start), CallbackQueryHandler(newproduct_wizard_start, pattern=r"^owner:newproduct$")],
        states={
            PRODUCT_NAME: [
                CallbackQueryHandler(product_cancel_callback, pattern=r"^prod:cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, product_name_entered),
            ],
            PRODUCT_MODE: [
                CallbackQueryHandler(product_cancel_callback, pattern=r"^prod:cancel$"),
                CallbackQueryHandler(product_mode_selected, pattern=r"^prodmode:(solo_profile|shared_profile|solo_account|shared_account|both|invite)$"),
            ],
            PRODUCT_VALUES: [
                CallbackQueryHandler(product_cancel_callback, pattern=r"^prod:cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, product_value_entered),
            ],
            PRODUCT_DURATION: [
                CallbackQueryHandler(product_duration_selected, pattern=r"^prodduration:(7|14|30)$"),
                CallbackQueryHandler(product_cancel_callback, pattern=r"^prod:cancel$"),
            ],
            PRODUCT_CONFIRM: [CallbackQueryHandler(product_confirm_selected, pattern=r"^prodconfirm:(save|cancel)$")],
        },
        fallbacks=[CommandHandler("abort", product_cancel_command)],
        allow_reentry=True,
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("editproduct", edit_product_command_start), CallbackQueryHandler(edit_product_start, pattern=r"^owner:editproduct$")],
        states={
            EDIT_PRODUCT_SELECT: [
                CallbackQueryHandler(edit_product_cancel, pattern=r"^editprod:cancel$"),
                CallbackQueryHandler(edit_product_choose, pattern=r"^editprod:(product:\d+|back)$"),
            ],
            EDIT_PRODUCT_FIELD: [
                CallbackQueryHandler(edit_product_cancel, pattern=r"^editprod:cancel$"),
                CallbackQueryHandler(edit_product_choose_field, pattern=r"^editprod:field:[a-z_]+$"),
                CallbackQueryHandler(edit_product_set_mode, pattern=r"^editprod:mode:(solo_profile|shared_profile|solo_account|shared_account|both|invite)$"),
                CallbackQueryHandler(edit_product_back_fields, pattern=r"^editprod:backfields$"),
                CallbackQueryHandler(edit_product_choose, pattern=r"^editprod:back$"),
            ],
            EDIT_PRODUCT_VALUE: [
                CallbackQueryHandler(edit_product_cancel, pattern=r"^editprod:cancel$"),
                CallbackQueryHandler(edit_product_back_fields, pattern=r"^editprod:backfields$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_product_save_value),
            ],
        },
        fallbacks=[CommandHandler("abort", cancel_conversation)],
        allow_reentry=True,
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("stock", stock_wizard_start), CallbackQueryHandler(stock_wizard_start, pattern=r"^owner:stock$")],
        states={
            STOCK_PRODUCT: [
                CallbackQueryHandler(stock_product_selected, pattern=r"^stockprod:\d+$"),
                CallbackQueryHandler(stock_navigation_callback, pattern=r"^stock:cancel$"),
            ],
            STOCK_EMAIL: [
                CallbackQueryHandler(stock_navigation_callback, pattern=r"^stock:(cancel|back:\d+)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, stock_email_entered),
            ],
            STOCK_PASSWORD: [
                CallbackQueryHandler(stock_navigation_callback, pattern=r"^stock:(cancel|back:\d+)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, stock_password_entered),
            ],
            STOCK_PROFILE: [
                CallbackQueryHandler(stock_navigation_callback, pattern=r"^stock:(cancel|back:\d+)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, stock_profile_entered),
            ],
            STOCK_PIN: [
                CallbackQueryHandler(stock_navigation_callback, pattern=r"^stock:(cancel|back:\d+)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, stock_pin_entered),
            ],
            STOCK_LIMIT: [
                CallbackQueryHandler(stock_navigation_callback, pattern=r"^stock:(cancel|back:\d+)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, stock_limit_entered),
            ],
            STOCK_EXPIRY: [
                CallbackQueryHandler(stock_navigation_callback, pattern=r"^stock:(cancel|back:\d+)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, stock_expiry_entered),
            ],
            STOCK_CONFIRM: [
                CallbackQueryHandler(stock_confirm_selected, pattern=r"^stock:save$"),
                CallbackQueryHandler(stock_navigation_callback, pattern=r"^stock:(cancel|back:\d+)$"),
            ],
        },
        fallbacks=[CommandHandler("abort", stock_cancel_command)],
        allow_reentry=True,
    ))
    app.add_handler(CommandHandler("addprofile", addprofile))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("settemplate", settemplate_wizard_start), CallbackQueryHandler(settemplate_wizard_start, pattern=r"^owner:settemplate$")],
        states={
            TEMPLATE_PRODUCT: [
                CallbackQueryHandler(template_product_selected, pattern=r"^tplprod:\d+$"),
            ],
            TEMPLATE_TYPE: [
                CallbackQueryHandler(template_type_selected, pattern=r"^tpltype:(solo_account|shared_account|solo_profile|shared_profile|invite)$"),
                CallbackQueryHandler(template_back_products, pattern=r"^tpl:back_products$"),
            ],
            TEMPLATE_TEXT: [
                CallbackQueryHandler(template_back_types, pattern=r"^tpl:back_types$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, template_text_entered),
            ],
        },
        fallbacks=[
            CommandHandler("abort", template_cancel_command),
            CallbackQueryHandler(template_cancel_callback, pattern=r"^tpl:cancel$"),
        ],
        allow_reentry=True,
    ))
    app.add_handler(CommandHandler("templates", templates))
    app.add_handler(CommandHandler("products", products))
    app.add_handler(CommandHandler("stockview", stockview))
    app.add_handler(CommandHandler("admins", admins))
    app.add_handler(CallbackQueryHandler(sale_finish_callback, pattern=r"^(sale_done|sale_cancel):\d+$"))
    app.add_handler(CallbackQueryHandler(access_callback, pattern=r"^access:(approve|reject):\d+$"))
    app.add_handler(CallbackQueryHandler(cancellation_callback, pattern=r"^cancelreq:(restore|consume|decline):\d+$"))
    app.add_handler(CallbackQueryHandler(inactivity_callback, pattern=r"^inactive:(keep|terminate):\d+$"))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("mysales", mysales))
    app.add_handler(CommandHandler("buyers", buyers_command))
    app.add_handler(CommandHandler("ranking", ranking))
    app.add_handler(CommandHandler("cancel", cancel_sale))
    app.add_handler(CommandHandler("payroll", payroll))
    app.add_handler(CommandHandler("approvepayroll", approve_payroll))
    app.add_handler(CommandHandler("markpaid", mark_paid))
    app.add_handler(CommandHandler("bonus", add_bonus))
    app.add_handler(CommandHandler("audit", audit))
    app.add_error_handler(error_handler)

    # A single polling worker handles all periodic tasks to avoid a second paid service.
    if app.job_queue:
        # Polling every 15 minutes limits wakeups for a free serverless PostgreSQL database.
        # A reservation is also checked at the moment the admin confirms it.
        app.job_queue.run_repeating(expire_reservations, interval=900, first=60, name="expire-reservations")
        app.job_queue.run_daily(send_daily_alerts, time=time(hour=9, minute=0, tzinfo=TIMEZONE), name="daily-stock-expiry-alerts")
        app.job_queue.run_daily(buyer_expiry_alerts, time=time(hour=9, minute=5, tzinfo=TIMEZONE), name="daily-buyer-expiry-alerts")
        app.job_queue.run_daily(inactivity_alerts, time=time(hour=0, minute=10, tzinfo=TIMEZONE), name="three-day-inactivity-check")
        app.job_queue.run_daily(daily_summary, time=time(hour=23, minute=55, tzinfo=TIMEZONE), name="daily-owner-summary")
        # PTB/APS weekday numbering: Sunday=0, Friday=5.
        app.job_queue.run_daily(close_weekly_payroll, time=time(hour=23, minute=59, tzinfo=TIMEZONE), name="friday-payroll-close", days=(5,))
    else:
        raise RuntimeError("JobQueue is unavailable. Install python-telegram-bot[job-queue].")

    log.info("Inventory bot starting in %s timezone", TIMEZONE)
    app.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
