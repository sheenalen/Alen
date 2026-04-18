# Kanthari

Kerala catering storefront and admin dashboard built with Python, SQLite, HTML, and Tailwind CSS.

## What is included

- Customer signup and login
- Menu browsing by category
- Item variants, quantity selection, and production-date lead-time enforcement
- Pickup and delivery checkout flow with zip-based delivery zones
- Stored payment methods: cash, PayPal, Venmo, credit card, Zelle
- Stripe-aware credit-card checkout when `STRIPE_SECRET_KEY` and `STRIPE_PUBLISHABLE_KEY` are configured
- Customer order history and notification log
- Admin dashboard for today’s orders, upcoming production, daily prep totals, Costco shopping list, and P&L
- Admin menu management and order-status updates
- Seed data with the Google Form items plus a sample Kerala menu

## Run

```powershell
pip install -r requirements.txt
python app.py
```

Then open `http://127.0.0.1:5000`.

## Test

```powershell
python -m unittest discover -s tests
```

## Seeded admin account

- Email: `admin@kanthari.local`
- Password: `admin123`

## Notes

- The SQLite database is created automatically as `kanthari.db`.
- Credit-card checkout falls back to a local paid-order simulation when Stripe keys are not set.
- The seed data includes the Google Form products captured on April 17, 2026:
  - Kadumanga Pickle
  - Shrimp Pickle
  - Tuna Fish Pickle
