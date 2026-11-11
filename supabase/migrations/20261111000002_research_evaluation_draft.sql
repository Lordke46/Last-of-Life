-- REVIEW REQUIRED. Append-only shadow evaluation; no live model or coupon changes.
CREATE SCHEMA IF NOT EXISTS research_evaluation;
REVOKE ALL ON SCHEMA research_evaluation FROM PUBLIC;
CREATE TABLE IF NOT EXISTS research_evaluation.baseline_candidates (
 id TEXT PRIMARY KEY, match_identity TEXT NOT NULL REFERENCES ai_research.match_identities(canonical_id), market TEXT NOT NULL,
 kickoff TIMESTAMPTZ NOT NULL, frozen_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 model_version TEXT NOT NULL, model_snapshot JSONB NOT NULL, model_snapshot_hash TEXT NOT NULL CHECK(model_snapshot_hash ~ '^[0-9a-f]{64}$'),
 snapshot_captured_at TIMESTAMPTZ NOT NULL,
 selected_probability NUMERIC CHECK(selected_probability IS NULL OR selected_probability BETWEEN 0 AND 1),
 price_snapshot JSONB, price_observed_at TIMESTAMPTZ, data_status TEXT NOT NULL,
 CHECK(frozen_at<kickoff), CHECK(snapshot_captured_at<=frozen_at), CHECK(price_observed_at IS NULL OR price_observed_at<=frozen_at)
);
CREATE TABLE IF NOT EXISTS research_evaluation.research_decisions (
 id TEXT PRIMARY KEY, baseline_id TEXT NOT NULL REFERENCES research_evaluation.baseline_candidates(id),
 recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), decision TEXT NOT NULL
 CHECK(decision IN ('keep','avoid','uncertain','no_research','rejected')),
 report_id TEXT, report_revision INTEGER, evaluator TEXT NOT NULL, rationale TEXT, risk_assessment JSONB,
 CHECK ((report_id IS NULL AND report_revision IS NULL) OR (report_id IS NOT NULL AND report_revision>0)),
 FOREIGN KEY(report_id,report_revision) REFERENCES ai_research.report_revisions(report_id,revision)
);
CREATE TABLE IF NOT EXISTS research_evaluation.coupon_snapshots (
 id TEXT PRIMARY KEY, recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 kind TEXT NOT NULL CHECK(kind IN ('baseline','shadow')),
 exposure_type TEXT NOT NULL DEFAULT 'hypothetical' CHECK(exposure_type IN ('hypothetical','actual')),
 odds_product NUMERIC, stake NUMERIC, currency TEXT, CHECK(stake IS NULL OR stake>0)
);
CREATE TABLE IF NOT EXISTS research_evaluation.coupon_selections (
 coupon_id TEXT NOT NULL REFERENCES research_evaluation.coupon_snapshots(id),
 baseline_id TEXT NOT NULL REFERENCES research_evaluation.baseline_candidates(id),
 market TEXT NOT NULL, selected_odds NUMERIC, PRIMARY KEY(coupon_id,baseline_id)
);
CREATE TABLE IF NOT EXISTS research_evaluation.settlements (
 id TEXT PRIMARY KEY, baseline_id TEXT NOT NULL REFERENCES research_evaluation.baseline_candidates(id),
 result_status TEXT NOT NULL, source TEXT NOT NULL, settled_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 revision INTEGER NOT NULL CHECK(revision>0), payoff NUMERIC, UNIQUE(baseline_id,revision)
);
CREATE TABLE IF NOT EXISTS research_evaluation.coupon_settlements (
 id TEXT PRIMARY KEY, coupon_id TEXT NOT NULL REFERENCES research_evaluation.coupon_snapshots(id),
 result_status TEXT NOT NULL, gross_return NUMERIC, data_source TEXT NOT NULL,
 recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 revision INTEGER NOT NULL CHECK(revision>0), UNIQUE(coupon_id,revision),
 CHECK(gross_return IS NULL OR gross_return>=0)
);
-- Timestamp is assigned by the server, not supplied by the caller.
CREATE OR REPLACE FUNCTION research_evaluation.guard_prematch() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,research_evaluation AS $$
DECLARE ko timestamptz;
BEGIN
 IF TG_TABLE_NAME='baseline_candidates' THEN
  NEW.frozen_at:=clock_timestamp();
  SELECT kickoff INTO ko FROM ai_research.match_identities WHERE canonical_id=NEW.match_identity;
  IF ko IS DISTINCT FROM NEW.kickoff THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='EVALUATION_IDENTITY_MISMATCH';
  END IF;
 ELSE
  NEW.recorded_at:=clock_timestamp();
  SELECT kickoff INTO ko FROM research_evaluation.baseline_candidates WHERE id=NEW.baseline_id;
  IF NEW.report_id IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM ai_research.report_revisions v JOIN ai_research.jobs j ON j.id=v.job_id
    JOIN research_evaluation.baseline_candidates b ON b.id=NEW.baseline_id
    WHERE v.report_id=NEW.report_id AND v.revision=NEW.report_revision
      AND j.canonical_id=b.match_identity AND v.researched_at::timestamptz < b.kickoff
      AND v.received_at::timestamptz <= NEW.recorded_at) THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='EVALUATION_REPORT_MISMATCH';
  END IF;
 END IF;
 IF ko IS NULL OR ko<=clock_timestamp() THEN
  RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='EVALUATION_PREMATCH_REQUIRED';
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS baseline_prematch ON research_evaluation.baseline_candidates;
CREATE TRIGGER baseline_prematch BEFORE INSERT ON research_evaluation.baseline_candidates FOR EACH ROW EXECUTE FUNCTION research_evaluation.guard_prematch();
DROP TRIGGER IF EXISTS decision_prematch ON research_evaluation.research_decisions;
CREATE TRIGGER decision_prematch BEFORE INSERT ON research_evaluation.research_decisions FOR EACH ROW EXECUTE FUNCTION research_evaluation.guard_prematch();
CREATE OR REPLACE FUNCTION research_evaluation.guard_coupon_time() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,research_evaluation AS $$
DECLARE ko timestamptz; baseline_market text;
BEGIN
 IF TG_TABLE_NAME='coupon_snapshots' THEN
  NEW.recorded_at:=clock_timestamp();
 ELSE
  SELECT kickoff,market INTO ko,baseline_market FROM research_evaluation.baseline_candidates WHERE id=NEW.baseline_id;
  IF ko IS NULL OR ko<=clock_timestamp() OR baseline_market IS DISTINCT FROM NEW.market THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='EVALUATION_PREMATCH_REQUIRED';
  END IF;
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS coupon_server_time ON research_evaluation.coupon_snapshots;
CREATE TRIGGER coupon_server_time BEFORE INSERT ON research_evaluation.coupon_snapshots FOR EACH ROW EXECUTE FUNCTION research_evaluation.guard_coupon_time();
DROP TRIGGER IF EXISTS coupon_selection_prematch ON research_evaluation.coupon_selections;
CREATE TRIGGER coupon_selection_prematch BEFORE INSERT ON research_evaluation.coupon_selections FOR EACH ROW EXECUTE FUNCTION research_evaluation.guard_coupon_time();
CREATE OR REPLACE FUNCTION research_evaluation.stamp_settlement() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,research_evaluation AS $$
BEGIN
 IF TG_TABLE_NAME='settlements' THEN NEW.settled_at:=clock_timestamp();
 ELSE NEW.recorded_at:=clock_timestamp(); END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS settlement_server_time ON research_evaluation.settlements;
CREATE TRIGGER settlement_server_time BEFORE INSERT ON research_evaluation.settlements FOR EACH ROW EXECUTE FUNCTION research_evaluation.stamp_settlement();
DROP TRIGGER IF EXISTS coupon_settlement_server_time ON research_evaluation.coupon_settlements;
CREATE TRIGGER coupon_settlement_server_time BEFORE INSERT ON research_evaluation.coupon_settlements FOR EACH ROW EXECUTE FUNCTION research_evaluation.stamp_settlement();
CREATE OR REPLACE FUNCTION research_evaluation.reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='EVALUATION_APPEND_ONLY'; END $$;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['baseline_candidates','research_decisions','coupon_snapshots','coupon_selections','settlements','coupon_settlements'] LOOP
  EXECUTE format('DROP TRIGGER IF EXISTS evaluation_no_update_delete ON research_evaluation.%I',t);
  EXECUTE format('CREATE TRIGGER evaluation_no_update_delete BEFORE UPDATE OR DELETE ON research_evaluation.%I FOR EACH ROW EXECUTE FUNCTION research_evaluation.reject_mutation()',t);
  EXECUTE format('DROP TRIGGER IF EXISTS evaluation_no_truncate ON research_evaluation.%I',t);
  EXECUTE format('CREATE TRIGGER evaluation_no_truncate BEFORE TRUNCATE ON research_evaluation.%I FOR EACH STATEMENT EXECUTE FUNCTION research_evaluation.reject_mutation()',t);
 END LOOP;
END $$;
-- Deliberately no grants: trial and access policies require separate approval.
