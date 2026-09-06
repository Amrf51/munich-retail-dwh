# Munich Retail Sales — Dimensional Data Warehouse

A production-grade Kimball dimensional warehouse (Star Schema) in PostgreSQL with an idempotent Python ELT pipeline for a retail sales extract in Munich, Germany.

---

## Quickstart

### Prerequisites
* [Docker](https://www.docker.com/) & Docker Compose
* Python >= 3.11 with [uv](https://docs.astral.sh/uv/) (or standard Python `pip`)

### 1. Start Environment & Deploy Schema
```bash
# 1. Create your local environment configuration
cp .env.example .env

# 2. Start PostgreSQL (port 5433) and Adminer (port 8080)
make up

# 3. Install dependencies and CLI into virtualenv
make install

# 4. Deploy schemas and database objects (idempotent)
make migrate

# 5. Inspect database tables and row counts
make inspect
```

* **PostgreSQL:** `localhost:5433` (DB: `munich_retail`, User: `dwh`, Pass: `dwh`)
* **Adminer Web UI:** [http://localhost:8080](http://localhost:8080)

---

## Architecture & Pipeline Layers

The warehouse implements an ELT architecture separated into **4 distinct schemas**:

```
[CSV: munich_retail_sales_raw.csv]
                 │
                 ▼
          ┌──────────────┐
          │     raw      │   <-- Untyped landing zone (all TEXT via COPY)
          └──────┬───────┘
                 │
                 ▼
          ┌──────────────┐
          │   staging    │   <-- Cleansed, typed, arithmetic repaired & flagged
          └──────┬───────┘
                 │
                 ▼
          ┌──────────────┐
          │     mart     │   <-- Star Schema for BI & Analytics (Kimball)
          └──────────────┘

          ┌──────────────┐
          │     meta     │   <-- Audit ledger, migrations, and batch history
          └──────────────┘
```

1. **`raw` (Landing & Quarantine):**
   * Raw ingestion where every source column lands as `TEXT` using PostgreSQL `COPY`.
   * Untyped landing ensures ingestion **never crashes** due to formatting anomalies or `"N/A"` strings.
   * Completely unparseable rows route to a dead-letter queue (`raw.rejects`) rather than being dropped.

2. **`staging` (Cleansing & Standardization):**
   * **Data Types:** Casts raw strings into typed numeric, timestamp, and boolean fields.
   * **Date Parsing:** Unifies 3 mixed date patterns (`YYYY-MM-DD`, `DD/MM/YYYY`, `MM-DD-YYYY`).
   * **Casing & Aliases:** Standardizes casing drift (`completed` $\rightarrow$ `Completed`) and resolves vendor/store aliases (e.g. `OEZ München` $\rightarrow$ `Olympia Einkaufszentrum`).
   * **Canonical Revenue:** Recomputes `total_amount = quantity * unit_price` because source totals contained mathematical corruption, while preserving `total_amount_raw` for financial auditability.
   * **Observability:** Sets bitwise flags (`dq_flags`) to identify anomalies without discarding data.

3. **`mart` (Dimensional Star Schema):**
   * **Fact Table:** `fact_sales` (Grain: 1 transaction line item).
   * **Dimensions:** `dim_date`, `dim_store`, `dim_product`, `dim_supplier`, `dim_customer`.
   * **Handling Missing Data:** Maps walk-in / guest shoppers (23.5% missing `customer_id`) to surrogate key `-1` so analytical `INNER JOIN`s never drop revenue.
   * **Localization:** `dim_date` includes Bavarian public holidays (*Ladenschlussgesetz* store closing laws).

4. **`meta` (Bookkeeping & Idempotency):**
   * `meta.schema_migrations`: Tracks SHA256 checksums of applied `.sql` files, guaranteeing zero-side-effect, idempotent runs.
   * `meta.load_batch` & `meta.dq_result`: Records execution lineage and data quality test history over time.

---

## Data Modeling & Design Decisions

* **Schema ERD:** [docs/erd.md](docs/erd.md)
* **Interactive Diagram:** [Explore on dbdiagram.io](https://dbdiagram.io/d/6a9dadfe5450bea1be01caa6)
* **Source Data Profiling:** [docs/profiling_findings.md](docs/profiling_findings.md)

### Key Architectural Choices:
* **`transaction_id` is typed as `TEXT`:** Data profiling identified 34,923 UUIDs and 689 10-character alphanumeric strings. Declaring `UUID` would cause Postgres to reject 689 valid transactions.
* **`invoice_number` as a Degenerate Dimension:** 99.6% of multi-row invoice numbers carried conflicting dates and stores. `invoice_number` is a colliding synthetic string, not a true basket header. Treating it as a degenerate dimension prevents grain corruption.
* **Selective Snowflaking (`dim_supplier`):** `dim_supplier` is normalized out of `dim_product` to isolate supplier master records and manage aliasing cleanly.

---

## Makefile Reference

| Command | Action |
| :--- | :--- |
| `make help` | Show all available commands and descriptions |
| `make up` | Start PostgreSQL and Adminer containers |
| `make install` | Install Python dependencies and CLI (`uv sync --all-extras`) |
| `make migrate` | Deploy and apply database migrations idempotently |
| `make extract` | Ingest raw CSV into `raw.sales_raw` |
| `make transform` | Clean, standardize, repair arithmetic, and flag staging data |
| `make load` | Upsert into mart dimensions and `fact_sales` |
| `make run` | Full pipeline end-to-end (`migrate` $\rightarrow$ `extract` $\rightarrow$ `transform` $\rightarrow$ `load`) |
| `make test` | Run automated warehouse validation test suite (pytest) |
| `make inspect` | Display table catalog and live row counts |
| `make batches` | Display recent pipeline execution logs from `meta.load_batch` |
| `make profile` | Re-run source CSV profiler and regenerate documentation |
| `make psql` | Open an interactive `psql` shell inside the PostgreSQL container |
| `make down` | Stop Docker containers (preserves data volume) |
| `make nuke` | Stop containers and wipe the database volume |