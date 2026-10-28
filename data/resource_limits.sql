-- Run as SYS in the application PDB before accepting business queries.
-- CPU_PER_CALL is in hundredths of a second; these limits cover parse/execute/fetch.
ALTER SYSTEM SET RESOURCE_LIMIT = TRUE;
CREATE PROFILE atb_query_limits LIMIT
    CPU_PER_CALL 1000
    LOGICAL_READS_PER_CALL 100000;
ALTER USER atb PROFILE atb_query_limits;
GRANT SELECT ON sys.v_$parameter TO atb;
