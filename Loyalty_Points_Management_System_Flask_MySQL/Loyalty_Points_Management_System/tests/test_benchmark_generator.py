import unittest
from datetime import date
from sqlalchemy import func

from app import Customer, Order, OrderLine, PointsTransaction, app, db, seed_default_data
from generate_benchmark_data import generate_benchmark_data


class BenchmarkGeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config.update(
            TESTING=True,
            SECRET_KEY="benchmark-test-secret",
            SQLALCHEMY_DATABASE_URI="sqlite://",
        )

    def setUp(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()
            db.create_all()
            seed_default_data()

    def test_generates_exact_relationship_counts_and_stable_checksum(self):
        with app.app_context():
            result = generate_benchmark_data(
                customer_count=4,
                order_count=8,
                transaction_count=24,
                seed=17,
                reference_date=date(2026, 11, 12),
                batch_size=3,
            )
            db.session.remove()
            self.assertEqual(Customer.query.count(), 4)
            self.assertEqual(Order.query.count(), 8)
            self.assertEqual(OrderLine.query.count(), 24)
            self.assertEqual(PointsTransaction.query.count(), 24)
            self.assertEqual(
                db.session.query(func.sum(PointsTransaction.points)).scalar(),
                result["total_points_awarded"],
            )
            self.assertGreater(result["total_points_awarded"], 0)
            self.assertEqual(result["point_transactions"], 24)

            with self.assertRaises(RuntimeError):
                generate_benchmark_data(
                    customer_count=4,
                    order_count=8,
                    transaction_count=24,
                    seed=17,
                    reference_date=date(2026, 11, 12),
                    batch_size=3,
                )

            first_checksum = result["sha256"]
            db.session.remove()
            db.drop_all()
            db.create_all()
            seed_default_data()
            repeated = generate_benchmark_data(
                customer_count=4,
                order_count=8,
                transaction_count=24,
                seed=17,
                reference_date=date(2026, 11, 12),
                batch_size=3,
            )
            self.assertEqual(repeated["sha256"], first_checksum)

    def test_rejects_transaction_count_not_three_per_order(self):
        with app.app_context():
            with self.assertRaises(ValueError):
                generate_benchmark_data(
                    customer_count=2,
                    order_count=4,
                    transaction_count=4,
                )


if __name__ == "__main__":
    unittest.main()
