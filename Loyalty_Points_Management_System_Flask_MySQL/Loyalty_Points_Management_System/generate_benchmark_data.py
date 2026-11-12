import argparse
import hashlib
import json
import random
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from flask import Flask
from sqlalchemy import create_engine, func, make_url, select

from app import (
    Customer,
    LoyaltyAccount,
    Order,
    OrderLine,
    PointsTransaction,
    Rule,
    Tier,
    app,
    db,
    seed_default_data,
)


DEFAULT_REFERENCE_DATE = date(2026, 11, 12)
LINES_PER_ORDER = 3
BATCH_SIZE = 1000


def _insert_batch(connection, table, rows, checksum):
    if not rows:
        return
    for row in rows:
        encoded = json.dumps(
            [table.name, row],
            sort_keys=True,
            separators=(",", ":"),
            default=lambda value: value.isoformat() if hasattr(value, "isoformat") else str(value),
        )
        checksum.update(encoded.encode("utf-8"))
        checksum.update(b"\n")
    connection.execute(table.insert(), rows)


def _insert_rows(connection, table, rows, checksum, batch_size):
    batch = []
    count = 0
    for row in rows:
        batch.append(row)
        if len(batch) == batch_size:
            _insert_batch(connection, table, batch, checksum)
            count += len(batch)
            batch.clear()
    if batch:
        _insert_batch(connection, table, batch, checksum)
        count += len(batch)
    return count


def _order_specs(customer_count, orders_per_customer, seed, reference_date, minimum_order):
    rng = random.Random(seed)
    start_date = reference_date - timedelta(days=180)
    lower_amount = max(
        1000,
        int(minimum_order.to_integral_value(rounding=ROUND_CEILING)),
    )
    upper_amount = lower_amount + 7000
    segment_days = max(1, 180 // max(orders_per_customer, 1))

    for customer_index in range(customer_count):
        for order_index in range(orders_per_customer):
            segment_start = min(179, order_index * 180 // orders_per_customer)
            day_offset = min(179, segment_start + rng.randrange(segment_days))
            order_day = start_date + timedelta(days=day_offset)
            order_date = datetime.combine(order_day, time.min) + timedelta(seconds=rng.randrange(86400))
            amount = rng.randint(lower_amount, upper_amount)
            first_line = amount // LINES_PER_ORDER
            line_amounts = (
                first_line,
                first_line,
                amount - 2 * first_line,
            )
            yield customer_index, order_index, order_date, amount, line_amounts


def _tier_for_spend(spend, tiers):
    selected = tiers[0]
    for tier in tiers:
        if spend >= Decimal(tier.min_qualifying_spend):
            selected = tier
    return selected


def _batch_order_rows(
    customer_count,
    orders_per_customer,
    seed,
    reference_date,
    minimum_order,
    points_per_currency,
    tiers,
    expiry_days,
    batch_size,
):
    qualifying_spend = [Decimal("0")] * customer_count
    order_rows = []
    line_rows = []
    transaction_rows = []
    order_number = 0
    transaction_number = 0
    line_number = 0

    for customer_index, _, order_date, amount, line_amounts in _order_specs(
        customer_count, orders_per_customer, seed, reference_date, minimum_order
    ):
        order_number += 1
        account_id = f"LOY-SYN-{customer_index + 1:06d}"
        customer_id = f"CUS-SYN-{customer_index + 1:06d}"
        order_id = f"ORD-SYN-{reference_date.year}-{order_number:06d}"
        net_amount = Decimal(amount)
        tier = _tier_for_spend(qualifying_spend[customer_index], tiers)

        order_rows.append({
            "order_id": order_id,
            "customer_id": customer_id,
            "order_date": order_date,
            "gross_amount": net_amount,
            "discount_amount": Decimal("0"),
            "net_amount": net_amount,
            "returned_amount": Decimal("0"),
            "eligible_net_spend": net_amount,
            "order_status": "COMPLETED",
        })

        for line_index, line_amount in enumerate(line_amounts, start=1):
            line_number += 1
            transaction_number += 1
            line_total = Decimal(line_amount)
            points = int(
                (line_total * points_per_currency * Decimal(tier.earning_rate))
                .to_integral_value(rounding=ROUND_FLOOR)
            ) if net_amount >= minimum_order else 0
            line_rows.append({
                "order_line_id": f"LINE-SYN-{line_number:08d}",
                "order_id": order_id,
                "sku": f"Benchmark item {line_index}",
                "quantity": 1,
                "unit_price": line_total,
                "line_net_amount": line_total,
            })
            transaction_rows.append({
                "transaction_id": f"PTS-SYN-{reference_date.year}-{transaction_number:08d}",
                "account_id": account_id,
                "transaction_type": "ACCRUAL",
                "points_change": points,
                "order_id": order_id,
                "redemption_id": None,
                "reference_transaction_id": None,
                "lot_expiry_date": order_date.date() + timedelta(days=expiry_days),
                "expiry_batch_id": None,
                "description": f"Benchmark accrual for line {line_index} on {order_id}",
                "issue_type": "NONE",
                "transaction_date": order_date,
            })
        qualifying_spend[customer_index] += net_amount

        if len(order_rows) == batch_size:
            yield order_rows, line_rows, transaction_rows
            order_rows, line_rows, transaction_rows = [], [], []

    if order_rows:
        yield order_rows, line_rows, transaction_rows


def generate_benchmark_data(
    customer_count=20_000,
    order_count=100_000,
    transaction_count=300_000,
    seed=20261112,
    reference_date=DEFAULT_REFERENCE_DATE,
    batch_size=BATCH_SIZE,
):
    if customer_count < 1 or order_count < 1 or batch_size < 1:
        raise ValueError("Customer, order, and batch counts must be positive.")
    if order_count % customer_count:
        raise ValueError("Order count must divide evenly among customers.")
    orders_per_customer = order_count // customer_count
    if transaction_count != order_count * LINES_PER_ORDER:
        raise ValueError(
            f"This dataset creates {LINES_PER_ORDER} accrual transactions per order; "
            f"transaction_count must be {order_count * LINES_PER_ORDER}."
        )

    tiers = Tier.query.filter_by(active=True).order_by(Tier.min_qualifying_spend.asc()).all()
    rule = Rule.query.filter_by(active=True).order_by(Rule.id.desc()).first()
    if not tiers or not rule:
        raise RuntimeError("Initialize tiers and earning rules before generating the benchmark database.")

    targets = (Customer, LoyaltyAccount, Order, OrderLine, PointsTransaction)
    existing_counts = {
        model.__tablename__: db.session.execute(
            select(func.count()).select_from(model.__table__)
        ).scalar_one()
        for model in targets
    }
    populated = {table: count for table, count in existing_counts.items() if count}
    if populated:
        raise RuntimeError(
            "Refusing to add benchmark rows to non-empty benchmark tables: "
            + ", ".join(f"{table}={count}" for table, count in populated.items())
        )

    minimum_order = Decimal(rule.minimum_order_amount)
    points_per_currency = Decimal(rule.points_per_currency)
    expiry_days = rule.expiry_days
    final_spend = [Decimal("0")] * customer_count
    first_order_dates = [None] * customer_count
    for customer_index, _, order_date, amount, _ in _order_specs(
        customer_count, orders_per_customer, seed, reference_date, minimum_order
    ):
        final_spend[customer_index] += Decimal(amount)
        if first_order_dates[customer_index] is None:
            first_order_dates[customer_index] = order_date

    checksum = hashlib.sha256()
    expected_points = 0
    inserted_orders = 0
    inserted_lines = 0
    inserted_transactions = 0

    with db.engine.begin() as connection:
        inserted_customers = _insert_rows(
            connection,
            Customer.__table__,
            ({
                "customer_id": f"CUS-SYN-{index + 1:06d}",
                "first_name": f"Benchmark Customer {index + 1:06d}",
                "email": f"benchmark{index + 1:06d}@example.test",
                "phone": f"+91000{index + 1:08d}",
                "password_hash": None,
                "created_at": first_order_dates[index],
            } for index in range(customer_count)),
            checksum,
            batch_size,
        )
        inserted_accounts = _insert_rows(
            connection,
            LoyaltyAccount.__table__,
            ({
                "account_id": f"LOY-SYN-{index + 1:06d}",
                "customer_id": f"CUS-SYN-{index + 1:06d}",
                "tier_id": _tier_for_spend(final_spend[index], tiers).id,
                "qualifying_spend": final_spend[index],
                "account_status": "ACTIVE",
                "created_at": first_order_dates[index],
            } for index in range(customer_count)),
            checksum,
            batch_size,
        )

        order_batches = _batch_order_rows(
            customer_count,
            orders_per_customer,
            seed,
            reference_date,
            minimum_order,
            points_per_currency,
            tiers,
            expiry_days,
            batch_size,
        )
        for order_rows, line_rows, transaction_rows in order_batches:
            _insert_batch(connection, Order.__table__, order_rows, checksum)
            _insert_batch(connection, OrderLine.__table__, line_rows, checksum)
            _insert_batch(connection, PointsTransaction.__table__, transaction_rows, checksum)
            inserted_orders += len(order_rows)
            inserted_lines += len(line_rows)
            inserted_transactions += len(transaction_rows)
            expected_points += sum(row["points_change"] for row in transaction_rows)

        actual_counts = {
            model.__tablename__: connection.execute(
                select(func.count()).select_from(model.__table__)
            ).scalar_one()
            for model in targets
        }
        expected_counts = {
            "customer": customer_count,
            "loyaltyaccount": customer_count,
            "order": order_count,
            "orderline": order_count * LINES_PER_ORDER,
            "pointstransaction": transaction_count,
        }
        if actual_counts != expected_counts:
            raise RuntimeError(f"Benchmark count verification failed: {actual_counts}")

        actual_points = connection.execute(
            select(func.coalesce(func.sum(PointsTransaction.points), 0))
        ).scalar_one()
        if actual_points != expected_points:
            raise RuntimeError(
                f"Benchmark points verification failed: expected {expected_points}, got {actual_points}."
            )

    db.session.expire_all()
    return {
        "customers": inserted_customers,
        "orders": inserted_orders,
        "order_lines": inserted_lines,
        "point_transactions": inserted_transactions,
        "total_points_awarded": expected_points,
        "seed": seed,
        "reference_date": reference_date.isoformat(),
        "sha256": checksum.hexdigest(),
    }


def _create_benchmark_database(database_name):
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", database_name):
        raise ValueError("Database name must contain only letters, numbers, and underscores (up to 64 characters).")
    base_url = make_url(app.config["SQLALCHEMY_DATABASE_URI"])
    if base_url.get_backend_name() != "mysql":
        raise RuntimeError("The benchmark generator requires the configured MySQL connection.")

    server_engine = create_engine(base_url.set(database=None))
    try:
        with server_engine.begin() as connection:
            connection.exec_driver_sql(
                f"CREATE DATABASE IF NOT EXISTS `{database_name}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
    finally:
        server_engine.dispose()

    benchmark_app = Flask("loyalty_benchmark")
    benchmark_app.config.update(app.config)
    benchmark_app.config["SQLALCHEMY_DATABASE_URI"] = base_url.set(database=database_name)
    db.init_app(benchmark_app)
    return benchmark_app


def main():
    parser = argparse.ArgumentParser(
        description="Create an isolated, deterministic loyalty benchmark dataset in MySQL."
    )
    parser.add_argument("--database", default="loyalty_benchmark_db")
    parser.add_argument("--customers", type=int, default=20_000)
    parser.add_argument("--orders", type=int, default=100_000)
    parser.add_argument("--transactions", type=int, default=300_000)
    parser.add_argument("--seed", type=int, default=20261112)
    parser.add_argument("--reference-date", type=date.fromisoformat, default=DEFAULT_REFERENCE_DATE)
    args = parser.parse_args()

    benchmark_app = _create_benchmark_database(args.database)
    with benchmark_app.app_context():
        db.create_all()
        seed_default_data()
        summary = generate_benchmark_data(
            customer_count=args.customers,
            order_count=args.orders,
            transaction_count=args.transactions,
            seed=args.seed,
            reference_date=args.reference_date,
        )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
