# Munich retail sales — dimensional warehouse

A star schema in PostgreSQL and a Python ELT pipeline for a denormalised retail
sales extract.

## Local setup

Requires Docker and Docker Compose.

    cp .env.example .env
    make up

Postgres listens on `localhost:5433`, Adminer on http://localhost:8080.