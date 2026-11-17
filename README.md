# Telegram Inventory, Sales & Payroll Bot

Python Telegram bot intended to run as **one Voroa Background Worker**. It uses PostgreSQL for persistent data and encrypts account credentials before storing them.

## Important: the genuinely free hosting layout

Voroa's Free workspace shares **750 running hours per month across free services and free databases**. A single always-running worker uses about 744 hours. If you also run a free Voroa PostgreSQL database all month, the shared allowance is used up after roughly 16 days and Voroa stops the free services until the next month.

To keep the bot worker on Voroa and avoid that specific shared-hour problem, this package is configured for:

- **Voroa:** one Free Background Worker, 0.1 shared vCPU / 512 MB RAM.
- **Database:** a separate free PostgreSQL provider such as Neon Free. Neon Free currently lists 1 GB storage and 100 compute-unit-hours per project/month; compute scales to zero when idle. This is a free-tier workaround, not a production uptime guarantee.

Do **not** create a Voroa free Postgres database alongside the worker if the goal is to keep the Voroa worker running all month. If you prefer to keep both worker and database inside Voroa, expect the 750 shared hours to run out before month-end.

Free plans can change limits, pause services, or exhaust quotas. This setup is suitable for testing and a small early-stage bot, but it does not guarantee business-grade 24/7 uptime or backups.

## Voroa deployment

1. Create a **new private GitHub repository** for this project and upload all files in this folder. Do not upload `.env` or real credentials.
2. Create a Telegram bot using Telegram's official `@BotFather` and copy its token.
3. Create a free PostgreSQL project with Neon (or another PostgreSQL provider). Copy its connection string. If the provider gives a pooled connection string, use that. Include `sslmode=require` if required by the provider.
4. In Voroa, choose **New → Background Worker**, connect the private GitHub repository, and select the `main` branch.
5. Choose the **Free** instance. Build command: `pip install -r requirements.txt`. Start command: `python main.py`.
6. Add these environment variables in the Voroa service's Environment tab:

   | Key | Value |
   |---|---|
   | `BOT_TOKEN` | Token from BotFather |
   | `OWNER_TELEGRAM_ID` | Your numeric Telegram user ID |
   | `DATABASE_URL` | PostgreSQL connection URL from Neon / your provider |
   | `ENCRYPTION_KEY` | A newly generated Fernet key (see below) |
   | `TIMEZONE` | `Asia/Manila` |
   | `RESERVATION_MINUTES` | `10` |
   | `LOG_LEVEL` | `INFO` |

7. Generate the encryption key on your own computer:

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Save it in Voroa as `ENCRYPTION_KEY`. Keep a secure offline backup. **Never change or lose this key** after adding real stock: the bot will no longer be able to decrypt existing credentials.

8. Deploy and watch the logs. Once it starts successfully, open the bot in Telegram and send `/start` from the owner account.

The bot creates its initial database tables at startup. Use a **new, empty database** for this version. `create_all()` does not migrate an older starter database automatically. The SQLAlchemy connection pool is disabled so idle connections can close and Neon can scale its compute down when idle; reservation-expiry cleanup runs every 15 minutes, and the confirm button independently checks the exact reservation expiry.

## Included features

- Owner-only approval of access requests using immutable Telegram user IDs.
- Owner can approve, reject, assign admin, revoke access, and decide whether an inactive admin stays or is terminated.
- Products with Solo, Shared, Both, or Invite mode.
- Separate fixed capital and selling price for Solo Account, Shared Account, Solo Profile, Shared Profile, and Invite.
- Per-product low-stock and expiry alert thresholds.
- Encrypted account email/password and encrypted profile/PIN fields.
- Multiple profile slots per account, each with its own Solo/Shared buyer limits.
- Guided `/sale` flow; buyer identifier is one name/username for regular sales, and buyer email for Invite.
- Database-backed stock reservation with expiry, transaction code, assigned delivery template, and confirm/cancel action.
- Invite workflow prepares a message; the admin must send it manually through the official service and then confirm. The bot does not send invitation emails.
- Owner-editable delivery templates for all five delivery types.
- Admin's own sales and estimated salary report; owner-wide weekly report and sales ranking.
- Friday payroll close for Saturday 00:00 through Friday 23:59 Manila time, with owner approval and paid status.
- Commission tiers and the ₱500 weekly quota progress display.
- Admin cancellation requests require owner approval; the owner chooses whether stock can be restored. Owner can also cancel a reservation directly. Completed sales use the owner-approved refund workflow.
- Daily owner summary, low-stock alerts, expiry alerts, three-consecutive-day no-sales alerts, and audit log.

## Commission rules in this build

| Weekly net sales | Commission |
|---:|---:|
| ₱1–₱599.99 | 30% |
| ₱600–₱1,299.99 | 35% |
| ₱1,300–₱1,999.99 | 40% |
| ₱2,000–₱2,799.99 | 45% |
| ₱2,800 and above | 50% |

Salary = (weekly net sales − applicable capital) × commission rate. Salary is floored at zero. Refunds reduce net sales; a fully refunded sale marked resellable releases its inventory capacity. The ₱500 quota is displayed as progress and does not automatically penalize an admin.

## Owner command reference

- `/start` — owner dashboard
- `/newproduct NAME|MODE|SOLO_ACCOUNT_CAPITAL|SHARED_ACCOUNT_CAPITAL|SOLO_PROFILE_CAPITAL|SHARED_PROFILE_CAPITAL|INVITE_CAPITAL|SOLO_ACCOUNT_PRICE|SHARED_ACCOUNT_PRICE|SOLO_PROFILE_PRICE|SHARED_PROFILE_PRICE|INVITE_PRICE|LOW_STOCK_THRESHOLD|EXPIRY_NOTICE_DAYS`
- `/editproduct PRODUCT_ID|MODE|SOLO_ACCOUNT_CAPITAL|SHARED_ACCOUNT_CAPITAL|SOLO_PROFILE_CAPITAL|SHARED_PROFILE_CAPITAL|INVITE_CAPITAL|SOLO_ACCOUNT_PRICE|SHARED_ACCOUNT_PRICE|SOLO_PROFILE_PRICE|SHARED_PROFILE_PRICE|INVITE_PRICE|LOW_STOCK_THRESHOLD|EXPIRY_NOTICE_DAYS`
- `/stock PRODUCT_ID|EMAIL|PASSWORD|PROFILE_OR_-|PIN_OR_-|1|SHARED_ACCOUNT_LIMIT|YYYY-MM-DD_OR_-`
- `/addprofile ACCOUNT_ID|PROFILE_NAME|PIN_OR_-|1|SHARED_PROFILE_LIMIT`
- `/settemplate PRODUCT_ID|TYPE|TEMPLATE_TEXT`
- `/approve TELEGRAM_USER_ID`, `/reject TELEGRAM_USER_ID`, `/makeadmin TELEGRAM_USER_ID`, `/revoke TELEGRAM_USER_ID`
- `/products`, `/stockview`, `/admins`, `/templates`, `/audit`
- `/report`, `/ranking`, `/payroll`, `/approvepayroll PERIOD_ID`, `/markpaid PERIOD_ID`
- `/bonus PERIOD_ID|ADMIN_ID|AMOUNT|NOTE`
- `/cancel SALE_ID|yes_or_no_resellable` (owner only; reserved sales only)
- `/refund SALE_ID|AMOUNT|REASON|yes_or_no_resellable`

Delivery template types: `solo_account`, `shared_account`, `solo_profile`, `shared_profile`, `invite`.

Available placeholders: `{product_name}`, `{buyer}`, `{buyer_email}`, `{email}`, `{password}`, `{profile}`, `{pin}`, `{instructions}`, `{transaction_code}`.

## Admin commands

- `/start`, `/sale`, `/products`, `/stockview`, `/report`, `/ranking`, `/mysales`
- `/refund SALE_ID|AMOUNT|REASON|yes_or_no_resellable`

## Security notes

- Never put real bot tokens, database URLs, or encryption keys in GitHub or in a group chat.
- Enter `/stock` only in the private owner-to-bot chat. The command includes credentials in the Telegram message you send; delete that command message from the chat after adding the stock if you do not want it retained in chat history.
- Account credentials are encrypted in PostgreSQL. They are shown only to the admin assigned to the specific sale and are sent in that admin's private bot chat. Owner/admin commands are disabled in group chats so credentials cannot be posted to a group.
- Admins cannot see other admins' salary or stored account credentials.
- Keep a secure backup of the encryption key and database. The free database plan may not include automatic backups.
- Do not load live customer accounts until you have tested access approval, each sale type, reservation expiry, refund handling, and payroll using dummy stock.

## Verification status and known limitations

- Python syntax compilation passes for `main.py`.
- Offline smoke tests passed for database schema creation on SQLite, commission tier boundaries, Saturday–Friday payroll date boundaries, selling-mode mapping, Fernet encryption/decryption, and reservation-capacity release.
- This environment could not install the Telegram Bot API dependency because outbound package-network access was unavailable, so a live Telegram polling test against Telegram and an integration test against Neon/Voroa PostgreSQL could not be run here.
- The first deployment must use a fresh PostgreSQL database; there is no automatic migration from the earlier starter schema.
- A refund approved after its payroll period has already been closed does not automatically rewrite the saved payroll snapshot. The owner must reconcile that closed period before payout; test refund timing carefully.
- This is an early production-style implementation, not a certified financial or high-availability system. Test carefully with dummy data before relying on it for real customer sales.
