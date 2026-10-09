# Backend

Django + Django REST Framework backend for Empire Global. The project settings live in `config/`, accounts and
authentication in `accounts/`, agents and platform settings in `core/`, and transactions,
payments, notifications, collection bank accounts and application documents in `payments/`, and the
product catalogue in `catalog/`. The server-side audit log lives in `core/`.

## Setup (Windows)

```
cd backend
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser   # your first Super Admin: email, full name, password
python manage.py runserver
```

There is no demo data. On a brand-new database, migrations load the official product catalogue and
Empire Global's collection accounts; everything else (admins, agents, customers, transactions) is
created through the app. Sign in to the admin portal at `/admin/login` with the Super Admin you
created.

You can also create that first Super Admin over HTTP (e.g. from Postman) instead of
`createsuperuser`:

```
POST /api/setup/super-admin/
Content-Type: application/json

{"email": "you@example.com", "fullName": "Your Name", "password": "a strong password"}
```

It answers `201` with the new account, and `409 setup_complete` once any Super Admin exists, so it
can only ever create the first one. No CSRF token or login is needed. Outside development
(`DJANGO_DEBUG=false`) it is off unless `SETUP_TOKEN` is set on the server, and then the request must
send that value in an `X-Setup-Token` header. Limited to 5 attempts an hour.

To empty an existing development database completely: `python manage.py flush` (this also removes
products and bank accounts, which you then add from the admin portal's Products and Bank Accounts pages).

The dev server runs at http://127.0.0.1:8000/. Django's own admin site is at `/django-admin/`
(so it does not clash with the React admin portal's `/admin` routes). Run the tests with
`python manage.py test`.

To use the React app against this backend, run Django on port 8000 and, in the project root,
`npm run dev`. Vite proxies `/api` to Django, so open the app at http://localhost:5173/. To point it at
Django on another port, set `API_PROXY_TARGET` (e.g. `http://127.0.0.1:8001`) and `FRONTEND_URL` to match.

In development (`EMAIL_PROVIDER=console`, the default), emails are not sent. Each one, such as a
sign-up verification code or a password reset link, is printed as plain text in the terminal running
`runserver`, so you can copy the code or link from there:

```
========================================================================
EMAIL (not sent - EMAIL_PROVIDER=console)
To:      ada@example.com
Subject: Your Empire Global verification code
------------------------------------------------------------------------
Hi Ada Obi,

Your Empire Global verification code is 042436.
...
```

### Environment variables

| Variable | Default | Notes |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | insecure dev key | Required outside local development |
| `DJANGO_DEBUG` | `true` | Set `false` in production (also turns on secure cookies) |
| `DJANGO_ALLOWED_HOSTS` | empty | Comma-separated |
| `FRONTEND_URL` | `http://localhost:5173` | Used for reset links and CSRF trusted origin |
| `DEFAULT_FROM_EMAIL` | `Empire Global <no-reply@empireglobal.com>` | Must be on a domain verified in Resend |
| `EMAIL_PROVIDER` | `console` | `console` prints emails; `resend` sends them through Resend |
| `RESEND_API_KEY` | empty | Required when `EMAIL_PROVIDER=resend` |
| `GOOGLE_CLIENT_ID` | empty | Reserved for Google sign-in (not wired up yet) |
| `SETUP_TOKEN` | empty | Required by `POST /api/setup/super-admin/` outside development |
| `ADMIN_TWO_FACTOR` | `false` | `true` makes admins confirm each sign-in with an emailed code |

### Email (Resend)

Verification codes and reset links go through Django's mail API, so switching providers is a
settings change only. To send real email:

1. Verify your sending domain in the Resend dashboard and create an API key.
2. Set `EMAIL_PROVIDER=resend`, `RESEND_API_KEY=re_...` and `DEFAULT_FROM_EMAIL` (an address on
   that domain), then restart Django.

The backend is `accounts.mail.ResendEmailBackend`, which calls Resend's `POST /emails` API. It is
covered by a unit test with the HTTP call mocked but has not yet been run against a live Resend account.

### Google sign-in (planned)

The Google buttons on the login, register and complete-profile pages currently show "not available
yet". The intended flow: the frontend gets an ID token from Google Identity Services, posts it to a
new `/api/auth/google/` endpoint, and the backend verifies it against `GOOGLE_CLIENT_ID`, then either
signs the user in or returns their Google profile so they can add a phone number and agent code.
The frontend hooks are `continueWithGoogle` and `completeGoogleSignup` in `src/context/AuthContext.jsx`.

## Authentication

Auth uses Django's session cookie (HttpOnly) and CSRF protection. The React app should call the API
from the same origin (the Vite dev proxy forwards `/api` to Django), and:

1. call `GET /api/auth/csrf/` once on load to receive the `csrftoken` cookie;
2. send the **current** `csrftoken` cookie value as the `X-CSRFToken` header on every POST/PATCH
   (read it per request: Django rotates it on login);
3. send requests with `credentials: 'include'`.

Accounts have a `role` (`customer`, `admin`, `super_admin`) and a `status` (`pending`, `active`,
`suspended`, `inactive`). Email sign-ups stay `pending` until the emailed 6-digit code is confirmed.

| Method | Endpoint | Body | Notes |
| --- | --- | --- | --- |
| GET | `/api/auth/csrf/` | | Sets the CSRF cookie |
| POST | `/api/auth/register/` | `fullName, email, phone, agentCode?, password` | Emails a code. `201 {email, resendIn, otpLength}` |
| POST | `/api/auth/verify-email/` | `email, code` | Activates the account and signs in. `{user}` |
| POST | `/api/auth/resend-otp/` | `email` | 60s cooldown (`429` with `retryAfter`) |
| POST | `/api/auth/login/` | `email, password` | Customers only. `{user}` |
| POST | `/api/admin/auth/login/` | `email, password` | Admins and super admins only. `202 {twoFactorRequired, ...}` then a code step; see Sign-in protection |
| POST | `/api/auth/logout/` | | `204` |
| GET | `/api/auth/me/` | | Current user, `403` if signed out |
| PATCH | `/api/auth/me/` | `fullName?, phone?, nextOfKin?` | `nextOfKin` is `{fullName, phone, relationship, address}` |
| POST | `/api/auth/change-password/` | `currentPassword, newPassword` | Signs out other sessions. `{user}` |
| POST | `/api/auth/password-reset/` | `email` | Always the same response |
| POST | `/api/auth/password-reset/confirm/` | `uid, token, password` | From the emailed link |

### Admin portal

| Method | Endpoint | Who | Body / notes |
| --- | --- | --- | --- |
| GET | `/api/admin/admins/` | Super Admin | `{admins}` |
| POST | `/api/admin/admins/` | Super Admin | `fullName, email, role (admin, super_admin), password`. Created active, no email verification |
| PATCH | `/api/admin/admins/<id>/` | Super Admin | Any of `fullName, email, role, status (active, inactive), password`. Not your own role or status |
| DELETE | `/api/admin/admins/<id>/` | Super Admin | Not yourself |
| GET | `/api/admin/customers/` | Any admin | `{customers}`, every customer including unverified (`pending`) sign-ups |
| GET | `/api/admin/customers/<id>/` | Any admin | `{customer}` |
| PATCH | `/api/admin/customers/<id>/` | Any admin | `status` only: `active` or `suspended` |
| GET | `/api/admin/agents/` | Any admin | `{agents}` |
| POST | `/api/admin/agents/` | Any admin | `name, phone, location`. The server assigns the next code (`AG-1001`, `AG-1002`, ...) |
| PATCH | `/api/admin/agents/<code>/` | Any admin | Any of `name, phone, location, status (active, inactive)`. Codes never change; agents are deactivated, not deleted |
| GET | `/api/admin/settings/` | Any admin | `{settings}`: `platformName, supportEmail, supportPhone, timezone` |
| PATCH | `/api/admin/settings/` | Super Admin | Any of those fields |
| GET | `/api/settings/` | Anyone | `platformName, supportEmail, supportPhone`, shown on the website and Support page |

Admins edit their own name with `PATCH /api/auth/me/` and their password with
`POST /api/auth/change-password/`, the same endpoints customers use.

An agent code given at sign-up must belong to an active agent, or registration fails with an
`agentCode` field error. The code is optional.

Deactivating an admin, suspending a customer or resetting someone's password signs them out of
existing sessions.

## Transactions and payments (`payments/`)

A customer creates a transaction, pays into the account shown, and uploads the receipt. Admins view
and recommend; only a Super Admin's final approval credits the customer's balance (savings, thrift →
savings; investment → investment; loan → outstanding loan; hire-purchase → no balance). Approval,
the balance credit, the review-trail entry and the customer's notification are saved together or
not at all, and a payment can only be decided once.

| Method | Endpoint | Who | Notes |
| --- | --- | --- | --- |
| GET | `/api/transactions/` | Customer | Their own `{transactions}` |
| POST | `/api/transactions/` | Customer | `productId, amount, termMonths?, application?, nextOfKin?, termsAccepted?` as JSON, or multipart with that JSON in `payload` plus `document.<key>` files for loan / hire-purchase applications. Checked against the live product (limits, plan lengths, terms, required documents); the server sets the reference, dates, `draft` status and `paymentAccount` |
| GET | `/api/transactions/<reference>/` | Customer | Their own only |
| POST | `/api/transactions/<reference>/upload-receipt/` | Customer | Multipart `file` (JPG/PNG/WebP/PDF, 5 MB, contents checked). Moves to `pending`; refused once decided |
| GET | `/api/transactions/<reference>/receipt/` | Owner or admin | Streams the slip |
| GET | `/api/transactions/<reference>/documents/<key>/` | Owner or admin | Streams an application document |
| GET | `/api/notifications/` | Anyone signed in | Latest 100 `{notifications, unreadCount}`; each has `title, message, type, kind, link, read, date` |
| POST | `/api/notifications/<id>/read/`, `/api/notifications/read-all/` | Anyone signed in | Only their own |
| GET | `/api/admin/transactions/` | Any admin | Everyone's, with `slipTrail` |
| GET | `/api/admin/transactions/<reference>/` | Any admin | |
| POST | `/api/admin/transactions/<reference>/view/` | Any admin | Logs a view (once per admin per 10 minutes) |
| POST | `/api/admin/transactions/<reference>/recommend/` | Any admin | `decision (approve, reject), note?` |
| POST | `/api/admin/transactions/<reference>/approve/` | Super Admin | `note?`. Credits the balance |
| POST | `/api/admin/transactions/<reference>/reject/` | Super Admin | `note` is the reason shown to the customer |

Customers never see `slipTrail` (it can hold internal notes). Balances appear on the account
(`savingsBalance`, `investmentBalance`, `outstandingLoan`, `totalBalance`) in `/api/auth/me/` and the
admin customer list; they can't be edited directly, including in Django admin, where transactions
are read-only.

**Receipt files** are saved under `MEDIA_ROOT` (default `backend/media/`, git-ignored) with random
names and are never served as public files, only through the receipt endpoint above. In production,
put `MEDIA_ROOT` on persistent private storage (or a private S3-compatible bucket) and back it up.

**Application documents.** Loan and hire-purchase applications must include the four standard
documents (passport photograph, proof of address, proof of ID, completed application form), any the
product adds (e.g. proof of income), and the guarantor's ID (`guarantorId`). They're uploaded with
the application, checked like receipts, stored privately under `MEDIA_ROOT`, and the transaction and
its files are saved together or not at all.

**Payment account.** When a transaction is created the server records which collection account the
customer must pay into (`paymentAccount`). Later changes to bank accounts don't alter it.

## Withdrawals

A customer withdraws from one plan (an approved transaction), to a bank account they enter on the
request, confirming with their password. An admin recommends, a Super Admin approves (the amount is
taken off the customer's balance then) or rejects, and an admin marks it **paid** after sending the
bank transfer, recording the transfer reference. Pending and approved requests hold their money on the
plan, so two requests can't spend the same naira.

Each product's `withdrawal_rules` (JSON, editable in Django admin) decide what's allowed; the confirmed
rules for the official catalogue are set by `catalog/migrations/0004_default_withdrawal_rules.py`:

| Products | Rules |
| --- | --- |
| One-Year / Two+ Years Lump-Sum Investment | Before maturity: whole plan only, 5% penalty if within 30 days of the start. After maturity: any amount. 5 working days' notice |
| Monthly / Weekly / Daily Savings Investment Plans | Partial withdrawals from month 8; before that whole plan only (5% penalty within 30 days). 5 working days' notice |
| Monthly Collection | Any time; 24 hours' notice |
| Quarterly Collection | Any time; paid at the end of the quarter |
| Accessible Savings Account | Any time, no notice |
| Loans and hire-purchase | Not withdrawable |

Interest isn't tracked, so "interest is forfeited" has no amount to apply. Working days skip weekends
but not public holidays.

| Method | Endpoint | Who | Notes |
| --- | --- | --- | --- |
| GET | `/api/withdrawals/plans/` | Customer | Approved plans with `available`, `partialAllowed`, `earliestPayoutDate`, `reason`, `notes` |
| POST | `/api/withdrawals/quote/` | Customer | `planReference, amount?` → penalty, payout amount, earliest payout date, or `error` |
| GET / POST | `/api/withdrawals/` | Customer | POST `planReference, amount, bankName, accountNumber (10 digits), accountName, note?, password`. 10 an hour |
| POST | `/api/withdrawals/<reference>/cancel/` | Customer | While still under review |
| GET | `/api/admin/withdrawals/`, `/api/admin/withdrawals/<reference>/` | Any admin | With the review `trail` and the plan today |
| POST | `/api/admin/withdrawals/<reference>/recommend/` | Any admin | `decision, note?` |
| POST | `/api/admin/withdrawals/<reference>/approve/`, `.../reject/` | Super Admin | `note` (the reason, for a rejection) |
| POST | `/api/admin/withdrawals/<reference>/mark-paid/` | Any admin | `payoutReference` |

## Emails about money

Customers are emailed when a deposit is confirmed (with the new balance) or not confirmed (with the
reason), and when a withdrawal is requested, approved, rejected and paid (with the bank details and
transfer reference). Emails go out only after the change is saved, and a mail failure never undoes or
blocks it (it's logged; the in-app notification still appears).

## Notifications

The server creates them when things happen; customers and admins each see only their own. The app
checks for new ones every 30 seconds (and when the tab comes back into view), shows the unread count
on the bell, and pops a toast for anything new. Clicking one opens its `link`.

| Event | Who is told |
| --- | --- |
| Customer confirms their email | The customer (welcome) and all active admins (new customer) |
| Loan / hire-purchase application submitted | The customer and all active admins |
| Receipt uploaded (or replaced) | The customer and all active admins |
| Admin recommends approval / rejection | Active Super Admins (except whoever recommended) |
| Super Admin approves / rejects | The customer, and the admins who recommended it |
| Withdrawal requested | The customer and all active admins |
| Withdrawal recommended | Active Super Admins |
| Withdrawal approved | The customer, all active admins ("ready to pay"), and the recommenders |
| Withdrawal rejected / paid | The customer (and the recommenders, on rejection) |

## Product catalogue (`catalog/`)

The official catalogue is loaded by a data migration (`catalog/fixtures/initial_products.json`).
To load it into an existing database (e.g. after `flush`), run `python manage.py load_catalogue`;
products that already exist are left untouched.

| Method | Endpoint | Who | Notes |
| --- | --- | --- | --- |
| GET | `/api/products/` | Anyone | Active products only |
| GET | `/api/admin/products/` | Any admin | Including disabled ones |
| POST | `/api/admin/products/` | Any admin | `name, type, description, minAmount, maxAmount, category?, duration?, frequency?, termOptions?, expectedReturn?, benefits?, clauses?, requirements?, requiredDocuments?, itemCategories?, status?` |
| PATCH | `/api/admin/products/<id>/` | Any admin | Any of those; `{status}` alone enables or disables |
| DELETE | `/api/admin/products/<id>/` | Any admin | Refused with `product_in_use` once customers have transactions on it: disable it instead |

## Collection bank accounts

Empire Global's accounts are loaded by a data migration (`payments/fixtures/initial_bank_accounts.json`).
To load them into an existing database (e.g. after `flush`), run `python manage.py load_bank_accounts`;
accounts and facility mappings that already exist are left untouched.
Each facility (product type, or `default`) maps to one account; a facility without an active
account uses the default, then any active account.

| Method | Endpoint | Who | Notes |
| --- | --- | --- | --- |
| GET | `/api/admin/bank-accounts/` | Any admin | `{accounts, assignments}` |
| POST | `/api/admin/bank-accounts/` | Super Admin | `bankName, accountName, accountNumber (10 digits), notes?` |
| PATCH | `/api/admin/bank-accounts/<id>/` | Super Admin | Any of those, or `status (active, inactive)` |
| DELETE | `/api/admin/bank-accounts/<id>/` | Super Admin | Also removes its facility mappings |
| PUT | `/api/admin/bank-accounts/assignments/<facility>/` | Super Admin | `accountId` (`''` to fall back to the default), `facilityName?` for the audit log |

Only a Super Admin can change where customers pay, since an account change redirects money.

## Audit log

`GET /api/admin/audit-logs/` (any admin) returns the latest 1,000 entries. The server writes them
itself when an admin acts: admin sign-ins and password changes; creating, editing, (de)activating
or deleting admins; suspending customers; agents; settings; products; bank accounts; and every step
of payment review. The browser can't add, change or remove entries.

Errors are `{detail, code}` (plus `errors` for field problems), or DRF's `{field: [messages]}` for
validation errors. Codes the UI should handle: `invalid_credentials`, `email_not_verified`
(includes `email`; the user should go to the verify-email screen), `account_inactive`, `account_locked`
(includes `retryAfter`), `email_taken`, `otp_invalid`, `otp_expired`, `otp_locked`, `otp_cooldown`,
`signup_not_found`, `reset_link_invalid`, `two_factor_expired`, `same_password`.

Emailed codes expire after 10 minutes and lock after 5 wrong attempts. Login, sign-up and code
endpoints are also rate limited per IP.

### Sign-in protection

- **Account lockout.** 5 wrong passwords in a row lock that account for 15 minutes on either portal,
  even if the right password is then entered (`account_locked`). A correct password resets the
  count; resetting the password through the emailed link lifts the lock. Anyone who knows an email
  address can trigger a lock, which is the usual trade-off for stopping password guessing.
- **Two-factor sign-in for admins (off by default).** Admins sign in with email and password. To
  also require a 6-digit emailed code, set `ADMIN_TWO_FACTOR=true` in the environment. Then
  `POST /api/admin/auth/login/` returns `202 {twoFactorRequired, email (masked), resendIn}` instead of
  signing in; finish with `POST /api/admin/auth/verify-code/ {code}` (`POST /api/admin/auth/resend-code/`
  sends a new one, 60s cooldown). The code only works in the browser session that passed the password
  step, within 10 minutes. The admin login page handles both modes.
- **Temporary admin passwords.** Admins created by a Super Admin, or whose password a Super Admin
  resets, get `mustChangePassword: true`. Until they change it (`POST /api/auth/change-password/`,
  which now returns `{user}`), every admin endpoint answers `403`, and the React app sends them to
  `/admin/set-password`. The new password must differ from the temporary one.

### Changing a customer's email

Customers only (an admin's email is changed by a Super Admin):

| Method | Endpoint | Body | Notes |
| --- | --- | --- | --- |
| POST | `/api/auth/change-email/` | `newEmail, password` | Emails a code to the new address and a heads-up to the current one |
| POST | `/api/auth/change-email/resend/` | | 60s cooldown |
| POST | `/api/auth/change-email/confirm/` | `code` | Switches the email, returns `{user}`, and tells the old address |

Nothing changes until the code from the new inbox is confirmed.

### Cleaning up abandoned sign-ups

```
python manage.py purge_pending_signups --dry-run   # list what would go
python manage.py purge_pending_signups             # delete them
python manage.py purge_pending_signups --hours 72  # custom age
```

It removes customer sign-ups that never confirmed their email, were created more than 48 hours ago
(`PENDING_SIGNUP_MAX_AGE_HOURS`) and haven't requested a code since. Run it daily in production, for
example with Windows Task Scheduler or cron.
