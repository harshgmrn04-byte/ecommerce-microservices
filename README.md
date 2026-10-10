# E-Commerce Microservices

Hackathon: see the [deliverable audit and resilience report](docs/resilience-report.md)
and [submission/demo guide](docs/hackathon-submission.md). The local evidence
dashboard runs at http://localhost:8070 with `python3 -m chaos.dashboard`.
The adaptive planner, fresh baseline capture and seven live safety cases are
reproducible with the commands in the guide.

![CI/CD](https://img.shields.io/badge/CI/CD-GitHub%20Actions-blue)
![Version](https://img.shields.io/badge/version-1.0.0-green)
![License](https://img.shields.io/badge/license-MIT-orange)

A modern, scalable e-commerce platform built with microservices architecture. Each service is independently deployable, has its own database, and communicates via REST APIs and message queuing.

---

## Architecture Overview

```mermaid
graph TB
    subgraph "Frontend"
        NEXT[Next.js App<br/>Port 3000]
    end

    subgraph "Application Services"
        AUTH[Auth Service<br/>Port 3001]
        PROD[Product Service<br/>Port 3002]
        ORDER[Order Service<br/>Port 3003]
        NOTIF[Notification Service]
    end

    subgraph "Data Stores"
        PG[(PostgreSQL<br/>Per-service DBs)]
        MONGO[(MongoDB<br/>Products / Notifications)]
        REDIS[(Redis<br/>Cache Layer)]
    end

    subgraph "Infrastructure"
        RABBIT[RabbitMQ<br/>Message Broker]
        STRIPE[Stripe<br/>Payment Gateway]
        MAIL[MailHog<br/>Email Preview]
        CLOUD[Cloudinary<br/>Image Uploads]
    end

    NEXT --> AUTH
    NEXT --> PROD
    NEXT --> ORDER

    AUTH --> PG
    PROD --> PG
    PROD --> MONGO
    PROD --> REDIS
    PROD --> CLOUD
    ORDER --> PG
    ORDER --> STRIPE
    ORDER --> RABBIT
    RABBIT --> NOTIF
    NOTIF --> MONGO
    NOTIF --> MAIL
```

---

## Service Interaction Flow

```mermaid
sequenceDiagram
    participant U as Browser
    participant F as Frontend (Next.js)
    participant A as Auth Service
    participant P as Product Service
    participant O as Order Service
    participant N as Notification Service
    participant R as RabbitMQ

    U->>F: Browse products
    F->>P: GET /products
    P-->>F: Product list
    F-->>U: Render catalog

    U->>F: Register / Login
    F->>A: POST /register
    A-->>F: JWT tokens
    F->>U: Store tokens

    U->>F: Add to cart
    F-->>U: Update cart (localStorage)

    U->>F: Checkout
    F->>O: POST /orders (JWT)
    O->>O: Create order (PostgreSQL)
    O->>O: Process payment (Stripe)
    O->>R: Publish order.created
    O-->>F: Order created
    R->>N: Consume order.created
    N->>N: Send email notification
    F-->>U: Redirect to order page
```

---

## Database Design

Each service has its own isolated database (separate PostgreSQL databases and/or MongoDB collections).

```mermaid
erDiagram
    User ||--o{ Order : places
    User {
        uuid id PK
        string name
        string email UK
        string password
        enum role
        string avatar
        string phone
        string address
        datetime createdAt
        datetime updatedAt
        string refreshToken
        string resetToken
        datetime resetTokenExp
    }

    Category ||--o{ Product : contains
    Category {
        uuid id PK
        string name UK
        string slug UK
    }

    Product {
        uuid id PK
        string name
        string slug UK
        string description
        float price
        float comparePrice
        string[] images
        int stock
        bool featured
        uuid categoryId FK
        datetime createdAt
        datetime updatedAt
    }

    Order ||--o{ OrderItem : includes
    Order {
        uuid id PK
        string userId
        float total
        float subtotal
        float shipping
        float discount
        string status
        string paymentIntentId
        json shippingAddress
        string trackingNumber
        datetime estimatedDelivery
        datetime paidAt
        datetime createdAt
        datetime updatedAt
    }

    OrderItem {
        uuid id PK
        uuid orderId FK
        string productId
        string name
        float price
        int quantity
        string image
    }
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Frontend** | Next.js 14, TypeScript, TailwindCSS, shadcn/ui |
| **Auth** | Node.js, Express, JWT, bcrypt, Prisma |
| **Products** | Node.js, Express, Prisma, Mongoose, Redis |
| **Orders** | Node.js, Express, Prisma, Stripe, RabbitMQ |
| **Notifications** | Node.js, RabbitMQ, MongoDB, Resend/MailHog |
| **Databases** | PostgreSQL 16, MongoDB 7, Redis 7 |
| **Message Broker** | RabbitMQ 3 (Management) |
| **Containerization** | Docker & Docker Compose |
| **Email Dev** | MailHog |
| **CI/CD** | GitHub Actions |
| **Image Upload** | Cloudinary |

---

## Getting Started

### Prerequisites

- Node.js 20+
- Docker & Docker Compose
- npm or yarn

### Quick Start (Docker)

```bash
# 1. Clone and enter the project
git clone https://github.com/harshgmrn04-byte/ecommerce-microservices.git
cd ecommerce-microservices

# 2. Start the application and monitoring (13 containers)
docker compose up --build -d

# 3. The startup scripts automatically seed the local admin and product catalog.
# Default local admin: admin@test.com / 123456

# 4. Access the platform
open http://localhost:3000
```

Each service has its own `.dockerignore` because Compose builds from the service
directory. These exclusions keep host `node_modules`, generated output, and local
environment files out of the Linux images. Copying Mac dependencies into a
container can make native modules such as `bcrypt` fail with `Exec format error`.
If authentication fails to start, inspect `docker compose logs auth-service`,
then rebuild with `docker compose up -d --build --wait --wait-timeout 240`.

### Manual Start (without Docker)

Start infrastructure services first (PostgreSQL, MongoDB, Redis, RabbitMQ), then run each service:

```bash
# Auth Service
cd auth-service && npm install && cp .env.example .env && npm run dev

# Product Service
cd product-service && npm install && cp .env.example .env && npm run dev

# Order Service
cd order-service && npm install && cp .env.example .env && npm run dev

# Notification Service
cd notification-service && npm install && cp .env.example .env && npm run dev

# Frontend
cd frontend && npm install && cp .env.example .env && npm run dev
```

---

## Services & Ports

| Service | Container | Port | Host Port | Dependencies |
|---------|-----------|------|-----------|--------------|
| Frontend | `ecommerce-frontend` | 3000 | 3000 | Auth, Products, Orders |
| Auth API | `ecommerce-auth` | 3001 | 3004 | PostgreSQL |
| Products API | `ecommerce-products` | 3002 | 3002 | PostgreSQL, MongoDB, Redis |
| Orders API | `ecommerce-orders` | 3003 | 3003 | PostgreSQL, RabbitMQ |
| Notifications | `ecommerce-notifications` | — | — | RabbitMQ, MongoDB |
| PostgreSQL | `ecommerce-postgres` | 5432 | 5433 | — |
| MongoDB | `ecommerce-mongodb` | 27017 | 27017 | — |
| Redis | `ecommerce-redis` | 6379 | 6380 | — |
| RabbitMQ | `ecommerce-rabbitmq` | 5672 / 15672 | 5672 / 15672 | — |
| MailHog | `ecommerce-mailhog` | 1025 / 8025 | 1025 / 8025 | — |

---

## Environment Variables

### Auth Service
| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://ecommerce:ecommerce123@postgres:5432/ecommerce_auth` | PostgreSQL connection |
| `JWT_SECRET` | `super-secret-jwt-key-change-in-production` | JWT signing key |
| `JWT_REFRESH_SECRET` | `super-secret-refresh-key-change-in-production` | Refresh token key |
| `JWT_EXPIRES_IN` | `15m` | Access token TTL |
| `JWT_REFRESH_EXPIRES_IN` | `7d` | Refresh token TTL |
| `ADMIN_EMAIL` | `admin@test.com` | Idempotently seeded local administrator |
| `ADMIN_PASSWORD` | `123456` | Local administrator password; override outside the sandbox |
| `PORT` | `3001` | Service port |
| `FRONTEND_URL` | `http://localhost:3000` | CORS origin |

### Product Service
| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://.../ecommerce_products` | PostgreSQL for categories/products |
| `MONGODB_URI` | `mongodb://ecommerce:ecommerce123@mongodb:27017/ecommerce` | MongoDB for reviews/search |
| `REDIS_URL` | `redis://redis:6379` | Cache |
| `CLOUDINARY_CLOUD_NAME` | — | Cloudinary name |
| `CLOUDINARY_API_KEY` | — | Cloudinary API key |
| `CLOUDINARY_API_SECRET` | — | Cloudinary secret |
| `JWT_SECRET` | `super-secret-jwt-key-change-in-production` | Token verification |
| `PORT` | `3002` | Service port |
| `FRONTEND_URL` | `http://localhost:3000` | CORS origin |

### Order Service
| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://.../ecommerce_orders` | PostgreSQL for orders/items |
| `STRIPE_SECRET_KEY` | `sk_test_placeholder` | Stripe API key |
| `STRIPE_WEBHOOK_SECRET` | `whsec_placeholder` | Stripe webhook secret |
| `RABBITMQ_URL` | `amqp://ecommerce:ecommerce123@rabbitmq:5672` | Message broker |
| `JWT_SECRET` | `super-secret-jwt-key-change-in-production` | Token verification |
| `PORT` | `3003` | Service port |
| `FRONTEND_URL` | `http://localhost:3000` | CORS origin |

### Notification Service
| Variable | Default | Description |
|----------|---------|-------------|
| `RABBITMQ_URL` | `amqp://ecommerce:ecommerce123@rabbitmq:5672` | Message broker |
| `MONGODB_URI` | `mongodb://ecommerce:ecommerce123@mongodb:27017/ecommerce` | Notification storage |
| `FROM_EMAIL` | `noreply@ecommerce.com` | Sender address |
| `SMTP_HOST` | `mailhog` | Local SMTP server |
| `SMTP_PORT` | `1025` | Local SMTP port |
| `HEALTH_PORT` | `3004` | Internal notification health server |

---

## API Endpoints

### Auth Service (Port 3004 mapped to 3001)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/register` | — | Create account |
| `POST` | `/login` | — | Login (returns JWT) |
| `POST` | `/refresh-token` | — | Refresh access token |
| `GET` | `/me` | Bearer | Get current user |
| `PUT` | `/profile` | Bearer | Update profile |
| `POST` | `/forgot-password` | — | Request reset link |
| `POST` | `/reset-password` | — | Reset with token |

### Product Service (Port 3002)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/products` | — | List products (pagination, search, category filter) |
| `GET` | `/products/:id` | — | Product details |
| `POST` | `/products` | Admin | Create product |
| `PUT` | `/products/:id` | Admin | Update product |
| `DELETE` | `/products/:id` | Admin | Delete product |
| `POST` | `/products/:id/reviews` | Bearer | Add review |
| `GET` | `/categories` | — | List categories |
| `POST` | `/categories` | Admin | Create category |

### Order Service (Port 3003)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/orders` | Bearer | Create order (checkout) |
| `GET` | `/orders` | Bearer | List user orders |
| `GET` | `/orders/:id` | Bearer | Order details |
| `PUT` | `/orders/:id/status` | Admin | Update status |
| `GET` | `/orders/:id/tracking` | Bearer | Tracking info |
| `POST` | `/webhook/stripe` | — | Stripe webhook |

---

## Frontend Routes

| Path | Description |
|------|-------------|
| `/` | Homepage (search, categories, featured products) |
| `/products` | Full product catalog with filters |
| `/products/[id]` | Product detail page |
| `/cart` | Shopping cart |
| `/checkout` | Checkout (shipping address form) |
| `/auth/login` | Login page |
| `/auth/register` | Registration page |
| `/account` | User profile |
| `/account/orders` | Order history |
| `/account/orders/[id]` | Single order detail |
| `/admin` | Admin dashboard (CRUD products/categories) |

---

## Admin Panel

Access at `http://localhost:3000/admin` after logging in with an admin account.

**Default admin credentials:**
```
Email:    admin@test.com
Password: 123456
```

The admin panel allows:
- Create, edit, and delete products
- Manage categories
- View all orders
- Update order status

---

## Useful Tools

| Tool | URL | Credentials |
|------|-----|-------------|
| MailHog | `http://localhost:8025` | — |
| Prometheus | `http://localhost:9090` | — |
| Grafana | `http://localhost:3005` | `admin` / `admin` |
| cAdvisor | `http://localhost:8080` | — |
| RabbitMQ Management | `http://localhost:15672` | `ecommerce` / `ecommerce123` |
| Frontend | `http://localhost:3000` | — |
| Auth API | `http://localhost:3004` | — |
| Products API | `http://localhost:3002` | — |
| Orders API | `http://localhost:3003` | — |

Application readiness endpoints are available at `/health/ready` on the Auth,
Product, and Order APIs. The notification service exposes the same endpoint on
its internal container port `3004`. Docker Compose uses these checks before
starting the frontend.

### Monitoring baseline

Prometheus scrapes application, RabbitMQ, and container metrics every five
seconds. Grafana automatically provisions the **E-Commerce Resilience
Baseline** dashboard. To reproduce the normal-load baseline:

```bash
docker compose --profile tools run --rm k6
```

The recorded baseline, raw results, and derived safety thresholds are
documented in [`docs/monitoring-baseline.md`](docs/monitoring-baseline.md).
Use `python3 -m observability.capture_baseline` to refresh the admission artifact
with measured service request counts, current health and restart deltas.

### Controlled failure injection

The current pause controller has an independent watchdog, persistent kill latch,
project identity checks, rollback journal and verified recovery. Reproduce the
complete acceptance and report:

```bash
python3 -m chaos.map_application
python3 -m observability.capture_baseline
python3 -m chaos.acceptance
python3 -m chaos.report
python3 -m chaos.dashboard
```

The suite covers normal rollback, a stricter latency threshold, manual kill,
controller termination/crash, lease expiry and actuator restart. The planner
learns from real run history; only policy-approved profiles can be proposed.
See the [submission guide](docs/hackathon-submission.md) for interactive operator
commands. The legacy stop demo remains available below; its file kill switch is
separate from the pause actuator's journaled latch. Run the two paths separately.

The first sandbox experiment stops the notification consumer while checkout
continues and RabbitMQ buffers the notification. A safety controller enforces
the target allow-list, single-fault limit, monitoring thresholds, 15-second
unavailability limit, automatic rollback, and 30-second recovery deadline.

```bash
# Preflight only
python3 chaos/run_experiment.py --dry-run

# Inject the controlled fault and automatically recover
python3 chaos/run_experiment.py
```

See [`docs/controlled-failure-injection.md`](docs/controlled-failure-injection.md)
for the experiment hypothesis, kill switch, guardrails, and expected outcome.

---

## Docker Compose Architecture

```mermaid
graph LR
    subgraph "Infrastructure Layer"
        PG[PostgreSQL:5433]
        MON[MongoDB:27017]
        RD[Redis:6380]
        RBT[RabbitMQ:5672]
        MH[MailHog:8025]
    end

    subgraph "Application Layer"
        AUTH[Auth:3004]
        PROD[Products:3002]
        ORD[Orders:3003]
        NOTIF[Notifications]
    end

    subgraph "Presentation Layer"
        WEB[Frontend:3000]
    end

    WEB --> AUTH
    WEB --> PROD
    WEB --> ORD
    AUTH --> PG
    PROD --> PG & MON & RD
    ORD --> PG & RBT
    RBT --> NOTIF
    NOTIF --> MON & MH
```

---

## Development

### Prisma Schema Management

Each service manages its own PostgreSQL database via Prisma:

```bash
# After changing a Prisma schema, rebuild:
docker compose up --build -d <service-name>

# Push schema changes directly (inside container):
docker exec ecommerce-auth npx prisma db push --accept-data-loss
```

### Adding a New Service

1. Create a new directory with the service code
2. Add a `Dockerfile` following the existing patterns
3. Add the service to `docker-compose.yml`
4. Expose any necessary ports

### Useful Commands

```bash
# View all container logs (follow)
docker compose logs -f

# View logs for a specific service
docker compose logs auth-service

# Rebuild a single service
docker compose up --build -d order-service

# Reset everything (wipes volumes)
docker compose down -v && docker compose up --build -d

# Access PostgreSQL
docker exec -it ecommerce-postgres psql -U ecommerce -d ecommerce_products

# Access MongoDB
docker exec -it ecommerce-mongodb mongosh -u ecommerce -p ecommerce123
```

---

## Project Structure

```
ecommerce-microservices/
├── auth-service/            # Authentication & user management
│   ├── prisma/             # User/Order schema
│   ├── src/
│   │   ├── controllers/    # Route handlers
│   │   ├── middlewares/     # Auth, error handling
│   │   ├── routes/         # Express routes
│   │   ├── utils/          # Logger, etc.
│   │   └── validators/     # express-validator rules
│   ├── Dockerfile
│   └── entrypoint.sh
│
├── product-service/         # Product catalog & search
│   ├── prisma/             # Category/Product schema
│   ├── src/
│   │   ├── controllers/
│   │   ├── middlewares/
│   │   ├── models/         # Mongoose models (reviews)
│   │   ├── routes/
│   │   └── utils/          # Cloudinary, Redis cache
│   ├── Dockerfile
│   └── entrypoint.sh
│
├── order-service/           # Orders & payments
│   ├── prisma/             # Order/OrderItem schema
│   ├── src/
│   │   ├── controllers/
│   │   ├── middlewares/
│   │   ├── routes/
│   │   └── utils/          # RabbitMQ, Invoice PDF
│   ├── Dockerfile
│   └── entrypoint.sh
│
├── notification-service/    # Email notifications
│   ├── src/
│   │   ├── models/         # Mongoose notification model
│   │   └── services/       # Email sender
│   ├── Dockerfile
│   └── entrypoint.sh
│
├── frontend/                # Next.js application
│   ├── src/
│   │   ├── app/            # Pages (App Router)
│   │   ├── components/     # UI components (shadcn)
│   │   ├── contexts/       # Auth, Cart, Theme
│   │   ├── lib/            # API clients, utilities
│   │   └── types/          # TypeScript types
│   └── Dockerfile
│
├── docker-compose.yml       # Master orchestrator
└── README.md
```

---

## License

MIT
