# DECISIONS.md

1. **Indexes.** `employees.emp_code` unique (business key, duplicate guard, sort); `employees(department, joined_on)` for headcount. `attendance_logs(emp_code, date)` unique: it guarantees one record per day, and serves monthly, trend and PATCH. `attendance_logs(date desc, emp_code)` serves the default list order and month scans. I rejected an index on `status`: only five values, so it barely narrows anything.
2. **Punch-in race.** Both requests compute the same record and call `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept exactly one. The other gets `DuplicateKeyError`, which I return as 409. No check-then-insert.
3. **Ties.** `$rank` gives tied people the same rank and skips the next one. `limit` filters on `rank <= limit`, so everyone tied at the cutoff is returned, even if that is more than `limit` rows.
4. **Headcount.** The summary starts from `employees` (joined on or before month end) and uses `$lookup` into the logs. Each person counts once, with zero logs giving zero totals.
5. **At 100x.** I would pre-aggregate monthly numbers per employee, updated on each write, so analytics stop scanning raw records.