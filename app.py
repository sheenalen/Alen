from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any

from flask import Flask, flash, g, jsonify, redirect, render_template, request, session, url_for

from seeds import DELIVERY_ZONES, MENU_ITEMS, PICKUP_SLOTS

try:
    import stripe
except ImportError:  # pragma: no cover
    stripe = None


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "kanthari.db"
ADMIN_EMAIL = "admin@kanthari.local"
PAYMENT_METHODS = ["cash", "paypal", "venmo", "credit_card", "zelle"]
ORDER_STATUSES = ["pending", "paid", "in_production", "ready", "completed"]
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY")
MENU_MEDIA = {
    item["slug"]: {
        "image_url": item.get("image_url"),
        "image_credit": item.get("image_credit"),
        "image_source": item.get("image_source"),
    }
    for item in MENU_ITEMS
}


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "kanthari-dev-secret")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_: Any) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query_db(query: str, params: tuple[Any, ...] = (), one: bool = False) -> Any:
    cur = get_db().execute(query, params)
    rows = cur.fetchall()
    cur.close()
    return (rows[0] if rows else None) if one else rows


def execute_db(query: str, params: tuple[Any, ...] = ()) -> int:
    db = get_db()
    cur = db.execute(query, params)
    db.commit()
    lastrowid = cur.lastrowid
    cur.close()
    return lastrowid


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def current_user() -> sqlite3.Row | None:
    user_id = session.get("user_id")
    if not user_id:
        return None
    return query_db("SELECT * FROM users WHERE id = ?", (user_id,), one=True)


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if current_user() is None:
            flash("Please sign in to continue.", "error")
            return redirect(url_for("login"))
        return view(**kwargs)

    return wrapped_view


def admin_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        user = current_user()
        if user is None or not user["is_admin"]:
            flash("Admin access required.", "error")
            return redirect(url_for("index"))
        return view(**kwargs)

    return wrapped_view


def cart_items() -> list[dict[str, Any]]:
    return session.setdefault("cart", [])


def active_menu_items() -> list[sqlite3.Row]:
    return query_db(
        """
        SELECT mi.*
        FROM menu_items mi
        WHERE mi.active = 1
        ORDER BY mi.category, mi.name
        """
    )


def grouped_menu() -> dict[str, list[sqlite3.Row]]:
    items = query_db(
        """
        SELECT mi.*, COUNT(v.id) AS variant_count
        FROM menu_items mi
        LEFT JOIN menu_variants v ON v.menu_item_id = mi.id
        WHERE mi.active = 1
        GROUP BY mi.id
        ORDER BY mi.category, mi.name
        """
    )
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for item in items:
        grouped[item["category"]].append(item)
    return grouped


def variants_for_item(item_id: int) -> list[sqlite3.Row]:
    return query_db(
        "SELECT * FROM menu_variants WHERE menu_item_id = ? ORDER BY price",
        (item_id,),
    )


def add_item_to_cart(item: sqlite3.Row, variant: sqlite3.Row, quantity: int, requested_date: date) -> tuple[bool, str]:
    minimum_date = first_available_date(item["lead_time_days"])
    max_date = date.today() + timedelta(days=7)
    if requested_date < minimum_date:
        return False, f"{item['name']} requires {item['lead_time_days']} day(s) lead time. Choose {minimum_date.isoformat()} or later."
    if requested_date > max_date:
        return False, f"Orders are limited to {max_date.isoformat()} or earlier."

    cart = cart_items()
    cart.append(
        {
            "menu_item_id": item["id"],
            "item_name": item["name"],
            "variant_id": variant["id"],
            "variant_name": variant["name"],
            "unit_price": variant["price"],
            "quantity": max(1, quantity),
            "production_date": requested_date.isoformat(),
        }
    )
    session.modified = True
    return True, f"Added {quantity} x {item['name']} ({variant['name']}) for {requested_date.isoformat()}."


def menu_catalog() -> list[dict[str, Any]]:
    catalog = []
    for item in active_menu_items():
        variants = variants_for_item(item["id"])
        catalog.append(
            {
                "id": item["id"],
                "name": item["name"],
                "slug": item["slug"],
                "category": item["category"],
                "description": item["description"],
                "lead_time_days": item["lead_time_days"],
                "variants": [dict(v) for v in variants],
            }
        )
    return catalog


def parse_requested_date(message: str) -> date:
    lowered = message.lower()
    if "tomorrow" in lowered:
        return date.today() + timedelta(days=1)
    if "today" in lowered:
        return date.today()
    iso_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", message)
    if iso_match:
        return date.fromisoformat(iso_match.group(1))
    return date.today() + timedelta(days=1)


def parse_quantity(message: str) -> int:
    match = re.search(r"\b(\d+)\b", message)
    return max(1, int(match.group(1))) if match else 1


def find_item_from_message(message: str, catalog: list[dict[str, Any]]) -> dict[str, Any] | None:
    lowered = message.lower()
    exact = [item for item in catalog if item["name"].lower() in lowered]
    if exact:
        return max(exact, key=lambda item: len(item["name"]))

    token_matches = []
    for item in catalog:
        name_tokens = [token for token in re.split(r"[^a-z0-9]+", item["name"].lower()) if len(token) > 2]
        score = sum(1 for token in name_tokens if token in lowered)
        if score:
            token_matches.append((score, item))
    if token_matches:
        token_matches.sort(key=lambda pair: (pair[0], len(pair[1]["name"])))
        return token_matches[-1][1]
    return None


def find_variant_from_message(message: str, item: dict[str, Any]) -> dict[str, Any]:
    lowered = message.lower()
    for variant in item["variants"]:
        if variant["name"].lower() in lowered:
            return variant

    keywords = {
        "half": "half",
        "full": "full",
        "family": "family",
        "party": "party",
        "veg": "veg",
        "fish": "fish",
        "16 oz": "16 oz",
        "1 liter": "1 liter",
        "2 liter": "2 liter",
        "6 count": "6 count",
        "12 count": "12 count",
        "8 pieces": "8 pieces",
        "16 pieces": "16 pieces",
    }
    for keyword in keywords:
        if keyword in lowered:
            for variant in item["variants"]:
                if keywords[keyword] in variant["name"].lower():
                    return variant
    return item["variants"][0]


def assistant_response(message: str) -> dict[str, Any]:
    catalog = menu_catalog()
    lowered = message.lower().strip()
    if not lowered:
        return {"reply": "Ask me to list menu items or add something to the cart, for example: add 2 pazham pori tomorrow.", "cart_count": sum(item["quantity"] for item in cart_items())}

    if any(phrase in lowered for phrase in ["what do you have", "show menu", "list menu", "menu items"]):
        preview = ", ".join(item["name"] for item in catalog[:6])
        return {"reply": f"Current menu highlights: {preview}. You can also ask for a category like pickles or snacks.", "cart_count": sum(item["quantity"] for item in cart_items())}

    for category in sorted({item["category"] for item in catalog}):
        if category.lower() in lowered:
            items = [item["name"] for item in catalog if item["category"] == category]
            return {"reply": f"{category}: {', '.join(items)}.", "cart_count": sum(item["quantity"] for item in cart_items())}

    if any(phrase in lowered for phrase in ["add", "order", "want", "get me"]):
        item = find_item_from_message(message, catalog)
        if item is None:
            return {"reply": "I couldn't match that to a menu item. Try the exact item name, for example: add 1 chicken biryani half tray tomorrow.", "cart_count": sum(item["quantity"] for item in cart_items())}
        variant = find_variant_from_message(message, item)
        requested_date = parse_requested_date(message)
        quantity = parse_quantity(message)
        item_row = query_db("SELECT * FROM menu_items WHERE id = ?", (item["id"],), one=True)
        variant_row = query_db("SELECT * FROM menu_variants WHERE id = ?", (variant["id"],), one=True)
        success, reply = add_item_to_cart(item_row, variant_row, quantity, requested_date)
        return {"reply": reply, "cart_count": sum(cart_item["quantity"] for cart_item in cart_items()), "success": success}

    matched = find_item_from_message(message, catalog)
    if matched:
        variants = ", ".join(f"{variant['name']} (${variant['price']:.2f})" for variant in matched["variants"])
        return {
            "reply": f"{matched['name']} is in {matched['category']}. Variants: {variants}. Lead time: {matched['lead_time_days']} day(s).",
            "cart_count": sum(item["quantity"] for item in cart_items()),
        }

    return {"reply": "I can help you browse or add items. Try: show pickles, what do you have, or add 2 parotta tomorrow.", "cart_count": sum(item["quantity"] for item in cart_items())}


def first_available_date(lead_time_days: int) -> date:
    return date.today() + timedelta(days=lead_time_days)


def delivery_zone(zip_code: str) -> sqlite3.Row | None:
    return query_db("SELECT * FROM delivery_zones WHERE zip_code = ?", (zip_code,), one=True)


def send_notification(order_id: int, subject: str, body: str) -> None:
    execute_db(
        "INSERT INTO notifications (order_id, subject, body, created_at) VALUES (?, ?, ?, ?)",
        (order_id, subject, body, datetime.now().isoformat()),
    )
    print(f"[notification] order={order_id} subject={subject}\n{body}")


def stripe_enabled() -> bool:
    return bool(stripe and STRIPE_SECRET_KEY and STRIPE_PUBLISHABLE_KEY)


def create_schema() -> None:
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS menu_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL,
            description TEXT NOT NULL,
            lead_time_days INTEGER NOT NULL DEFAULT 1,
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS menu_variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            menu_item_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            sku TEXT NOT NULL,
            price REAL NOT NULL,
            FOREIGN KEY (menu_item_id) REFERENCES menu_items (id)
        );

        CREATE TABLE IF NOT EXISTS ingredients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            menu_item_id INTEGER NOT NULL,
            ingredient_name TEXT NOT NULL,
            FOREIGN KEY (menu_item_id) REFERENCES menu_items (id)
        );

        CREATE TABLE IF NOT EXISTS delivery_zones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zip_code TEXT NOT NULL UNIQUE,
            city TEXT NOT NULL,
            fee REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pickup_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            address TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            customer_name TEXT NOT NULL,
            customer_email TEXT NOT NULL,
            customer_phone TEXT NOT NULL,
            fulfillment_type TEXT NOT NULL,
            delivery_address TEXT,
            zip_code TEXT,
            delivery_fee REAL NOT NULL DEFAULT 0,
            pickup_slot_id INTEGER,
            production_date TEXT NOT NULL,
            payment_method TEXT NOT NULL,
            transaction_reference TEXT NOT NULL,
            subtotal REAL NOT NULL,
            total_amount REAL NOT NULL,
            food_cost REAL NOT NULL,
            gross_profit REAL NOT NULL,
            status TEXT NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (pickup_slot_id) REFERENCES pickup_slots (id)
        );

        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            menu_item_id INTEGER NOT NULL,
            variant_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            variant_name TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            unit_price REAL NOT NULL,
            line_total REAL NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders (id),
            FOREIGN KEY (menu_item_id) REFERENCES menu_items (id),
            FOREIGN KEY (variant_id) REFERENCES menu_variants (id)
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders (id)
        );
        """
    )
    db.commit()


def seed_database() -> None:
    if query_db("SELECT COUNT(*) AS count FROM users", one=True)["count"] == 0:
        execute_db(
            """
            INSERT INTO users (full_name, email, password_hash, is_admin, created_at)
            VALUES (?, ?, ?, 1, ?)
            """,
            ("Kanthari Admin", ADMIN_EMAIL, hash_password("admin123"), datetime.now().isoformat()),
        )

    if query_db("SELECT COUNT(*) AS count FROM delivery_zones", one=True)["count"] == 0:
        for zone in DELIVERY_ZONES:
            execute_db(
                "INSERT INTO delivery_zones (zip_code, city, fee) VALUES (?, ?, ?)",
                (zone["zip_code"], zone["city"], zone["fee"]),
            )

    if query_db("SELECT COUNT(*) AS count FROM pickup_slots", one=True)["count"] == 0:
        for slot in PICKUP_SLOTS:
            execute_db(
                "INSERT INTO pickup_slots (label, address) VALUES (?, ?)",
                (slot["label"], slot["address"]),
            )

    if query_db("SELECT COUNT(*) AS count FROM menu_items", one=True)["count"] == 0:
        for item in MENU_ITEMS:
            item_id = execute_db(
                """
                INSERT INTO menu_items (name, slug, category, description, lead_time_days, active)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    item["name"],
                    item["slug"],
                    item["category"],
                    item["description"],
                    item["lead_time_days"],
                    item["active"],
                ),
            )
            for ingredient in item["ingredients"]:
                execute_db(
                    "INSERT INTO ingredients (menu_item_id, ingredient_name) VALUES (?, ?)",
                    (item_id, ingredient),
                )
            for variant in item["variants"]:
                execute_db(
                    """
                    INSERT INTO menu_variants (menu_item_id, name, sku, price)
                    VALUES (?, ?, ?, ?)
                    """,
                    (item_id, variant["name"], variant["sku"], variant["price"]),
                )


@app.context_processor
def inject_globals() -> dict[str, Any]:
    cart = cart_items()
    return {
        "current_user": current_user(),
        "cart_count": sum(item["quantity"] for item in cart),
        "cart_subtotal": sum(item["quantity"] * item["unit_price"] for item in cart),
        "today": date.today().isoformat(),
        "default_order_date": (date.today() + timedelta(days=1)).isoformat(),
        "max_order_date": (date.today() + timedelta(days=7)).isoformat(),
        "menu_media": MENU_MEDIA,
        "stripe_enabled": stripe_enabled(),
    }


@app.route("/")
def index():
    featured = query_db(
        """
        SELECT mi.*, mv.name AS variant_name, mv.price
        FROM menu_items mi
        JOIN menu_variants mv ON mv.menu_item_id = mi.id
        WHERE mi.active = 1
        ORDER BY mi.category, mi.name, mv.price
        """
    )
    summary = query_db(
        """
        SELECT
            COUNT(DISTINCT mi.id) AS active_items,
            COUNT(DISTINCT mi.category) AS category_count,
            MIN(mv.price) AS starting_price
        FROM menu_items mi
        JOIN menu_variants mv ON mv.menu_item_id = mi.id
        WHERE mi.active = 1
        """,
        one=True,
    )
    slots = query_db("SELECT * FROM pickup_slots ORDER BY id")
    return render_template("index.html", featured=featured, pickup_slots=slots, summary=summary)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        full_name = request.form["full_name"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        if not full_name or not email or not password:
            flash("All fields are required.", "error")
            return redirect(url_for("signup"))
        existing = query_db("SELECT id FROM users WHERE email = ?", (email,), one=True)
        if existing:
            flash("An account with that email already exists.", "error")
            return redirect(url_for("signup"))
        user_id = execute_db(
            """
            INSERT INTO users (full_name, email, password_hash, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (full_name, email, hash_password(password), datetime.now().isoformat()),
        )
        session["user_id"] = user_id
        flash("Account created.", "success")
        return redirect(url_for("menu"))
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        user = query_db(
            "SELECT * FROM users WHERE email = ? AND password_hash = ?",
            (email, hash_password(password)),
            one=True,
        )
        if user is None:
            flash("Invalid email or password.", "error")
            return redirect(url_for("login"))
        session["user_id"] = user["id"]
        flash("Signed in.", "success")
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("index"))


@app.route("/menu")
def menu():
    items = grouped_menu()
    item_variants = {
        item["id"]: variants_for_item(item["id"])
        for category_items in items.values()
        for item in category_items
    }
    return render_template("menu.html", grouped_items=items, item_variants=item_variants)


@app.route("/assistant/chat", methods=["POST"])
def chat_assistant():
    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message", "")).strip()
    response = assistant_response(message)
    return jsonify(response)


@app.route("/cart/add", methods=["POST"])
def add_to_cart():
    item = query_db("SELECT * FROM menu_items WHERE id = ?", (request.form["menu_item_id"],), one=True)
    variant = query_db("SELECT * FROM menu_variants WHERE id = ?", (request.form["variant_id"],), one=True)
    if item is None or variant is None:
        flash("Menu selection not found.", "error")
        return redirect(url_for("menu"))

    requested_date = date.fromisoformat(request.form["production_date"])
    quantity = max(1, int(request.form["quantity"]))
    success, message = add_item_to_cart(item, variant, quantity, requested_date)
    flash(message, "success" if success else "error")
    return redirect(url_for("menu"))


@app.route("/cart", methods=["GET", "POST"])
def cart():
    if request.method == "POST":
        if "remove_index" in request.form:
            index = int(request.form["remove_index"])
            cart = cart_items()
            if 0 <= index < len(cart):
                cart.pop(index)
                session.modified = True
                flash("Item removed.", "success")
        return redirect(url_for("cart"))

    subtotal = sum(item["quantity"] * item["unit_price"] for item in cart_items())
    return render_template(
        "cart.html",
        cart=cart_items(),
        subtotal=subtotal,
        pickup_slots=query_db("SELECT * FROM pickup_slots ORDER BY id"),
        payment_methods=PAYMENT_METHODS,
    )


@app.route("/checkout", methods=["POST"])
@login_required
def checkout():
    cart = cart_items()
    if not cart:
        flash("Your cart is empty.", "error")
        return redirect(url_for("menu"))

    fulfillment_type = request.form["fulfillment_type"]
    payment_method = request.form["payment_method"]
    notes = request.form.get("notes", "").strip()
    production_date = max(item["production_date"] for item in cart)
    subtotal = sum(item["quantity"] * item["unit_price"] for item in cart)
    delivery_fee = 0.0
    delivery_address = None
    zip_code = None
    pickup_slot_id = None

    if fulfillment_type == "delivery":
        delivery_address = request.form["delivery_address"].strip()
        zip_code = request.form["zip_code"].strip()
        zone = delivery_zone(zip_code)
        if zone is None:
            flash("Delivery is not available for that zip code.", "error")
            return redirect(url_for("cart"))
        delivery_fee = zone["fee"]
    else:
        pickup_slot_id = int(request.form["pickup_slot_id"])

    total = subtotal + delivery_fee
    food_cost = round(total * 0.40, 2)
    gross_profit = round(total - food_cost, 2)
    user = current_user()
    transaction_reference = f"{payment_method[:3].upper()}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    status = "pending"

    order_id = execute_db(
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
            user["id"],
            user["full_name"],
            user["email"],
            request.form["customer_phone"].strip(),
            fulfillment_type,
            delivery_address,
            zip_code,
            delivery_fee,
            pickup_slot_id,
            production_date,
            payment_method,
            transaction_reference,
            subtotal,
            total,
            food_cost,
            gross_profit,
            status,
            notes,
            datetime.now().isoformat(),
        ),
    )

    for item in cart:
        execute_db(
            """
            INSERT INTO order_items (
                order_id, menu_item_id, variant_id, item_name, variant_name,
                quantity, unit_price, line_total
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                item["menu_item_id"],
                item["variant_id"],
                item["item_name"],
                item["variant_name"],
                item["quantity"],
                item["unit_price"],
                item["quantity"] * item["unit_price"],
            ),
        )

    if payment_method == "credit_card" and stripe_enabled():
        stripe.api_key = STRIPE_SECRET_KEY
        checkout_session = stripe.checkout.Session.create(
            mode="payment",
            success_url=url_for("stripe_success", order_id=order_id, _external=True),
            cancel_url=url_for("orders", _external=True),
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {"name": item["item_name"], "description": item["variant_name"]},
                        "unit_amount": int(round(item["unit_price"] * 100)),
                    },
                    "quantity": item["quantity"],
                }
                for item in cart
            ]
            + (
                [
                    {
                        "price_data": {
                            "currency": "usd",
                            "product_data": {"name": "Delivery fee"},
                            "unit_amount": int(round(delivery_fee * 100)),
                        },
                        "quantity": 1,
                    }
                ]
                if delivery_fee
                else []
            ),
            customer_email=user["email"],
            metadata={"order_id": str(order_id)},
        )
        execute_db(
            "UPDATE orders SET transaction_reference = ? WHERE id = ?",
            (checkout_session.id, order_id),
        )
        session["cart"] = []
        return redirect(checkout_session.url, code=303)

    if payment_method == "credit_card":
        status = "paid"
        execute_db("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))

    send_notification(
        order_id,
        "Order confirmation",
        f"Your order #{order_id} has been placed. Payment method: {payment_method}. Total: ${total:.2f}.",
    )
    session["cart"] = []
    flash(f"Order #{order_id} placed successfully.", "success")
    return redirect(url_for("orders"))


@app.route("/stripe/success/<int:order_id>")
@login_required
def stripe_success(order_id: int):
    execute_db("UPDATE orders SET status = 'paid' WHERE id = ?", (order_id,))
    order = query_db("SELECT total_amount FROM orders WHERE id = ?", (order_id,), one=True)
    send_notification(
        order_id,
        "Stripe payment received",
        f"Order #{order_id} has been paid successfully. Total: ${order['total_amount']:.2f}.",
    )
    flash(f"Stripe payment recorded for order #{order_id}.", "success")
    return redirect(url_for("orders"))


@app.route("/orders")
@login_required
def orders():
    user = current_user()
    orders_rows = query_db(
        """
        SELECT o.*, ps.label AS pickup_label
        FROM orders o
        LEFT JOIN pickup_slots ps ON ps.id = o.pickup_slot_id
        WHERE o.user_id = ?
        ORDER BY o.created_at DESC
        """,
        (user["id"],),
    )
    order_details = {
        order["id"]: query_db(
            "SELECT * FROM order_items WHERE order_id = ? ORDER BY id",
            (order["id"],),
        )
        for order in orders_rows
    }
    notifications = {
        order["id"]: query_db(
            "SELECT * FROM notifications WHERE order_id = ? ORDER BY created_at DESC",
            (order["id"],),
        )
        for order in orders_rows
    }
    return render_template(
        "orders.html",
        orders=orders_rows,
        order_details=order_details,
        notifications=notifications,
    )


@app.route("/admin")
@admin_required
def admin_dashboard():
    today_iso = date.today().isoformat()
    week_end = (date.today() + timedelta(days=7)).isoformat()
    todays_orders = query_db(
        """
        SELECT o.*, ps.label AS pickup_label
        FROM orders o
        LEFT JOIN pickup_slots ps ON ps.id = o.pickup_slot_id
        WHERE o.production_date = ?
        ORDER BY o.created_at DESC
        """,
        (today_iso,),
    )
    upcoming_orders = query_db(
        """
        SELECT o.*, ps.label AS pickup_label
        FROM orders o
        LEFT JOIN pickup_slots ps ON ps.id = o.pickup_slot_id
        WHERE o.production_date BETWEEN ? AND ?
        ORDER BY o.production_date, o.created_at
        """,
        (today_iso, week_end),
    )
    production_rollup = query_db(
        """
        SELECT o.production_date, oi.item_name, oi.variant_name, SUM(oi.quantity) AS total_quantity
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        GROUP BY o.production_date, oi.item_name, oi.variant_name
        ORDER BY o.production_date, oi.item_name
        """
    )
    ingredient_rollup = query_db(
        """
        SELECT i.ingredient_name, SUM(oi.quantity) AS total_quantity
        FROM order_items oi
        JOIN ingredients i ON i.menu_item_id = oi.menu_item_id
        GROUP BY i.ingredient_name
        ORDER BY total_quantity DESC, i.ingredient_name
        """
    )
    pnl = query_db(
        """
        SELECT production_date, COUNT(*) AS order_count, SUM(total_amount) AS revenue,
               SUM(food_cost) AS food_cost, SUM(gross_profit) AS gross_profit
        FROM orders
        GROUP BY production_date
        ORDER BY production_date DESC
        """
    )
    return render_template(
        "admin_dashboard.html",
        todays_orders=todays_orders,
        upcoming_orders=upcoming_orders,
        production_rollup=production_rollup,
        ingredient_rollup=ingredient_rollup,
        pnl=pnl,
    )


@app.route("/admin/orders/<int:order_id>", methods=["GET", "POST"])
@admin_required
def admin_order_detail(order_id: int):
    order = query_db(
        """
        SELECT o.*, ps.label AS pickup_label, ps.address AS pickup_address
        FROM orders o
        LEFT JOIN pickup_slots ps ON ps.id = o.pickup_slot_id
        WHERE o.id = ?
        """,
        (order_id,),
        one=True,
    )
    if order is None:
        flash("Order not found.", "error")
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        new_status = request.form["status"]
        if new_status in ORDER_STATUSES:
            execute_db("UPDATE orders SET status = ? WHERE id = ?", (new_status, order_id))
            send_notification(
                order_id,
                f"Order status: {new_status}",
                f"Order #{order_id} is now marked as {new_status}.",
            )
            flash("Order updated.", "success")
        return redirect(url_for("admin_order_detail", order_id=order_id))

    items = query_db("SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order_id,))
    notifications = query_db(
        "SELECT * FROM notifications WHERE order_id = ? ORDER BY created_at DESC",
        (order_id,),
    )
    return render_template(
        "admin_order.html",
        order=order,
        items=items,
        notifications=notifications,
        statuses=ORDER_STATUSES,
    )


@app.route("/admin/menu", methods=["GET", "POST"])
@admin_required
def admin_menu():
    if request.method == "POST":
        if request.form.get("action") == "toggle":
            execute_db(
                "UPDATE menu_items SET active = CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id = ?",
                (request.form["menu_item_id"],),
            )
            flash("Menu item updated.", "success")
        else:
            item_id = execute_db(
                """
                INSERT INTO menu_items (name, slug, category, description, lead_time_days, active)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                (
                    request.form["name"].strip(),
                    request.form["slug"].strip(),
                    request.form["category"].strip(),
                    request.form["description"].strip(),
                    int(request.form["lead_time_days"]),
                ),
            )
            execute_db(
                """
                INSERT INTO menu_variants (menu_item_id, name, sku, price)
                VALUES (?, ?, ?, ?)
                """,
                (
                    item_id,
                    request.form["variant_name"].strip(),
                    request.form["sku"].strip(),
                    float(request.form["price"]),
                ),
            )
            for ingredient in [part.strip() for part in request.form["ingredients"].split(",") if part.strip()]:
                execute_db(
                    "INSERT INTO ingredients (menu_item_id, ingredient_name) VALUES (?, ?)",
                    (item_id, ingredient),
                )
            flash("Menu item created.", "success")
        return redirect(url_for("admin_menu"))

    items = query_db("SELECT * FROM menu_items ORDER BY category, name")
    variants = {
        item["id"]: variants_for_item(item["id"])
        for item in items
    }
    return render_template("admin_menu.html", items=items, variants=variants)


@app.route("/zone-check")
def zone_check():
    zip_code = request.args.get("zip_code", "").strip()
    zone = delivery_zone(zip_code) if zip_code else None
    return render_template("zone_check.html", zip_code=zip_code, zone=zone)


@app.route("/health")
def health():
    return {"status": "ok", "date": date.today().isoformat()}


with app.app_context():
    create_schema()
    seed_database()


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true", "yes", "on"}
    app.run(host=host, port=port, debug=debug)
