-- REVIEW REQUIRED. Not applied to production by the MVP pilot.
-- New, unexposed schema only; existing prediction/news/odds/coupon tables unchanged.
CREATE SCHEMA IF NOT EXISTS ai_research;
REVOKE ALL ON SCHEMA ai_research FROM PUBLIC;

DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='lol_research_runtime') THEN
  CREATE ROLE lol_research_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
 END IF;
END $$;

CREATE TABLE IF NOT EXISTS ai_research.service_keys (
 key_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
 scopes TEXT NOT NULL, expires_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_research.jobs (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, match_id TEXT NOT NULL,
 mode TEXT NOT NULL CHECK(mode IN ('full','lineup_check')),
 status TEXT NOT NULL CHECK(status IN ('queued','claimed','completed','expired')),
 deadline TEXT NOT NULL, input_version TEXT NOT NULL, context_json TEXT NOT NULL,
 request_id TEXT NOT NULL, created_at TEXT NOT NULL, lease_hash TEXT,
 lease_expires_at TEXT, report_id TEXT,
 UNIQUE(owner_id,match_id,mode,input_version,request_id)
);
CREATE INDEX IF NOT EXISTS ix_jobs_owner ON ai_research.jobs(owner_id,status,deadline);
CREATE TABLE IF NOT EXISTS ai_research.reports (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, match_id TEXT NOT NULL,
 mode TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(owner_id,match_id,mode)
);
CREATE TABLE IF NOT EXISTS ai_research.report_revisions (
 id TEXT PRIMARY KEY, report_id TEXT NOT NULL REFERENCES ai_research.reports(id),
 revision INTEGER NOT NULL, job_id TEXT NOT NULL UNIQUE REFERENCES ai_research.jobs(id),
 idempotency_key TEXT NOT NULL, payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
 received_at TEXT NOT NULL, researched_at TEXT NOT NULL, skill_version TEXT NOT NULL,
 freshness_status TEXT NOT NULL CHECK(freshness_status IN ('current','expired')),
 validation_status TEXT NOT NULL CHECK(validation_status='schema_valid'),
 UNIQUE(report_id,revision)
);
CREATE TABLE IF NOT EXISTS ai_research.rate_limits (
 bucket TEXT NOT NULL, window_id BIGINT NOT NULL, hits INTEGER NOT NULL,
 PRIMARY KEY(bucket,window_id)
);

-- Owner-executed views expose only approved source columns to the runtime role.
-- Do not grant the role access to underlying tables or the entire meta table.
CREATE OR REPLACE VIEW ai_research.source_matches AS
 SELECT id,league,kickoff,home,away,home_goals,away_goals,completed,status FROM public.matches;
CREATE OR REPLACE VIEW ai_research.source_events AS
 SELECT id,match_id,league,kickoff,home,away,updated_at,competition_id,league_name,country,is_small FROM public.iddaa_events;
CREATE OR REPLACE VIEW ai_research.source_odds AS
 SELECT event_id,market,odd FROM public.iddaa_odds;
CREATE OR REPLACE VIEW ai_research.source_news AS
 SELECT id,league,team,title,link,source,published,injury FROM public.news;
CREATE OR REPLACE VIEW ai_research.source_predictions AS
 SELECT match_id,captured_at,model_version,data_version,payload FROM public.prediction_snapshots;
CREATE OR REPLACE VIEW ai_research.source_meta AS
 SELECT key,value FROM public.meta WHERE key IN ('last_updated_at','iddaa_last_update','update_data_version','iddaa_quote_provenance')
 OR left(key,length('iddaa_quote_provenance:'))='iddaa_quote_provenance:';

REVOKE ALL ON ALL TABLES IN SCHEMA ai_research FROM PUBLIC;
GRANT USAGE ON SCHEMA ai_research TO lol_research_runtime;
GRANT SELECT ON ai_research.service_keys,ai_research.source_matches,ai_research.source_events,
 ai_research.source_odds,ai_research.source_news,ai_research.source_predictions,ai_research.source_meta
 TO lol_research_runtime;
GRANT SELECT,UPDATE ON ai_research.jobs TO lol_research_runtime;
GRANT SELECT,INSERT,UPDATE ON ai_research.reports TO lol_research_runtime;
GRANT SELECT,INSERT ON ai_research.report_revisions TO lol_research_runtime;
GRANT SELECT,INSERT,UPDATE,DELETE ON ai_research.rate_limits TO lol_research_runtime;
-- No login/password is created. No anon/authenticated/service_role grants are added.
-- A reviewed nonproduction login may later receive membership in this NOLOGIN role.
