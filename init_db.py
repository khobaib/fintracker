import sqlite3

conn = sqlite3.connect('fintracker.db')

with open('schema_v3_final.sql', encoding='utf-8') as f:
    sql = f.read()

# Clear rules tables so INSERT OR IGNORE picks up all changes
# Data tables (transactions, trips, etc.) are NOT touched
conn.execute("DELETE FROM classifier_rules")
conn.execute("DELETE FROM purpose_taxonomy")
conn.execute("DELETE FROM payment_method")
conn.execute("DELETE FROM accounts")
conn.execute("DELETE FROM currencies")
conn.execute("DELETE FROM cities")
conn.commit()

conn.executescript(sql)
conn.close()
print("Database rules and taxonomy refreshed.")
print("Transaction data preserved.")
