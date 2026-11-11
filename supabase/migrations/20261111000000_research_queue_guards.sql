-- REVIEW REQUIRED. Apply only to a disposable test DB until explicitly approved.
-- Identity is operator-reviewed: no fuzzy name-based merge is performed.
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='lol_research_identity_admin') THEN
  CREATE ROLE lol_research_identity_admin NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
 END IF;
END $$;
CREATE TABLE IF NOT EXISTS ai_research.match_identities (
 canonical_id TEXT PRIMARY KEY,
 competition TEXT NOT NULL CHECK (length(trim(competition))>0),
 home TEXT NOT NULL CHECK (length(trim(home))>0),
 away TEXT NOT NULL CHECK (length(trim(away))>0),
 kickoff TIMESTAMPTZ NOT NULL,
 reviewed_by TEXT NOT NULL CHECK (length(trim(reviewed_by))>0),
 review_reason TEXT NOT NULL CHECK (length(trim(review_reason))>0),
 reviewed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS ai_research.match_aliases (
 alias TEXT PRIMARY KEY, canonical_id TEXT NOT NULL REFERENCES ai_research.match_identities(canonical_id),
 provider TEXT NOT NULL CHECK(length(trim(provider))>0),
 source_competition TEXT NOT NULL CHECK(length(trim(source_competition))>0),
 source_home TEXT NOT NULL CHECK(length(trim(source_home))>0),
 source_away TEXT NOT NULL CHECK(length(trim(source_away))>0),
 source_kickoff TIMESTAMPTZ NOT NULL,
 reviewed_by TEXT NOT NULL CHECK(length(trim(reviewed_by))>0),
 review_reason TEXT NOT NULL CHECK(length(trim(review_reason))>0),
 reviewed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS ai_research.rerun_approvals (
 id TEXT PRIMARY KEY, canonical_id TEXT NOT NULL REFERENCES ai_research.match_identities(canonical_id),
 mode TEXT NOT NULL CHECK(mode IN ('full','lineup_check')), owner_id TEXT NOT NULL,
 approved_by TEXT NOT NULL CHECK(length(trim(approved_by))>0),
 reason TEXT NOT NULL CHECK(length(trim(reason))>0),
 approved_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), expires_at TIMESTAMPTZ NOT NULL,
 consumed_by_job TEXT UNIQUE
);
ALTER TABLE ai_research.jobs ADD COLUMN IF NOT EXISTS canonical_id TEXT REFERENCES ai_research.match_identities(canonical_id);
ALTER TABLE ai_research.jobs ADD COLUMN IF NOT EXISTS rerun_approval_id TEXT REFERENCES ai_research.rerun_approvals(id);
ALTER TABLE ai_research.jobs DROP CONSTRAINT IF EXISTS jobs_status_check;
ALTER TABLE ai_research.jobs ADD CONSTRAINT jobs_status_check CHECK(status IN ('queued','claimed','completed','expired','failed'));
REVOKE ALL ON ai_research.match_identities,ai_research.match_aliases,ai_research.rerun_approvals FROM PUBLIC, lol_research_runtime;
GRANT USAGE ON SCHEMA ai_research TO lol_research_identity_admin;
GRANT SELECT,INSERT ON ai_research.match_identities,ai_research.match_aliases,ai_research.rerun_approvals TO lol_research_identity_admin;

CREATE OR REPLACE FUNCTION ai_research.audit_operator_insert() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,ai_research AS $$
BEGIN
 IF TG_TABLE_NAME='rerun_approvals' THEN
  NEW.approved_by:=session_user; NEW.approved_at:=clock_timestamp(); NEW.consumed_by_job:=NULL;
  IF NEW.expires_at<=NEW.approved_at THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='RESEARCH_APPROVAL_EXPIRED';
  END IF;
 ELSE
  NEW.reviewed_by:=session_user; NEW.reviewed_at:=clock_timestamp();
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS audit_identity ON ai_research.match_identities;
CREATE TRIGGER audit_identity BEFORE INSERT ON ai_research.match_identities FOR EACH ROW EXECUTE FUNCTION ai_research.audit_operator_insert();
DROP TRIGGER IF EXISTS audit_alias ON ai_research.match_aliases;
CREATE TRIGGER audit_alias BEFORE INSERT ON ai_research.match_aliases FOR EACH ROW EXECUTE FUNCTION ai_research.audit_operator_insert();
DROP TRIGGER IF EXISTS audit_rerun ON ai_research.rerun_approvals;
CREATE TRIGGER audit_rerun BEFORE INSERT ON ai_research.rerun_approvals FOR EACH ROW EXECUTE FUNCTION ai_research.audit_operator_insert();

CREATE OR REPLACE FUNCTION ai_research.guard_job_insert() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,ai_research AS $$
DECLARE a ai_research.match_aliases%ROWTYPE; ident ai_research.match_identities%ROWTYPE;
        prior ai_research.jobs%ROWTYPE; approval ai_research.rerun_approvals%ROWTYPE;
        payload jsonb; t timestamptz := clock_timestamp(); used_count integer;
BEGIN
 IF current_setting('transaction_isolation') <> 'read committed' THEN
  RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='RESEARCH_ISOLATION_UNSUPPORTED';
 END IF;
 -- One transaction-level lock makes distinct-match/day counting and approvals atomic.
 PERFORM pg_advisory_xact_lock(675637820146::bigint);
 t := clock_timestamp();
 SELECT * INTO a FROM ai_research.match_aliases WHERE alias=NEW.match_id;
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='RESEARCH_IDENTITY_UNVERIFIED'; END IF;
 SELECT * INTO ident FROM ai_research.match_identities WHERE canonical_id=a.canonical_id;
 BEGIN payload := NEW.context_json::jsonb; EXCEPTION WHEN others THEN
  RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='RESEARCH_CONTEXT_INVALID'; END;
 IF payload #>> '{match,id}' IS DISTINCT FROM NEW.match_id
    OR payload #>> '{match,league}' IS DISTINCT FROM a.source_competition
    OR payload #>> '{match,home}' IS DISTINCT FROM a.source_home
    OR payload #>> '{match,away}' IS DISTINCT FROM a.source_away
    OR a.source_kickoff IS DISTINCT FROM ident.kickoff
    OR payload #>> '{match,kickoff}' IS NULL THEN
  RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='RESEARCH_IDENTITY_MISMATCH';
 END IF;
 BEGIN
  IF (payload #>> '{match,kickoff}')::timestamptz <> a.source_kickoff THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='RESEARCH_IDENTITY_MISMATCH';
  END IF;
 EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
  RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='RESEARCH_CONTEXT_INVALID';
 END;
 IF ident.kickoff<=t OR COALESCE(payload #>> '{match,completed}','false') IN ('true','1')
    OR lower(COALESCE(payload #>> '{match,status}','')) ~ '(postpon|cancel|abandon|suspend)'
    OR NEW.deadline::timestamptz<=t OR NEW.deadline::timestamptz>ident.kickoff THEN
  RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='RESEARCH_MATCH_NOT_UPCOMING';
 END IF;
 IF EXISTS(SELECT 1 FROM ai_research.jobs WHERE mode=NEW.mode AND canonical_id IS NULL) THEN
  RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='RESEARCH_IDENTITY_BACKFILL_REQUIRED';
 END IF;
 NEW.canonical_id := a.canonical_id;
 NEW.created_at := t::text;
 IF EXISTS(SELECT 1 FROM ai_research.jobs WHERE canonical_id=NEW.canonical_id AND mode=NEW.mode
     AND owner_id=NEW.owner_id AND match_id=NEW.match_id AND request_id=NEW.request_id
     AND input_version=NEW.input_version AND rerun_approval_id IS NOT DISTINCT FROM NEW.rerun_approval_id) THEN
  RETURN NULL;
 END IF;
 IF EXISTS(SELECT 1 FROM ai_research.jobs WHERE mode=NEW.mode AND owner_id=NEW.owner_id
     AND match_id=NEW.match_id AND request_id=NEW.request_id AND input_version=NEW.input_version) THEN
  RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='RESEARCH_IDEMPOTENCY_CONFLICT';
 END IF;
 SELECT * INTO prior FROM ai_research.jobs WHERE canonical_id=NEW.canonical_id AND mode=NEW.mode ORDER BY created_at LIMIT 1;
 IF FOUND THEN
  SELECT * INTO approval FROM ai_research.rerun_approvals WHERE id=NEW.rerun_approval_id FOR UPDATE;
  IF NOT FOUND OR approval.canonical_id<>NEW.canonical_id OR approval.mode<>NEW.mode
     OR approval.owner_id<>NEW.owner_id OR approval.consumed_by_job IS NOT NULL OR approval.expires_at<=t THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='RESEARCH_REPEAT_APPROVAL_REQUIRED';
  END IF;
  UPDATE ai_research.rerun_approvals SET consumed_by_job=NEW.id WHERE id=approval.id;
 ELSIF NEW.rerun_approval_id IS NOT NULL THEN
  RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='RESEARCH_APPROVAL_NOT_APPLICABLE';
 END IF;
 IF NEW.mode='full' THEN
  SELECT count(DISTINCT canonical_id) INTO used_count FROM ai_research.jobs
   WHERE mode='full' AND (created_at::timestamptz AT TIME ZONE 'Europe/Istanbul')::date=(t AT TIME ZONE 'Europe/Istanbul')::date;
  IF used_count>=3 AND NOT EXISTS(SELECT 1 FROM ai_research.jobs WHERE canonical_id=NEW.canonical_id AND mode='full'
       AND (created_at::timestamptz AT TIME ZONE 'Europe/Istanbul')::date=(t AT TIME ZONE 'Europe/Istanbul')::date) THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='RESEARCH_DAILY_QUOTA_EXCEEDED';
  END IF;
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS research_guard_job_insert ON ai_research.jobs;
CREATE TRIGGER research_guard_job_insert BEFORE INSERT ON ai_research.jobs FOR EACH ROW EXECUTE FUNCTION ai_research.guard_job_insert();

CREATE OR REPLACE FUNCTION ai_research.guard_job_identity_update() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,ai_research AS $$
BEGIN
 IF NEW.match_id IS DISTINCT FROM OLD.match_id OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
    OR NEW.mode IS DISTINCT FROM OLD.mode OR NEW.created_at IS DISTINCT FROM OLD.created_at
    OR NEW.rerun_approval_id IS DISTINCT FROM OLD.rerun_approval_id OR NEW.context_json IS DISTINCT FROM OLD.context_json
    OR NEW.input_version IS DISTINCT FROM OLD.input_version OR NEW.request_id IS DISTINCT FROM OLD.request_id
    OR NEW.deadline IS DISTINCT FROM OLD.deadline THEN
  RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='RESEARCH_JOB_IDENTITY_IMMUTABLE';
 END IF;
 IF NEW.canonical_id IS DISTINCT FROM OLD.canonical_id THEN
  IF OLD.canonical_id IS NOT NULL OR NOT pg_has_role(session_user,'lol_research_identity_admin','MEMBER')
     OR NOT EXISTS(SELECT 1 FROM ai_research.match_aliases a JOIN ai_research.match_identities i ON i.canonical_id=a.canonical_id
         WHERE a.alias=OLD.match_id AND a.canonical_id=NEW.canonical_id
           AND (OLD.context_json::jsonb)#>>'{match,id}'=a.alias
           AND (OLD.context_json::jsonb)#>>'{match,league}'=a.source_competition
           AND (OLD.context_json::jsonb)#>>'{match,home}'=a.source_home
           AND (OLD.context_json::jsonb)#>>'{match,away}'=a.source_away
           AND ((OLD.context_json::jsonb)#>>'{match,kickoff}')::timestamptz=a.source_kickoff
           AND a.source_kickoff=i.kickoff) THEN
   RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='RESEARCH_IDENTITY_BACKFILL_FORBIDDEN';
  END IF;
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS research_guard_job_identity_update ON ai_research.jobs;
CREATE TRIGGER research_guard_job_identity_update BEFORE UPDATE ON ai_research.jobs FOR EACH ROW EXECUTE FUNCTION ai_research.guard_job_identity_update();
