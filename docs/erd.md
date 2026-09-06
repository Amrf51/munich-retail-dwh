# Entity relationship diagram

Renders natively on GitHub.

> 🔗 **Interactive Schema:** [Explore this diagram interactively on dbdiagram.io](https://dbdiagram.io/d/6a9dadfe5450bea1be01caa6)

```mermaid
erDiagram
    DIM_DATE     ||--o{ FACT_SALES : "sold on"
    DIM_STORE    ||--o{ FACT_SALES : "sold at"
    DIM_PRODUCT  ||--o{ FACT_SALES : "sells"
    DIM_CUSTOMER ||--o{ FACT_SALES : "bought by"
    DIM_SUPPLIER ||--o{ DIM_PRODUCT : "supplies"

    FACT_SALES {
        bigint   sale_key           PK
        uuid     transaction_id     UK "natural key (grain: 1 line item)"
        int      date_key           FK
        int      store_key          FK
        int      product_key        FK
        int      customer_key       FK
        text     invoice_number        "degenerate dimension"
        text     transaction_status    "Completed, Returned, Canceled"
        text     payment_method        "Bargeld, Kreditkarte, etc."
        text     sales_staff_name
        int      quantity
        numeric  unit_price
        numeric  base_price
        numeric  discount_rate
        numeric  tax_amount
        numeric  total_amount          "canonical net revenue"
    }

    DIM_DATE {
        int     date_key      PK
        date    full_date     UK
        int     year
        int     quarter
        int     month
        text    month_name
        int     day_of_week
        text    day_name
        boolean is_weekend
    }

    DIM_STORE {
        int  store_key      PK
        int  store_id       UK
        text store_name
        text store_type
        text store_location
        text district_name
        text postal_code
        int  store_size_sqm
    }

    DIM_PRODUCT {
        int     product_key     PK
        int     product_id      UK
        text    product_name
        text    category
        text    subcategory
        text    brand
        text    department
        numeric base_price
        int     supplier_key    FK
    }

    DIM_SUPPLIER {
        int  supplier_key   PK
        int  supplier_id    UK
        text supplier_name
    }

    DIM_CUSTOMER {
        int  customer_key   PK
        int  customer_id    UK
        text customer_name
        text email_domain
        text loyalty_status
    }
```

## Interactive diagram

View and explore this schema interactively:
- **dbdiagram.io**: [https://dbdiagram.io/d/6a9dadfe5450bea1be01caa6](https://dbdiagram.io/d/6a9dadfe5450bea1be01caa6)
