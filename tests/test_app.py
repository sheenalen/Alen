import shutil
import unittest
import uuid
from datetime import date, timedelta
from pathlib import Path

import app as kanthari_app


class KanthariAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_root = Path(__file__).resolve().parent / ".tmp"
        self.temp_root.mkdir(exist_ok=True)
        self.temp_dir = self.temp_root / str(uuid.uuid4())
        self.temp_dir.mkdir()
        self.original_database_path = kanthari_app.DATABASE_PATH
        kanthari_app.DATABASE_PATH = self.temp_dir / "test.db"
        kanthari_app.app.config.update(TESTING=True)

        with kanthari_app.app.app_context():
            existing_db = kanthari_app.g.pop("db", None)
            if existing_db is not None:
                existing_db.close()
            kanthari_app.create_schema()
            kanthari_app.seed_database()

        self.client = kanthari_app.app.test_client()

    def create_user(
        self,
        *,
        full_name: str = "Test User",
        email: str = "user@example.com",
        password: str = "secret123",
        is_admin: bool = False,
    ) -> int:
        with kanthari_app.app.app_context():
            return kanthari_app.execute_db(
                """
                INSERT INTO users (full_name, email, password_hash, is_admin, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    full_name,
                    email,
                    kanthari_app.hash_password(password),
                    1 if is_admin else 0,
                    date.today().isoformat(),
                ),
            )

    def login_session(self, user_id: int) -> None:
        with self.client.session_transaction() as session_data:
            session_data["user_id"] = user_id

    def first_pickup_slot_id(self) -> int:
        with kanthari_app.app.app_context():
            pickup_slot = kanthari_app.query_db(
                "SELECT * FROM pickup_slots ORDER BY id",
                one=True,
            )
        return pickup_slot["id"]

    def first_variant_for_checkout(self) -> dict:
        with kanthari_app.app.app_context():
            return kanthari_app.query_db(
                """
                SELECT mv.*, mi.name AS item_name
                FROM menu_variants mv
                JOIN menu_items mi ON mi.id = mv.menu_item_id
                WHERE mi.lead_time_days <= 1
                ORDER BY mv.id
                """,
                one=True,
            )

    def set_cart(self, items: list[dict]) -> None:
        with self.client.session_transaction() as session_data:
            session_data["cart"] = items

    def tearDown(self) -> None:
        with kanthari_app.app.app_context():
            existing_db = kanthari_app.g.pop("db", None)
            if existing_db is not None:
                existing_db.close()

        kanthari_app.DATABASE_PATH = self.original_database_path
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_health_endpoint_returns_ok(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"status": "ok", "date": date.today().isoformat()},
        )

    def test_signup_creates_user_and_starts_session(self) -> None:
        response = self.client.post(
            "/signup",
            data={
                "full_name": "Test Customer",
                "email": "customer@example.com",
                "password": "secret123",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/menu"))

        with kanthari_app.app.app_context():
            user = kanthari_app.query_db(
                "SELECT * FROM users WHERE email = ?",
                ("customer@example.com",),
                one=True,
            )

        self.assertIsNotNone(user)
        self.assertEqual(user["full_name"], "Test Customer")

        with self.client.session_transaction() as session_data:
            self.assertEqual(session_data["user_id"], user["id"])

    def test_pickup_checkout_creates_order_and_clears_cart(self) -> None:
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        user_id = self.create_user(
            full_name="Checkout Customer",
            email="checkout@example.com",
        )
        variant = self.first_variant_for_checkout()
        pickup_slot_id = self.first_pickup_slot_id()
        self.login_session(user_id)
        self.set_cart(
            [
                {
                    "menu_item_id": variant["menu_item_id"],
                    "item_name": variant["item_name"],
                    "variant_id": variant["id"],
                    "variant_name": variant["name"],
                    "unit_price": variant["price"],
                    "quantity": 2,
                    "production_date": tomorrow,
                }
            ]
        )

        response = self.client.post(
            "/checkout",
            data={
                "customer_phone": "555-555-5555",
                "fulfillment_type": "pickup",
                "pickup_slot_id": str(pickup_slot_id),
                "payment_method": "cash",
                "notes": "Front porch pickup",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/orders"))

        with kanthari_app.app.app_context():
            order = kanthari_app.query_db(
                "SELECT * FROM orders WHERE user_id = ?",
                (user_id,),
                one=True,
            )
            order_items = kanthari_app.query_db(
                "SELECT * FROM order_items WHERE order_id = ?",
                (order["id"],),
            )

        self.assertIsNotNone(order)
        self.assertEqual(order["fulfillment_type"], "pickup")
        self.assertEqual(order["payment_method"], "cash")
        self.assertEqual(order["status"], "pending")
        self.assertEqual(len(order_items), 1)
        self.assertEqual(order_items[0]["quantity"], 2)

        with self.client.session_transaction() as session_data:
            self.assertEqual(session_data.get("cart"), [])

    def test_orders_requires_login(self) -> None:
        response = self.client.get("/orders", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))

    def test_checkout_requires_login(self) -> None:
        response = self.client.post("/checkout", data={}, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))

    def test_non_admin_is_redirected_from_admin(self) -> None:
        user_id = self.create_user()
        self.login_session(user_id)

        response = self.client.get("/admin", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/"))

    def test_invalid_delivery_zip_does_not_create_order(self) -> None:
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        user_id = self.create_user(email="delivery@example.com")
        variant = self.first_variant_for_checkout()
        self.login_session(user_id)
        self.set_cart(
            [
                {
                    "menu_item_id": variant["menu_item_id"],
                    "item_name": variant["item_name"],
                    "variant_id": variant["id"],
                    "variant_name": variant["name"],
                    "unit_price": variant["price"],
                    "quantity": 1,
                    "production_date": tomorrow,
                }
            ]
        )

        response = self.client.post(
            "/checkout",
            data={
                "customer_phone": "555-555-5555",
                "fulfillment_type": "delivery",
                "delivery_address": "123 Test St",
                "zip_code": "99999",
                "payment_method": "cash",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Delivery is not available for that zip code.", response.data)

        with kanthari_app.app.app_context():
            order = kanthari_app.query_db(
                "SELECT * FROM orders WHERE user_id = ?",
                (user_id,),
                one=True,
            )

        self.assertIsNone(order)

    def test_empty_cart_checkout_redirects_without_creating_order(self) -> None:
        user_id = self.create_user(email="empty@example.com")
        self.login_session(user_id)
        self.set_cart([])

        response = self.client.post(
            "/checkout",
            data={
                "customer_phone": "555-555-5555",
                "fulfillment_type": "pickup",
                "pickup_slot_id": str(self.first_pickup_slot_id()),
                "payment_method": "cash",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/menu"))

        with kanthari_app.app.app_context():
            order = kanthari_app.query_db(
                "SELECT * FROM orders WHERE user_id = ?",
                (user_id,),
                one=True,
            )

        self.assertIsNone(order)

    def test_duplicate_signup_is_rejected(self) -> None:
        self.create_user(full_name="Existing User", email="dupe@example.com")

        response = self.client.post(
            "/signup",
            data={
                "full_name": "Second User",
                "email": "dupe@example.com",
                "password": "secret123",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"An account with that email already exists.", response.data)

        with kanthari_app.app.app_context():
            users = kanthari_app.query_db(
                "SELECT * FROM users WHERE email = ?",
                ("dupe@example.com",),
            )

        self.assertEqual(len(users), 1)

    def test_login_rejects_invalid_password(self) -> None:
        self.create_user(email="login@example.com", password="correct-password")

        response = self.client.post(
            "/login",
            data={
                "email": "login@example.com",
                "password": "wrong-password",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid email or password.", response.data)

        with self.client.session_transaction() as session_data:
            self.assertNotIn("user_id", session_data)

    @unittest.expectedFailure
    def test_stripe_success_must_not_mark_another_users_order_paid(self) -> None:
        owner_id = self.create_user(email="owner@example.com")
        other_user_id = self.create_user(email="other@example.com")

        with kanthari_app.app.app_context():
            order_id = kanthari_app.execute_db(
                """
                INSERT INTO orders (
                    user_id, customer_name, customer_email, customer_phone, fulfillment_type,
                    delivery_address, zip_code, delivery_fee, pickup_slot_id, production_date,
                    payment_method, transaction_reference, subtotal, total_amount, food_cost,
                    gross_profit, status, notes, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_id,
                    "Owner User",
                    "owner@example.com",
                    "555-555-5555",
                    "pickup",
                    None,
                    None,
                    0.0,
                    self.first_pickup_slot_id(),
                    date.today().isoformat(),
                    "credit_card",
                    "cs_test_123",
                    20.0,
                    20.0,
                    8.0,
                    12.0,
                    "pending",
                    "",
                    date.today().isoformat(),
                ),
            )

        self.login_session(other_user_id)
        response = self.client.get(f"/stripe/success/{order_id}", follow_redirects=False)

        self.assertEqual(response.status_code, 403)

        with kanthari_app.app.app_context():
            order = kanthari_app.query_db(
                "SELECT status FROM orders WHERE id = ?",
                (order_id,),
                one=True,
            )

        self.assertEqual(order["status"], "pending")

    @unittest.expectedFailure
    def test_add_to_cart_rejects_variant_from_different_item(self) -> None:
        tomorrow = (date.today() + timedelta(days=1)).isoformat()

        with kanthari_app.app.app_context():
            item = kanthari_app.query_db(
                "SELECT * FROM menu_items ORDER BY id",
                one=True,
            )
            mismatched_variant = kanthari_app.query_db(
                "SELECT * FROM menu_variants WHERE menu_item_id != ? ORDER BY id",
                (item["id"],),
                one=True,
            )

        response = self.client.post(
            "/cart/add",
            data={
                "menu_item_id": str(item["id"]),
                "variant_id": str(mismatched_variant["id"]),
                "quantity": "1",
                "production_date": tomorrow,
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Menu selection not found.", response.data)

        with self.client.session_transaction() as session_data:
            self.assertEqual(session_data.get("cart"), [])

    @unittest.expectedFailure
    def test_checkout_preserves_distinct_production_dates(self) -> None:
        user_id = self.create_user(email="schedule@example.com")
        self.login_session(user_id)

        with kanthari_app.app.app_context():
            variants = kanthari_app.query_db(
                """
                SELECT mv.*, mi.name AS item_name
                FROM menu_variants mv
                JOIN menu_items mi ON mi.id = mv.menu_item_id
                WHERE mi.lead_time_days <= 2
                ORDER BY mv.id
                """,
            )

        self.set_cart(
            [
                {
                    "menu_item_id": variants[0]["menu_item_id"],
                    "item_name": variants[0]["item_name"],
                    "variant_id": variants[0]["id"],
                    "variant_name": variants[0]["name"],
                    "unit_price": variants[0]["price"],
                    "quantity": 1,
                    "production_date": (date.today() + timedelta(days=1)).isoformat(),
                },
                {
                    "menu_item_id": variants[1]["menu_item_id"],
                    "item_name": variants[1]["item_name"],
                    "variant_id": variants[1]["id"],
                    "variant_name": variants[1]["name"],
                    "unit_price": variants[1]["price"],
                    "quantity": 1,
                    "production_date": (date.today() + timedelta(days=2)).isoformat(),
                },
            ]
        )

        response = self.client.post(
            "/checkout",
            data={
                "customer_phone": "555-555-5555",
                "fulfillment_type": "pickup",
                "pickup_slot_id": str(self.first_pickup_slot_id()),
                "payment_method": "cash",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)

        with kanthari_app.app.app_context():
            orders = kanthari_app.query_db(
                "SELECT * FROM orders WHERE user_id = ? ORDER BY id",
                (user_id,),
            )

        self.assertEqual(len(orders), 2)


if __name__ == "__main__":
    unittest.main()
