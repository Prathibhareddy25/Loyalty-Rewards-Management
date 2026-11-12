# Customer Loyalty Points Management System

A Flask + MySQL web application for managing customer loyalty accounts, customer shopping, orders, accruals, returns/reversals, expiry and an auditable points ledger.

## 1. Features
- Dashboard KPIs and recent ledger activity.
- Customer registration with a one-to-one loyalty account.
- Customer login and a product catalogue with a shopping cart.
- Customer order history and returns limited to the signed-in customer's own purchases.
- Administrator access to the full customer directory and loyalty operations.
- Order entry; eligible net spend determines points earned.
- Tier-based earning multipliers and qualifying-spend progression.
- Reward catalogue and points redemption with balance validation.
- Partial/full returns; proportional points reversals reference the original accrual.
- Lot-based FIFO redemption and expiry; expiry runs are idempotent.
- Searchable transaction ledger and CSV export.
- Reports by transaction type and tier.

## 2. Requirements
- Python 3.10+
- MySQL 8+
- VS Code (recommended)

## 3. Installation (Windows / VS Code)
Open this folder in VS Code. In Terminal (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
copy .env.example .env
```

Edit `.env`. Set your MySQL settings, `SECRET_KEY` to a long random value, and configure a private `ADMIN_EMAIL` and strong `ADMIN_PASSWORD`. Admin accounts are configured through these environment variables; there is no default admin password.

In MySQL Workbench, open `schema.sql` and execute it. It creates `loyalty_db`.

Then start the app:

```powershell
python app.py
```

Open `http://127.0.0.1:5000/login` and sign in with the configured admin email and password. Open `http://127.0.0.1:5000/setup` once to create tables and seed tiers, rules and rewards. For the existing loyalty database schema, this step applies an idempotent, additive migration: it retains existing tables and rows, maps the app to the existing column names, and adds only fields the app requires. It also creates a separate `expiry_batch_run` table; the existing `expirybatch` table is left unchanged. Then add products in Products, and share `http://127.0.0.1:5000/register` with customers so they can sign up and shop.

## 4. Main workflow
1. As an administrator, add products in Products and review all profiles in Customers. Admin-created or older customer profiles can be enabled for login from the directory by setting a password.
2. Customers register, sign in, add products to their cart and place orders. Orders and returns are scoped to the signed-in customer.
3. Admins can also record manual orders and process returns for any customer.
4. Add rewards in Rewards and redeem points in Redeem. The app checks the live ledger balance and consumes oldest available accrual lots first.
5. Run Expiry using a date. Only unspent points in expired lots are debited. Repeating the same date does not expire those points twice.
6. Review Ledger and Reports; export the ledger as CSV.

## 5. IDs and ledger design
- Loyalty account: `LOY-<customer-id>` (zero-padded customer ID in the UI).
- Orders: `ORD-2026-000001`.
- Points transactions: `PTS-2026-00000001`.
- Redemptions: `RDM-2026-000001`.
- Expiry batches: `EXP-2026-000001`.

The points ledger is append-only through the UI: operations add new rows instead of editing or deleting old points transactions. `source_accrual_id` traces redemption, reversal and expiry debits to the accrual lot. The account balance is calculated as the sum of all ledger entries, not stored as a mutable balance.

## 6. Default business rules
- Bronze: 1.00x; Silver: 1.25x from ₹5,000; Gold: 1.50x from ₹15,000; Platinum: 2.00x from ₹30,000.
- Default rule: 1 point per ₹1 before tier multiplier; minimum eligible order ₹100; expiry after 365 days.
- Net spend = gross amount − discount amount.
- Points = floor(net spend × rule rate × tier multiplier).
- Tier progression uses qualifying net spend; returns reduce qualifying spend.
- Redemption consumes earliest-expiring/oldest accrual lots first (FIFO by creation date).

Change tier thresholds, rates and expiry policy to match the organisation's approved rules before production use.

## 7. Synthetic benchmark dataset

The generator writes to a separate `loyalty_benchmark_db` database, leaving `loyalty_db` and its customer records untouched. Run it from this folder:

```powershell
.\.venv\Scripts\python.exe generate_benchmark_data.py
```

By default, it creates 20,000 synthetic customers, 100,000 orders, three order lines per order, and 300,000 accrual transactions (one per line). It uses the MySQL connection settings from `.env`, seeds the standard tiers/rule/rewards, verifies row counts and point totals in a single transaction, and prints a deterministic SHA-256 checksum. The generator refuses to append into non-empty benchmark tables. Optional `--seed`, `--reference-date`, `--database`, `--customers`, `--orders`, and `--transactions` arguments support reproducible test runs; transaction count must equal three times the order count, and orders are evenly distributed across customers.

To browse the benchmark records in the web app, point `DB_NAME` (or `DATABASE_URL`) at `loyalty_benchmark_db` and restart the app. Keep the normal `loyalty_db` configuration to continue using live customer data. This generator creates clean synthetic data; it does not add labelled anomalies or real customer information.

## 8. Database relationships
- Customer 1:1 LoyaltyAccount.
- Tier 1:N LoyaltyAccount.
- Customer 1:N Order.
- Order 1:N OrderLine.
- LoyaltyAccount 1:N PointsTransaction.
- Order 1:N PointsTransaction (optional for non-order ledger events).
- PointsTransaction 1:N child transactions through `source_accrual_id`.
- LoyaltyAccount 1:N Redemption; Reward 1:N Redemption.
- ExpiryBatch 1:N conceptually through the transaction description in this starter version. For regulated production audit, add an explicit `expiry_batch_id` FK to PointsTransaction.

## 9. Validation and production notes
- Unique constraints exist for all public codes and customer email.
- Foreign keys enforce parent-child integrity.
- Redemption validates available balance before committing.
- Expiry is idempotent because each run computes remaining balance per accrual lot.
- Return validation prevents returning more than the remaining order value.
- Passwords are stored as hashes; admin credentials and the session signing key must be configured privately in `.env`.
- Customer routes only expose the current customer's orders and returns; all customer directory and loyalty administration routes require an admin session.
- The shop records orders but does not process payments, shipping or inventory. Customer registration does not verify email addresses. The app is an operational MVP, not yet a production-certified financial ledger. Before production: add email verification and login rate limits, row-level locking or serializable isolation for concurrent redemptions, immutable DB triggers/permissions, structured audit logs, managed migrations, backups, monitoring, and explicit expiry-batch linkage.
- The benchmark generator prints a deterministic dataset checksum; anomaly ground-truth labels are not included.
