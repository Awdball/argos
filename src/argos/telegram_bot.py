"""Interactive Telegram bot (@awdargos_bot) for induction approval, commands, and deal quiesce."""

import asyncio
import html
import logging
from typing import Optional

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, NetworkError, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from argos.config import Settings
from argos.db import get_session
from argos.inductor import induct_item
from argos.models import ApiUsageLog, Listing, ProviderGap, WatchItem
from argos.providers.registry import get_provider, list_providers
from argos.scheduler import apply_listing_feedback, effective_interval_minutes, run_item_scan_now

logger = logging.getLogger(__name__)


def _fmt_usd(val: Optional[float]) -> str:
    if val is None:
        return "N/A"
    return f"${val:,.2f}" if val % 1 else f"${val:,.0f}"


def _format_cadence_lines(item: WatchItem, settings: Settings) -> str:
    lines = []
    for pname in item.active_providers or []:
        prov = get_provider(pname)
        if prov is None:
            continue
        mins = effective_interval_minutes(prov, item.is_hot)
        freq = f"every {mins}m" if mins < 60 else f"every {mins // 60}h"
        status_tag = "active" if prov.is_configured(settings) else "awaiting API key"
        lines.append(f"• <code>{html.escape(pname)}</code>: {freq} ({status_tag})")
    return "\n".join(lines) or "• None"


def format_induction_card(item: WatchItem, settings: Settings) -> str:
    """Build an HTML Telegram summary card for an inducted watch item."""
    retailers = []
    for tr in (item.trusted_retailers or [])[:6]:
        if isinstance(tr, dict):
            name = html.escape(str(tr.get("name") or tr.get("domain") or ""))
            tag = " [uncommon/gap logged]" if tr.get("uncommon") else ""
            retailers.append(f"• {name}{tag}")

    bundles = []
    for b in item.bundle_components or []:
        if isinstance(b, dict):
            bundles.append(
                f"• {html.escape(str(b.get('name', 'Addon')))} "
                f"(+{_fmt_usd(b.get('used_value_usd'))} used / +{_fmt_usd(b.get('new_value_usd'))} new)"
            )

    neg_kw = ", ".join(html.escape(str(k)) for k in (item.negative_keywords or [])[:10]) or "none"
    queries = ", ".join(f"<code>{html.escape(q)}</code>" for q in (item.search_queries or [])[:4])
    hot_badge = "🔥 HOT" if item.is_hot else "Standard"

    parts = [
        f"🛡 <b>Argos Watch #{item.id}: {html.escape(item.name)}</b>",
        f"Category: <code>{html.escape(item.category)}</code> | Mode: <b>{hot_badge}</b> | Status: <code>{html.escape(item.status)}</code>",
        "",
        f"<b>Pricing Targets</b>",
        f"• Retail Baseline: <b>{_fmt_usd(item.retail_baseline_usd)}</b>",
        f"• New / Open-Box Target: <b>≤ {_fmt_usd(item.new_deal_target_usd)}</b>",
        f"• Secondhand Target: <b>≤ {_fmt_usd(item.used_deal_target_usd)}</b>",
        f"• Geo Radius: <b>{item.radius_miles} mi</b> around <code>{html.escape(settings.home_postal)}</code>",
    ]
    if item.deal_likelihood:
        parts.extend(["", f"<b>Market Odds:</b> {html.escape(item.deal_likelihood)}"])
    if bundles:
        parts.extend(["", "<b>Bundle Add-on Allowances:</b>", *bundles])
    parts.extend(
        [
            "",
            f"<b>Queries:</b> {queries}",
            f"<b>Exclusions:</b> {neg_kw}",
        ]
    )
    if retailers:
        parts.extend(["", "<b>Trusted Retailers:</b>", *retailers])
    parts.extend(["", "<b>Check Cadence (≥1×/day guaranteed):</b>", _format_cadence_lines(item, settings)])
    if item.induction_summary:
        parts.extend(["", f"<i>{html.escape(item.induction_summary)}</i>"])
    return "\n".join(parts)


def induction_keyboard(item: WatchItem) -> Optional[InlineKeyboardMarkup]:
    if item.status == "active":
        return None
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Activate Watch", callback_data=f"induct:activate:{item.id}"),
                InlineKeyboardButton("🔥 Activate Hot", callback_data=f"induct:hot:{item.id}"),
            ],
            [InlineKeyboardButton("🗑️ Cancel", callback_data=f"induct:cancel:{item.id}")],
        ]
    )


def format_deal_alert(item: WatchItem, listing: Listing, alert_reason: str) -> str:
    """Format an HTML deal alert card with landed price and distance."""
    header = "📉 <b>PRICE DROP</b>" if alert_reason == "price_drop" else "🔥 <b>NEW DEAL FOUND</b>"
    ship_note = f" (+{_fmt_usd(listing.shipping_usd)} ship)" if listing.shipping_usd > 0 else ""
    pct_str = ""
    if item.retail_baseline_usd and item.retail_baseline_usd > 0:
        pct = ((item.retail_baseline_usd - listing.landed_cost_usd) / item.retail_baseline_usd) * 100.0
        if pct > 0:
            pct_str = f" — <b>{pct:.0f}% below retail</b>"

    loc_bits = []
    if listing.location_text:
        loc_bits.append(html.escape(listing.location_text))
    if listing.distance_miles is not None:
        loc_bits.append(f"{listing.distance_miles:.1f} mi away")
    loc_line = " | ".join(loc_bits)

    lines = [
        f"{header}: <b>{html.escape(item.name)}</b> (Watch #{item.id})",
        f"<b>{html.escape(listing.title)}</b>",
        f"Source: <code>{html.escape(listing.provider)}</code> | Cond: <code>{html.escape(listing.condition)}</code>",
        f"Landed Price: <b>{_fmt_usd(listing.landed_cost_usd)}</b>{ship_note}{pct_str}",
    ]
    if loc_line:
        lines.append(f"Location: {loc_line}")
    if listing.matched_bundle and listing.matched_bundle_names:
        lines.append(
            f"Bundle Detected: <b>{html.escape(' + '.join(listing.matched_bundle_names))}</b>"
        )
    if listing.deal_reason:
        lines.append(f"Assessment: {html.escape(listing.deal_reason)}")
    lines.append(f"Score: <b>{listing.deal_score:.0f}/100</b> | Listing #{listing.id}")
    lines.append(f'<a href="{html.escape(listing.url, quote=True)}">Open Listing</a>')
    return "\n".join(lines)


def deal_quiesce_keyboard(listing_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💸 Not Good Enough", callback_data=f"q:price:{listing_id}"),
                InlineKeyboardButton("📍 Too Far", callback_data=f"q:distance:{listing_id}"),
            ],
            [
                InlineKeyboardButton("⏳ Good Deal, Bad Timing", callback_data=f"q:timing:{listing_id}"),
                InlineKeyboardButton("🚫 Wrong Item", callback_data=f"q:wrong:{listing_id}"),
            ],
            [
                InlineKeyboardButton("⌛ Expired / OOS", callback_data=f"q:expired:{listing_id}"),
                InlineKeyboardButton("🚩 False Positive", callback_data=f"q:false_positive:{listing_id}"),
            ],
            [InlineKeyboardButton("✅ Bought / Archive Watch", callback_data=f"q:bought:{listing_id}")],
        ]
    )


class ArgosTelegramBot:
    """Manages Telegram polling lifecycle and outbound interactive notifications."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.app: Optional[Application] = None

    @property
    def is_enabled(self) -> bool:
        return bool(
            self.settings.enable_telegram
            and self.settings.telegram_bot_token.strip()
            and self.settings.telegram_chat_id is not None
        )

    def _authorized(self, update: Update) -> bool:
        if self.settings.telegram_chat_id is None:
            return False
        chat = update.effective_chat
        return bool(chat and chat.id == self.settings.telegram_chat_id)

    async def start(self) -> None:
        if not self.is_enabled:
            logger.info("Telegram bot disabled or missing token/chat_id.")
            return
        self.app = (
            Application.builder()
            .token(self.settings.telegram_bot_token.strip())
            .connect_timeout(15.0)
            .read_timeout(20.0)
            .concurrent_updates(True)
            .build()
        )
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("help", self.cmd_start))
        self.app.add_handler(CommandHandler("add", self.cmd_add))
        self.app.add_handler(CommandHandler("list", self.cmd_list))
        self.app.add_handler(CommandHandler("info", self.cmd_info))
        self.app.add_handler(CommandHandler("hot", self.cmd_hot))
        self.app.add_handler(CommandHandler("pause", self.cmd_pause))
        self.app.add_handler(CommandHandler("resume", self.cmd_resume))
        self.app.add_handler(CommandHandler("archive", self.cmd_archive))
        self.app.add_handler(CommandHandler("check", self.cmd_check))
        self.app.add_handler(CommandHandler("gaps", self.cmd_gaps))
        self.app.add_handler(CommandHandler("providers", self.cmd_providers))
        self.app.add_handler(CommandHandler("status", self.cmd_providers))
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))
        self.app.add_error_handler(self._on_error)

        await self.app.initialize()
        await self.app.start()
        if self.app.updater is not None:
            await self.app.updater.start_polling(drop_pending_updates=False)
        logger.info("Argos Telegram bot polling started.")

    async def stop(self) -> None:
        if self.app is None:
            return
        try:
            if self.app.updater is not None and self.app.updater.running:
                await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
        except Exception as exc:
            logger.warning("Error stopping Telegram bot: %s", exc)

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Unhandled Telegram bot error: %s", context.error, exc_info=context.error)
        if isinstance(update, Update) and update.effective_message is not None:
            if not isinstance(context.error, (NetworkError, TimedOut)):
                await self._reply_safe(
                    update.effective_message,
                    "⚠️ Argos hit an internal error handling that command. Check logs for details.",
                    parse_mode=None,
                )

    async def _reply_safe(
        self,
        message,
        text: str,
        parse_mode: Optional[str] = ParseMode.HTML,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        disable_web_page_preview: bool = False,
        attempts: int = 3,
    ) -> Optional[int]:
        """Send a reply with retry on transient network/timeout errors."""
        for attempt in range(1, max(1, attempts) + 1):
            try:
                kwargs = {"reply_markup": reply_markup, "disable_web_page_preview": disable_web_page_preview}
                if parse_mode is not None:
                    kwargs["parse_mode"] = parse_mode
                msg = await message.reply_text(text, **kwargs)
                return getattr(msg, "message_id", None)
            except (NetworkError, TimedOut) as exc:
                logger.warning(
                    "Telegram reply attempt %d/%d failed (%s)", attempt, attempts, exc
                )
                if attempt < attempts:
                    await asyncio.sleep(1.5)
            except TelegramError as exc:
                logger.warning("Telegram reply failed: %s", exc)
                return None
        return None

    async def _send_safe(
        self,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        attempts: int = 3,
    ) -> Optional[int]:
        if not self.is_enabled or self.app is None or self.settings.telegram_chat_id is None:
            return None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                msg = await self.app.bot.send_message(
                    chat_id=self.settings.telegram_chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
                return msg.message_id
            except Forbidden:
                logger.warning(
                    "Telegram 403 Forbidden: user must open https://t.me/awdargos_bot and send /start."
                )
                return None
            except (NetworkError, TimedOut) as exc:
                logger.warning(
                    "Telegram send attempt %d/%d failed (%s)", attempt, attempts, exc
                )
                if attempt < attempts:
                    await asyncio.sleep(1.5)
            except TelegramError as exc:
                logger.warning("Telegram send failed: %s", exc)
                return None
        return None

    async def send_induction_card(self, item: WatchItem) -> Optional[int]:
        return await self._send_safe(
            format_induction_card(item, self.settings),
            reply_markup=induction_keyboard(item),
        )

    async def send_deal_alert(
        self, watch_item_id: int, listing_id: int, alert_reason: str
    ) -> Optional[int]:
        with get_session() as session:
            item = session.get(WatchItem, watch_item_id)
            listing = session.get(Listing, listing_id)
            if not item or not listing:
                return None
            text = format_deal_alert(item, listing, alert_reason)
        return await self._send_safe(text, reply_markup=deal_quiesce_keyboard(listing_id))

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update) or not update.message:
            return
        with get_session() as session:
            active_cnt = len(
                session.scalars(select(WatchItem).where(WatchItem.status == "active")).all()
            )
        msg = (
            "🛡 <b>Argos Deal Watch Active</b>\n"
            f"Watching <b>{active_cnt}</b> active item(s) around <code>{html.escape(self.settings.home_postal)}</code>.\n\n"
            "<b>Commands:</b>\n"
            "• <code>/add &lt;item&gt;</code> — Research & induct item (prefix <code>--hot</code> for fast polling)\n"
            "• <code>/list</code> — List watched items\n"
            "• <code>/info &lt;id&gt;</code> — Details & recent hits\n"
            "• <code>/check &lt;id&gt;</code> — Scan all providers now\n"
            "• <code>/hot &lt;id&gt;</code> — Toggle hot-item frequency\n"
            "• <code>/pause &lt;id&gt;</code> | <code>/resume &lt;id&gt;</code> | <code>/archive &lt;id&gt;</code>\n"
            "• <code>/gaps</code> — Niche retailers needing custom scrapers\n"
            "• <code>/providers</code> — Provider health & API budget"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

    async def cmd_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update) or not update.message:
            return
        raw = " ".join(context.args).strip()
        if not raw:
            await update.message.reply_text("Usage: <code>/add [--hot] item description</code>", parse_mode=ParseMode.HTML)
            return
        is_hot = False
        if raw.lower().startswith("--hot "):
            is_hot = True
            raw = raw[6:].strip()
        elif raw.lower().startswith("hot:"):
            is_hot = True
            raw = raw[4:].strip()

        # Best-effort ack: never abort induction if Telegram has a transient hiccup.
        await self._reply_safe(
            update.message,
            f"🔍 Inducting & researching <b>{html.escape(raw)}</b>...",
            parse_mode=ParseMode.HTML,
        )
        try:
            item_id = await induct_item(
                raw, self.settings, is_hot=is_hot, auto_activate=False
            )
        except Exception as exc:
            logger.exception("Induction failed for %r", raw)
            await self._reply_safe(
                update.message,
                f"⚠️ Induction failed for <b>{html.escape(raw)}</b>: {html.escape(str(exc))}",
                parse_mode=ParseMode.HTML,
            )
            return
        with get_session() as session:
            item = session.get(WatchItem, item_id)
            assert item is not None
            card = format_induction_card(item, self.settings)
            kb = induction_keyboard(item)
        await self._reply_safe(
            update.message,
            card,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
            disable_web_page_preview=True,
        )

    async def cmd_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update) or not update.message:
            return
        with get_session() as session:
            items = session.scalars(select(WatchItem).order_by(WatchItem.id.asc())).all()
            if not items:
                await update.message.reply_text("No watched items yet. Send <code>/add &lt;item&gt;</code>.", parse_mode=ParseMode.HTML)
                return
            lines = ["📋 <b>Argos Watchlist</b>"]
            for it in items:
                hot = "🔥" if it.is_hot else ""
                lines.append(
                    f"• <b>#{it.id}</b> {hot} [{html.escape(it.status)}] <b>{html.escape(it.name)}</b> — "
                    f"Used ≤ {_fmt_usd(it.used_deal_target_usd)} | New ≤ {_fmt_usd(it.new_deal_target_usd)} | {it.radius_miles} mi"
                )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update) or not update.message:
            return
        if not context.args or not context.args[0].isdigit():
            await update.message.reply_text("Usage: <code>/info &lt;id&gt;</code>", parse_mode=ParseMode.HTML)
            return
        item_id = int(context.args[0])
        with get_session() as session:
            item = session.get(WatchItem, item_id)
            if not item:
                await update.message.reply_text(f"Watch #{item_id} not found.")
                return
            card = format_induction_card(item, self.settings)
            recent = session.scalars(
                select(Listing)
                .where(Listing.watch_item_id == item_id)
                .order_by(Listing.last_seen_at.desc())
                .limit(5)
            ).all()
            if recent:
                card += "\n\n<b>Recent Listings:</b>\n" + "\n".join(
                    f"• [{html.escape(l.state)}] {_fmt_usd(l.landed_cost_usd)} ({html.escape(l.provider)}): "
                    f'<a href="{html.escape(l.url, quote=True)}">{html.escape(l.title[:60])}</a>'
                    for l in recent
                )
        await update.message.reply_text(card, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    async def cmd_hot(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update) or not update.message:
            return
        if not context.args or not context.args[0].isdigit():
            await update.message.reply_text("Usage: <code>/hot &lt;id&gt;</code>", parse_mode=ParseMode.HTML)
            return
        item_id = int(context.args[0])
        with get_session() as session:
            item = session.get(WatchItem, item_id)
            if not item:
                await update.message.reply_text(f"Watch #{item_id} not found.")
                return
            item.is_hot = not item.is_hot
            label = "🔥 HOT" if item.is_hot else "Standard"
            cadence = _format_cadence_lines(item, self.settings)
        await update.message.reply_text(
            f"Watch #{item_id} set to <b>{label}</b>.\n{cadence}", parse_mode=ParseMode.HTML
        )

    async def _set_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, status: str
    ) -> None:
        if not self._authorized(update) or not update.message:
            return
        if not context.args or not context.args[0].isdigit():
            await update.message.reply_text(f"Usage: <code>/{status} &lt;id&gt;</code>", parse_mode=ParseMode.HTML)
            return
        item_id = int(context.args[0])
        with get_session() as session:
            item = session.get(WatchItem, item_id)
            if not item:
                await update.message.reply_text(f"Watch #{item_id} not found.")
                return
            item.status = status
        await update.message.reply_text(f"Watch #{item_id} status → <code>{status}</code>.", parse_mode=ParseMode.HTML)

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set_status(update, context, "paused")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set_status(update, context, "active")

    async def cmd_archive(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set_status(update, context, "archived")

    async def cmd_check(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update) or not update.message:
            return
        if not context.args or not context.args[0].isdigit():
            await update.message.reply_text("Usage: <code>/check &lt;id&gt;</code>", parse_mode=ParseMode.HTML)
            return
        item_id = int(context.args[0])
        await self._reply_safe(
            update.message,
            f"🔎 Scanning all configured providers for Watch #{item_id}...",
            parse_mode=None,
        )
        try:
            res = await run_item_scan_now(item_id, self.settings, alert_cb=self.send_deal_alert)
        except ValueError as exc:
            await self._reply_safe(update.message, str(exc), parse_mode=None)
            return
        summary = (
            f"Scan complete for Watch #{item_id}: "
            f"<b>{res['items_found']}</b> listing(s) evaluated, "
            f"<b>{res['new_alerts']}</b> new alert(s)."
        )
        if res["errors"]:
            summary += "\nErrors: " + html.escape("; ".join(res["errors"]))
        await self._reply_safe(update.message, summary, parse_mode=ParseMode.HTML)

    async def cmd_gaps(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update) or not update.message:
            return
        with get_session() as session:
            gaps = session.scalars(
                select(ProviderGap)
                .where(ProviderGap.status == "open")
                .order_by(ProviderGap.occurrences.desc())
                .limit(15)
            ).all()
            if not gaps:
                await update.message.reply_text("No open provider gaps logged.")
                return
            lines = ["🧩 <b>Open Provider Gaps (Uncommon / Blocked Retailers)</b>"]
            for g in gaps:
                lines.append(
                    f"• <b>{html.escape(g.retailer_name)}</b> (<code>{html.escape(g.domain)}</code>, ×{g.occurrences}) — "
                    f"{html.escape(g.reason[:100])}"
                )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_providers(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update) or not update.message:
            return
        provs = list_providers(self.settings)
        with get_session() as session:
            watches = len(session.scalars(select(WatchItem).where(WatchItem.status == "active")).all())
            listings = len(session.scalars(select(Listing)).all())
            usages = session.scalars(select(ApiUsageLog)).all()
            usage_str = ", ".join(
                f"{u.provider}: {u.calls_count} calls (${u.estimated_cost_usd:.2f})"
                for u in usages
            ) or "none"
        lines = [
            "⚙️ <b>Argos Providers & Status</b>",
            f"Active watches: <b>{watches}</b> | Tracked listings: <b>{listings}</b>",
            f"Metered API usage: {html.escape(usage_str)}",
            "",
        ]
        for p in provs:
            badge = "✅" if p["configured"] else "⏸️ needs key"
            lines.append(
                f"• {badge} <b>{html.escape(p['display_name'])}</b> "
                f"(<code>{html.escape(p['name'])}</code>) — "
                f"{p['normal_interval_minutes']}m / hot {p['hot_interval_minutes']}m"
            )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update) or not update.callback_query:
            return
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        parts = data.split(":")

        if len(parts) == 3 and parts[0] == "induct":
            action, raw_id = parts[1], parts[2]
            if not raw_id.isdigit():
                return
            item_id = int(raw_id)
            with get_session() as session:
                item = session.get(WatchItem, item_id)
                if not item:
                    await query.edit_message_reply_markup(reply_markup=None)
                    return
                if action == "activate":
                    item.status = "active"
                    note = f"✅ Watch #{item_id} activated (Standard cadence)."
                elif action == "hot":
                    item.status = "active"
                    item.is_hot = True
                    note = f"🔥 Watch #{item_id} activated in HOT mode!"
                elif action == "cancel":
                    item.status = "archived"
                    note = f"🗑️ Watch #{item_id} cancelled/archived."
                else:
                    return
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(note)
            if action in ("activate", "hot"):
                await run_item_scan_now(item_id, self.settings, alert_cb=self.send_deal_alert)
            return

        if len(parts) == 3 and parts[0] == "q":
            action, raw_id = parts[1], parts[2]
            if not raw_id.isdigit():
                return
            listing_id = int(raw_id)
            with get_session() as session:
                res = apply_listing_feedback(session, listing_id, action, self.settings)
            if not res:
                await query.message.reply_text(f"Listing #{listing_id} not found.")
                return
            msg_map = {
                "quiesced_price": f"💸 Listing #{listing_id} muted at current price (will only re-alert on ≥{int(self.settings.price_drop_realert_pct*100)}% drop).",
                "quiesced_distance": f"📍 Listing #{listing_id} muted permanently (too far).",
                "quiesced_timing": f"⏳ Listing #{listing_id} snoozed for {self.settings.timing_snooze_days} days without changing price targets.",
                "quiesced_wrong_item": f"🚫 Listing #{listing_id} suppressed as wrong item/spam.",
                "quiesced_expired": f"⌛ Listing #{listing_id} flagged as expired / out of stock and muted.",
                "quiesced_false_positive": f"🚩 Listing #{listing_id} flagged as false positive and suppressed.",
                "bought": f"✅ Marked Listing #{listing_id} bought and archived Watch #{res['watch_item_id']}. Congrats!",
            }
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(msg_map.get(res["state"], f"Updated listing #{listing_id}."))
