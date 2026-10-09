"""Creates the tables; safe to run on every deploy."""

import ledger

if __name__ == "__main__":
    with ledger.connect() as conn:
        conn.execute(ledger.SCHEMA)
    print("schema ready", flush=True)
