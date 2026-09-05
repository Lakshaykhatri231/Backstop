#!/bin/bash
LABEL="$1"
FILE="test_results.txt"

echo "==================================================" >> "$FILE"
echo "SCENARIO: $LABEL" >> "$FILE"
echo "TIME: $(date)" >> "$FILE"
echo "==================================================" >> "$FILE"

echo "" >> "$FILE"
echo "--- Latest event (full row) ---" >> "$FILE"
psql postgresql://lakshaykhatri@localhost:5432/revenue_recovery -x -c "SELECT * FROM events ORDER BY received_at DESC LIMIT 1;" >> "$FILE"

echo "" >> "$FILE"
echo "--- Raw Razorpay payload (exactly what they sent) ---" >> "$FILE"
psql postgresql://lakshaykhatri@localhost:5432/revenue_recovery -t -c "SELECT raw_payload FROM events ORDER BY received_at DESC LIMIT 1;" | python3 -m json.tool >> "$FILE" 2>>"$FILE"

echo "" >> "$FILE"
echo "--- Latest storefront order ---" >> "$FILE"
psql postgresql://lakshaykhatri@localhost:5432/revenue_recovery -x -c "SELECT * FROM storefront_orders ORDER BY created_at DESC LIMIT 1;" >> "$FILE"

echo "" >> "$FILE"
echo "--- Merchant revenue snapshot ---" >> "$FILE"
curl -s http://localhost:8000/merchant/revenue >> "$FILE"

echo "" >> "$FILE"
echo "--- Last 5 audit log entries ---" >> "$FILE"
curl -s "http://localhost:8000/audit/log?limit=5" >> "$FILE"

echo -e "\n\n" >> "$FILE"
echo "Captured '$LABEL' -> appended to $FILE"
