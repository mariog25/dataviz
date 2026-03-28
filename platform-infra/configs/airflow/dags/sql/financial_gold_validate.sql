-- Bank fact count must match raw
SELECT
  CASE
    WHEN (
      (SELECT count(*) FROM iceberg.silver.finance_transactions_raw) =
      (SELECT count(*) FROM iceberg.gold.fact_bank_transaction)
    )
    THEN 1
    ELSE CAST(fail('fact_bank_transaction count mismatch') AS integer)
  END;

-- No duplicated txn_id
SELECT
  CASE
    WHEN NOT EXISTS (
      SELECT txn_id
      FROM iceberg.gold.fact_bank_transaction
      GROUP BY txn_id
      HAVING count(*) > 1
    )
    THEN 1
    ELSE CAST(fail('duplicated txn_id in fact_bank_transaction') AS integer)
  END;

-- Mortgage fact count must match silver
SELECT
  CASE
    WHEN (
      (SELECT count(*) FROM iceberg.silver.finance_mortgage_payments) =
      (SELECT count(*) FROM iceberg.gold.fact_mortgage_payment)
    )
    THEN 1
    ELSE CAST(fail('fact_mortgage_payment count mismatch') AS integer)
  END;