CREATE SCHEMA IF NOT EXISTS iceberg.gold;

CREATE OR REPLACE TABLE iceberg.gold.dim_bank
WITH (format = 'PARQUET') AS
SELECT
    row_number() OVER (ORDER BY bank_code)                     AS bank_sk,
    bank_code,
    bank_name,
    source_system_code,
    is_active,
    created_at
FROM (
    SELECT DISTINCT
        trim(r.bank)                                           AS bank_code,
        trim(r.bank)                                           AS bank_name,
        'BANK_STATEMENT'                                       AS source_system_code,
        true                                                   AS is_active,
        current_timestamp                                      AS created_at
    FROM iceberg.silver.finance_transactions_raw r
    WHERE r.bank IS NOT NULL
      AND trim(r.bank) <> ''
) s;

--Create merchant dimension
CREATE OR REPLACE TABLE iceberg.gold.dim_merchant
WITH (
  format = 'PARQUET'
) AS
WITH ranked AS (
    SELECT
        trim(e.merchant_norm)                                            AS merchant_norm,
        coalesce(trim(e.canonical_label), trim(e.merchant_norm))         AS merchant_label,
        row_number() OVER (
            PARTITION BY trim(e.merchant_norm)
            ORDER BY
                CASE
                    WHEN e.canonical_label IS NOT NULL
                     AND trim(e.canonical_label) <> '' THEN 0
                    ELSE 1
                END,
                e.enriched_at DESC NULLS LAST,
                e.batch_id DESC NULLS LAST
        )                                                                AS rn
    FROM iceberg.silver.finance_txn_enrichment e
    WHERE e.merchant_norm IS NOT NULL
      AND trim(e.merchant_norm) <> ''
)
SELECT
    row_number() OVER (ORDER BY merchant_norm)                           AS merchant_sk,
    merchant_norm,
    merchant_label,
    --null                                                               AS merchant_group_code,
    true                                                                 AS is_active,
    current_timestamp                                                    AS created_at
FROM ranked
WHERE rn = 1;

---dimension for categories
CREATE OR REPLACE TABLE iceberg.gold.dim_transaction_category
WITH (format = 'PARQUET') AS
SELECT
    row_number() OVER (ORDER BY category_id)                   AS transaction_category_sk,
    category_id,
    category_l1,
    category_l2,
    category_path,
    category_description,
    domain_code,
    is_active,
    created_at
FROM (
    SELECT
        c.category_id                                          AS category_id,
        c.category_l1                                          AS category_l1,
        c.category_l2                                          AS category_l2,
        concat(c.category_l1, '.', c.category_l2)              AS category_path,
        c.description                                          AS category_description,
        c.domain                                               AS domain_code,
        c.active                                               AS is_active,
        c.created_at                                           AS created_at
    FROM iceberg.silver.ref_finance_categories c
) s;

-- source document dimension
CREATE OR REPLACE TABLE iceberg.gold.dim_source_document
WITH (
  format = 'PARQUET'
) AS
WITH bank_docs AS (
    SELECT DISTINCT
        concat(
            coalesce(trim(source_path), ''), '||',
            coalesce(trim(source_pdf), ''), '||',
            coalesce(trim(source_type), ''), '||',
            coalesce(trim(period), '')
        )                                                      AS source_document_id,
        'BANK_STATEMENT'                                       AS source_system_code,
        trim(source_type)                                      AS source_type_code,
        trim(source_pdf)                                       AS source_pdf_name,
        trim(source_path)                                      AS source_path,
        trim(period)                                           AS statement_period_code,
        current_timestamp                                      AS created_at
    FROM iceberg.silver.finance_transactions_raw
    WHERE source_pdf IS NOT NULL
),
mortgage_docs AS (
    SELECT DISTINCT
        concat(
            'MORTGAGE||',
            coalesce(trim(source_pdf), '')
        )                                                      AS source_document_id,
        'MORTGAGE_STATEMENT'                                   AS source_system_code,
        'MORTGAGE_PAYMENT'                                     AS source_type_code,
        trim(source_pdf)                                       AS source_pdf_name,
        null                                                   AS source_path,
        null                                                   AS statement_period_code,
        current_timestamp                                      AS created_at
    FROM iceberg.silver.finance_mortgage_payments
    WHERE source_pdf IS NOT NULL
),
all_docs AS (
    SELECT * FROM bank_docs
    UNION
    SELECT * FROM mortgage_docs
)
SELECT
    row_number() OVER (
        ORDER BY source_system_code, source_document_id
    )                                                          AS source_document_sk,
    source_document_id,
    source_system_code,
    source_type_code,
    source_pdf_name,
    source_path,
    statement_period_code,
    created_at
FROM all_docs;

-- mortgage contract dimension
CREATE OR REPLACE TABLE iceberg.gold.dim_mortgage_contract
WITH (
  format = 'PARQUET'
) AS
WITH base AS (
    SELECT
        trim(mp.contract_num)                                      AS contract_num,
        'bbva'                                                     AS bank_code,
        cast(min(mp.date_charge) as date)                          AS contract_start_date,
        cast(max(mp.date_charge) as date)                          AS contract_end_date,
        true                                                       AS is_active,
        current_timestamp                                          AS created_at
    FROM iceberg.silver.finance_mortgage_payments mp
    WHERE mp.contract_num IS NOT NULL
      AND trim(mp.contract_num) <> ''
    GROUP BY trim(mp.contract_num)
)
SELECT
    row_number() OVER (ORDER BY contract_num)                      AS mortgage_contract_sk,
    contract_num,
    bank_code,
    contract_start_date,
    contract_end_date,
    is_active,
    created_at
FROM base;

-- interest periods dimension
CREATE OR REPLACE TABLE iceberg.gold.dim_interest_rate_period
WITH (
  format = 'PARQUET'
) AS
WITH base AS (
    SELECT DISTINCT
        concat(
            cast(period_init as varchar), '||',
            cast(period_end as varchar), '||',
            cast(rate_month as varchar)
        )                                                          AS interest_rate_period_id,
        period_init                                                AS period_init_date,
        period_end                                                 AS period_end_date,
        rate_month                                                 AS rate_month,
        boe_ref                                                    AS boe_reference_code,
        euribor_year                                               AS euribor_year_num,
        vpo_plan_rate_pct                                          AS vpo_plan_rate_pct,
        euribor_12m_pct                                            AS euribor_12m_pct,
        irph_entities_pct                                          AS irph_entities_pct,
        review_6m_month                                            AS review_6m_month,
        review_12m_month                                           AS review_12m_month,
        source_note                                                AS source_note,
        current_timestamp                                          AS created_at
    FROM iceberg.silver.ref_finance_interest_types
)
SELECT
    row_number() OVER (
        ORDER BY period_init_date, period_end_date, rate_month
    )                                                              AS interest_rate_period_sk,
    interest_rate_period_id,
    period_init_date,
    period_end_date,
    rate_month,
    boe_reference_code,
    euribor_year_num,
    vpo_plan_rate_pct,
    euribor_12m_pct,
    irph_entities_pct,
    review_6m_month,
    review_12m_month,
    source_note,
    created_at
FROM base;


--dimension dates diary 
CREATE OR REPLACE TABLE iceberg.gold.dim_date
WITH (
  format = 'PARQUET'
) AS
WITH date_series AS (
    SELECT d AS full_date
    FROM UNNEST(
        sequence(DATE '2007-12-19', DATE '2034-12-31', INTERVAL '1' DAY)
    ) AS t(d)
)
SELECT
    CAST(date_format(full_date, '%Y%m%d') AS INTEGER)       AS date_sk,
    full_date                                               AS full_date,
    year(full_date)                                         AS year_num,
    quarter(full_date)                                      AS quarter_num,
    month(full_date)                                        AS month_num,
    day(full_date)                                          AS day_of_month_num,
    day_of_week(full_date)                                  AS day_of_week_num,
    date_format(full_date, '%W')                            AS day_name,
    date_format(full_date, '%M')                            AS month_name,
    CAST(date_format(full_date, '%Y%m') AS INTEGER)         AS year_month_code,
    week(full_date)                                         AS week_of_year_num,
    CASE
        WHEN full_date = last_day_of_month(full_date) THEN true
        ELSE false
    END                                                     AS is_month_end,
    CASE
        WHEN day_of_week(full_date) IN (6, 7) THEN true
        ELSE false
    END                                                     AS is_weekend,
    current_timestamp                                       AS created_at
FROM date_series;

--date monthly bases
CREATE OR REPLACE TABLE iceberg.gold.dim_month
WITH (
  format = 'PARQUET'
) AS
WITH month_series AS (
    SELECT m AS month_start_date
    FROM UNNEST(
        sequence(DATE '2007-01-01', DATE '2030-12-01', INTERVAL '1' MONTH)
    ) AS t(m)
)
SELECT
    CAST(date_format(month_start_date, '%Y%m') AS INTEGER)  AS month_sk,
    month_start_date                                        AS month_start_date,
    last_day_of_month(month_start_date)                     AS month_end_date,
    year(month_start_date)                                  AS year_num,
    quarter(month_start_date)                               AS quarter_num,
    month(month_start_date)                                 AS month_num,
    date_format(month_start_date, '%M')                     AS month_name,
    CAST(date_format(month_start_date, '%Y%m') AS INTEGER)  AS year_month_code,
    current_timestamp                                       AS created_at
FROM month_series;

CREATE OR REPLACE TABLE iceberg.gold.fact_bank_transaction
WITH (
  format = 'PARQUET'
) AS
WITH enrichment_dedup AS (
    SELECT *
    FROM (
        SELECT
            e.*,
            row_number() OVER (
                PARTITION BY e.txn_id
                ORDER BY e.enriched_at DESC NULLS LAST, e.batch_id DESC NULLS LAST
            ) AS rn
        FROM iceberg.silver.finance_txn_enrichment e
    ) x
    WHERE rn = 1
),
raw_base AS (
    SELECT
        r.txn_id,
        trim(r.bank)                                  AS bank_code,
        trim(r.source_type)                           AS source_type_code,
        trim(r.period)                                AS period_code,
        cast(r.statement_date as date)                AS statement_date,
        cast(r.txn_date as date)                      AS txn_date,
        cast(r.value_date as date)                    AS value_date,
        r.concept,
        r.concept_norm,
        r.merchant_key,
        r.location,
        trim(r.currency)                              AS currency_code,
        r.amount,
        r.amount_abs,
        r.balance,
        trim(r.source_pdf)                            AS source_pdf_name,
        trim(r.source_path)                           AS source_path,
        r.page                                        AS page_num,
        r.row_idx                                     AS row_num,
        r.raw,
        r.ingested_at,
        r.batch_id
    FROM iceberg.silver.finance_transactions_raw r
),
joined_data AS (
    SELECT
        r.*,
        e.merchant_norm,
        e.canonical_label                             AS merchant_label,
        e.category_id,
        e.category_l1,
        e.category_l2,
        e.category_path,
        e.confidence                                  AS confidence_score,
        e.validation_status,
        e.requires_review                             AS requires_review_flag,
        e.catalog_hash,
        e.taxonomy_source,
        e.taxonomy_table,
        e.method                                      AS classification_method,
        e.model                                       AS classification_model,
        e.embed_model,
        e.catalog_version,
        e.prompt_hash,
        e.retrieval_top_k,
        e.chosen_candidate_id,
        e.candidates_json,
        e.enrichment_json,
        e.enriched_at                                 AS classification_ts
    FROM raw_base r
    LEFT JOIN enrichment_dedup e
        ON r.txn_id = e.txn_id
),
joined_dims AS (
    SELECT
        j.txn_id,
        d_txn.date_sk                                 AS txn_date_sk,
        d_val.date_sk                                 AS value_date_sk,
        d_stmt.date_sk                                AS statement_date_sk,
        b.bank_sk,
        c.transaction_category_sk,
        m.merchant_sk,
        sd.source_document_sk,
        j.bank_code,
        j.source_type_code,
        j.period_code,
        j.statement_date,
        j.txn_date,
        j.value_date,
        j.concept,
        j.concept_norm,
        j.merchant_key,
        j.merchant_label,
        j.location,
        j.currency_code,
        j.amount,
        j.amount_abs,
        j.balance,
        j.source_pdf_name,
        j.source_path,
        j.page_num,
        j.row_num,
        j.raw,
        j.category_id,
        j.category_l1,
        j.category_l2,
        j.category_path,
        j.merchant_norm,
        j.confidence_score,
        j.validation_status,
        j.requires_review_flag,
        j.catalog_hash,
        j.taxonomy_source,
        j.taxonomy_table,
        j.classification_method,
        j.classification_model,
        j.embed_model,
        j.catalog_version,
        j.prompt_hash,
        j.retrieval_top_k,
        j.chosen_candidate_id,
        j.candidates_json,
        j.enrichment_json,
        j.classification_ts,
        j.ingested_at,
        j.batch_id
    FROM joined_data j
    LEFT JOIN iceberg.gold.dim_date d_txn
        ON j.txn_date = d_txn.full_date
    LEFT JOIN iceberg.gold.dim_date d_val
        ON j.value_date = d_val.full_date
    LEFT JOIN iceberg.gold.dim_date d_stmt
        ON j.statement_date = d_stmt.full_date
    LEFT JOIN iceberg.gold.dim_bank b
        ON j.bank_code = b.bank_code
    LEFT JOIN iceberg.gold.dim_transaction_category c
        ON j.category_id = c.category_id
    LEFT JOIN iceberg.gold.dim_merchant m
        ON j.merchant_norm = m.merchant_norm
    LEFT JOIN iceberg.gold.dim_source_document sd
        ON concat(
            coalesce(j.source_path, ''), '||',
            coalesce(j.source_pdf_name, ''), '||',
            coalesce(j.source_type_code, ''), '||',
            coalesce(j.period_code, '')
        ) = sd.source_document_id
)
SELECT
    txn_id,
    txn_date_sk,
    value_date_sk,
    statement_date_sk,
    bank_sk,
    transaction_category_sk,
    merchant_sk,
    source_document_sk,
    bank_code,
    category_id,
    merchant_norm,
    source_type_code,
    period_code,
    statement_date,
    txn_date,
    value_date,
    concept,
    concept_norm,
    merchant_key,
    merchant_label,
    location,
    currency_code,
    amount,
    amount_abs,
    balance,
    source_pdf_name,
    source_path,
    page_num,
    row_num,
    raw,
    category_l1,
    category_l2,
    category_path,
    confidence_score,
    validation_status,
    requires_review_flag,
    catalog_hash,
    taxonomy_source,
    taxonomy_table,
    classification_method,
    classification_model,
    embed_model,
    catalog_version,
    prompt_hash,
    retrieval_top_k,
    chosen_candidate_id,
    candidates_json,
    enrichment_json,
    classification_ts,
    ingested_at,
    batch_id
FROM joined_dims;

--fact mortgage payments-------------------
CREATE OR REPLACE TABLE iceberg.gold.fact_mortgage_payment
WITH (
  format = 'PARQUET'
) AS
WITH mortgage_base AS (
    SELECT
        trim(mp.contract_num)                            AS contract_num,
        trim(mp.source_pdf)                              AS source_pdf_name,
        mp.page                                          AS page_num,
        cast(mp.date_charge as date)                     AS charge_date,
        cast(mp.period_init as date)                     AS amortization_period_start_date,
        cast(mp.period_end as date)                      AS amortization_period_end_date,
        mp.capital_amort                                 AS capital_amortized_amt,
        mp.interests                                     AS interest_amt,
        mp.import_total                                  AS installment_total_amt,
        mp.capital_pending                               AS outstanding_principal_amt,
        mp.base_calculous                                AS calculation_base_amt,
        mp.interest_type                                 AS applied_interest_rate_pct,
        mp.ingested_at
    FROM iceberg.silver.finance_mortgage_payments mp
),
mortgage_with_rate AS (
    SELECT
        m.*,
        irp.interest_rate_period_sk,
        irp.interest_rate_period_id,
        irp.rate_month,
        irp.period_init_date,
        irp.period_end_date,
        irp.boe_reference_code,
        irp.euribor_year_num,
        irp.vpo_plan_rate_pct,
        irp.euribor_12m_pct,
        irp.irph_entities_pct,
        irp.review_6m_month,
        irp.review_12m_month,
        irp.source_note
    FROM mortgage_base m
    LEFT JOIN iceberg.gold.dim_interest_rate_period irp
        ON date_trunc('month', m.charge_date) = irp.rate_month
),
joined_dims AS (
    SELECT
        m.contract_num,
        mc.mortgage_contract_sk,
        d_charge.date_sk                                  AS charge_date_sk,
        d_pinit.date_sk                                   AS period_init_date_sk,
        d_pend.date_sk                                    AS period_end_date_sk,
        dm_rate.month_sk                                  AS rate_month_sk,
        dm_r6.month_sk                                    AS review_6m_month_sk,
        dm_r12.month_sk                                   AS review_12m_month_sk,
        sd.source_document_sk,
        m.interest_rate_period_sk,
        m.interest_rate_period_id,
        m.source_pdf_name,
        m.page_num,
        m.charge_date,
        m.amortization_period_start_date,
        m.amortization_period_end_date,
        m.capital_amortized_amt,
        m.interest_amt,
        m.installment_total_amt,
        m.outstanding_principal_amt,
        m.calculation_base_amt,
        m.applied_interest_rate_pct,
        m.rate_month,
        m.period_init_date,
        m.period_end_date,
        m.boe_reference_code,
        m.euribor_year_num,
        m.vpo_plan_rate_pct,
        m.euribor_12m_pct,
        m.irph_entities_pct,
        m.review_6m_month,
        m.review_12m_month,
        m.source_note,
        m.ingested_at
    FROM mortgage_with_rate m
    LEFT JOIN iceberg.gold.dim_mortgage_contract mc
        ON m.contract_num = mc.contract_num
    LEFT JOIN iceberg.gold.dim_date d_charge
        ON m.charge_date = d_charge.full_date
    LEFT JOIN iceberg.gold.dim_date d_pinit
        ON m.amortization_period_start_date = d_pinit.full_date
    LEFT JOIN iceberg.gold.dim_date d_pend
        ON m.amortization_period_end_date = d_pend.full_date
    LEFT JOIN iceberg.gold.dim_month dm_rate
        ON m.rate_month = dm_rate.month_start_date
    LEFT JOIN iceberg.gold.dim_month dm_r6
        ON m.review_6m_month = dm_r6.month_start_date
    LEFT JOIN iceberg.gold.dim_month dm_r12
        ON m.review_12m_month = dm_r12.month_start_date
    LEFT JOIN iceberg.gold.dim_source_document sd
        ON upper(concat('MORTGAGE||', coalesce(trim(m.source_pdf_name), '')))
         = upper(sd.source_document_id)
)
SELECT
    mortgage_contract_sk,
    interest_rate_period_sk,
    source_document_sk,
    charge_date_sk,
    period_init_date_sk,
    period_end_date_sk,
    rate_month_sk,
    review_6m_month_sk,
    review_12m_month_sk,
    contract_num,
    interest_rate_period_id,
    source_pdf_name,
    page_num,
    charge_date,
    amortization_period_start_date,
    amortization_period_end_date,
    capital_amortized_amt,
    interest_amt,
    installment_total_amt,
    outstanding_principal_amt,
    calculation_base_amt,
    applied_interest_rate_pct,
    rate_month,
    boe_reference_code,
    euribor_year_num,
    vpo_plan_rate_pct,
    euribor_12m_pct,
    irph_entities_pct,
    review_6m_month,
    review_12m_month,
    source_note,
    CASE
        WHEN source_document_sk IS NULL THEN true
        ELSE false
    END                                                     AS is_reconstructed_flag,
    CASE
        WHEN source_document_sk IS NULL THEN false
        ELSE true
    END                                                     AS source_document_available_flag,
    CASE
        WHEN source_document_sk IS NULL THEN 'RECONSTRUCTED'
        ELSE 'DOCUMENTED'
    END                                                     AS data_quality_status,
    CASE
        WHEN source_document_sk IS NULL THEN 'RECONSTRUCTED'
        ELSE 'PDF_STATEMENT'
    END                                                     AS record_source_type,
    CASE
        WHEN source_document_sk IS NULL THEN 'MORTGAGE_INFERENCE_V1'
        ELSE null
    END                                                     AS reconstruction_method,
    CASE
        WHEN source_document_sk IS NULL THEN 'No source PDF available- reconstructed record'
        ELSE 'Linked to source mortgage document'
    END                                                     AS data_quality_notes,
    ingested_at
FROM joined_dims;

CREATE OR REPLACE VIEW iceberg.gold.vw_bank_transaction_enriched AS
SELECT
    f.txn_id,
    f.bank_code,
    f.source_type_code,
    f.period_code,
    f.statement_date,
    f.txn_date,
    f.value_date,
    f.concept,
    f.concept_norm,
    f.merchant_key,
    f.merchant_norm,
    f.merchant_label,
    f.location,
    f.currency_code,
    f.amount,
    f.amount_abs,
    f.balance,
    f.category_id,
    f.category_l1,
    f.category_l2,
    f.category_path,
    f.confidence_score,
    f.validation_status,
    f.requires_review_flag,
    f.classification_method,
    f.classification_model,
    f.source_pdf_name,
    f.source_path,
    f.page_num,
    f.row_num,
    f.classification_ts,
    f.ingested_at,
    f.batch_id
FROM iceberg.gold.fact_bank_transaction f;

CREATE OR REPLACE VIEW iceberg.gold.vw_mortgage_payment_enriched AS
SELECT
    f.contract_num,
    f.source_pdf_name,
    f.page_num,
    f.charge_date,
    f.amortization_period_start_date,
    f.amortization_period_end_date,
    f.capital_amortized_amt,
    f.interest_amt,
    f.installment_total_amt,
    f.outstanding_principal_amt,
    f.calculation_base_amt,
    f.applied_interest_rate_pct,
    f.rate_month,
    f.boe_reference_code,
    f.vpo_plan_rate_pct                             AS boe_interest_pct,
    f.irph_entities_pct                             AS irph_official_pct,
    f.euribor_12m_pct,
    CAST(f.irph_entities_pct - f.euribor_12m_pct AS DECIMAL(6,3)) AS irph_vs_euribor_spread_pct,
    f.review_6m_month,
    f.review_12m_month,
    f.source_note,
    f.is_reconstructed_flag,
    f.source_document_available_flag,
    f.data_quality_status,
    f.record_source_type,
    f.reconstruction_method,
    f.data_quality_notes,
    f.ingested_at
FROM iceberg.gold.fact_mortgage_payment f
ORDER BY f.charge_date;