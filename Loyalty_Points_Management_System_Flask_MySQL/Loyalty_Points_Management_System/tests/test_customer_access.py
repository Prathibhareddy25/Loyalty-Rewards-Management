import unittest
from decimal import Decimal
from unittest.mock import patch

from app import Customer, LoyaltyAccount, Order, PointsTransaction, Product, Rule, Tier, app, db


class CustomerAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config.update(
            TESTING=True,
            SECRET_KEY="test-secret",
            SQLALCHEMY_DATABASE_URI="sqlite://",
        )
        with app.app_context():
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        with app.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()

    def setUp(self):
        self.client = app.test_client()
        with app.app_context():
            db.session.remove()
            db.drop_all()
            db.create_all()
            tier = Tier(name="Bronze", min_qualifying_spend=0, earning_rate=1)
            db.session.add(tier)
            db.session.add(Rule(
                name="Test rule",
                points_per_currency=1,
                minimum_order_amount=100,
                expiry_days=365,
            ))
            db.session.add(Product(name="Test item", description="", price=120, active=True))
            db.session.commit()

    def _csrf_token(self, path="/login"):
        self.client.get(path)
        with self.client.session_transaction() as browser_session:
            return browser_session["_csrf_token"]

    def _register_customer(self, email):
        token = self._csrf_token("/register")
        return self.client.post("/register", data={
            "csrf_token": token,
            "full_name": "Test Customer",
            "email": email,
            "phone": "555-0100",
            "password": "customer-pass-123",
        })

    def test_customer_can_checkout_and_only_access_own_orders(self):
        self.assertEqual(self._register_customer("one@example.test").status_code, 302)

        with app.app_context():
            customer = Customer.query.filter_by(email="one@example.test").one()
            customer_id = customer.id
            product_id = Product.query.one().id
            other_customer = Customer(
                id="CUS-OTHER",
                full_name="Other Customer",
                email="other@example.test",
            )
            db.session.add(other_customer)
            db.session.flush()
            other_order = Order(
                id="ORD-OTHER",
                customer_id=other_customer.id,
                gross_amount=120,
                discount_amount=0,
                net_amount=120,
                qualifying_spend=120,
                status="COMPLETED",
            )
            db.session.add(other_order)
            db.session.commit()

        token = self._csrf_token("/shop")
        response = self.client.post(f"/cart/add/{product_id}", data={
            "csrf_token": token,
            "quantity": "2",
        })
        self.assertEqual(response.status_code, 302)

        token = self._csrf_token("/cart")
        response = self.client.post("/checkout", data={"csrf_token": token})
        self.assertEqual(response.status_code, 302)

        orders_page = self.client.get("/orders")
        self.assertIn(b"ORD-2026-", orders_page.data)
        self.assertNotIn(b"ORD-OTHER", orders_page.data)
        self.assertEqual(self.client.get("/customers").location, "/shop")

        with app.app_context():
            own_order_code = Order.query.filter_by(customer_id=customer_id).one().id
        token = self._csrf_token("/returns")
        self.client.post("/returns", data={
            "csrf_token": token,
            "order_code": "ORD-OTHER",
            "return_amount": "10",
        })
        self.client.post("/returns", data={
            "csrf_token": token,
            "order_code": own_order_code,
            "return_amount": "10",
        })
        with app.app_context():
            own_order = Order.query.filter_by(customer_id=customer_id).one()
            self.assertEqual(own_order.qualifying_spend, Decimal("240.00"))
            self.assertEqual(own_order.returned_amount, Decimal("10.00"))
            self.assertEqual(Order.query.filter_by(id="ORD-OTHER").one().returned_amount, Decimal("0.00"))
            self.assertEqual(len(own_order.lines), 1)
            self.assertEqual(own_order.lines[0].quantity, 2)
            reversal = PointsTransaction.query.filter_by(
                order_id=own_order.id, transaction_type="REVERSAL"
            ).one()
            self.assertEqual(reversal.points, -10)

    def test_admin_login_can_view_customer_directory(self):
        self._register_customer("customer@example.test")
        with self.client.session_transaction() as browser_session:
            browser_session.clear()
        with patch.dict("os.environ", {
            "ADMIN_EMAIL": "admin@example.test",
            "ADMIN_PASSWORD": "private-admin-password",
        }):
            token = self._csrf_token("/login")
            response = self.client.post("/login", data={
                "csrf_token": token,
                "email": "admin@example.test",
                "password": "private-admin-password",
            })
        self.assertEqual(response.status_code, 302)
        customers_page = self.client.get("/customers")
        self.assertEqual(customers_page.status_code, 200)
        self.assertIn(b"customer@example.test", customers_page.data)
        token = self._csrf_token("/products")
        response = self.client.post("/products", data={
            "csrf_token": token,
            "name": "Admin product",
            "description": "Catalog test",
            "price": "25.50",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/products").status_code, 200)

    def test_registered_customer_can_sign_in_again(self):
        self._register_customer("customer@example.test")
        with app.app_context():
            customer = Customer.query.filter_by(email="customer@example.test").one()
            self.assertNotEqual(customer.password_hash, "customer-pass-123")

        token = self._csrf_token("/shop")
        self.client.post("/logout", data={"csrf_token": token})
        token = self._csrf_token("/login")
        response = self.client.post("/login", data={
            "csrf_token": token,
            "email": "customer@example.test",
            "password": "customer-pass-123",
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/shop"))

    def test_post_without_csrf_token_is_rejected(self):
        response = self.client.post("/login", data={"email": "a@example.test", "password": "bad"})
        self.assertEqual(response.status_code, 400)

    def test_login_before_database_setup_shows_actionable_message(self):
        with app.app_context():
            db.drop_all()
        with patch.dict("os.environ", {"ADMIN_EMAIL": "", "ADMIN_PASSWORD": ""}):
            token = self._csrf_token("/login")
            response = self.client.post("/login", data={
                "csrf_token": token,
                "email": "admin@example.test",
                "password": "incorrect",
            })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"database is not initialized", response.data.lower())
        self.assertIn(b"ADMIN_EMAIL", response.data)

    def test_private_admin_routes_require_admin_session(self):
        response = self.client.get("/products")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/login"))

    def test_uninitialized_admin_is_sent_to_database_setup(self):
        with app.app_context():
            db.drop_all()
        with patch.dict("os.environ", {
            "ADMIN_EMAIL": "admin@example.test",
            "ADMIN_PASSWORD": "private-admin-password",
        }):
            token = self._csrf_token("/login")
            self.client.post("/login", data={
                "csrf_token": token,
                "email": "admin@example.test",
                "password": "private-admin-password",
            })
        self.assertEqual(self.client.get("/").location, "/setup")
        response = self.client.get("/setup", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Dashboard", response.data)


if __name__ == "__main__":
    unittest.main()
