-- Apply only after the research queue guard migration has been verified.
-- Separate NOLOGIN role; grant to a dedicated scheduler login, never the Bot.
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='lol_research_queue_writer') THEN
  CREATE ROLE lol_research_queue_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
 END IF;
END $$;
CREATE OR REPLACE VIEW ai_research.queue_principals AS
 SELECT key_id, principal_id, expires_at, revoked FROM ai_research.service_keys;
REVOKE ALL ON ai_research.queue_principals FROM PUBLIC, lol_research_runtime;
GRANT USAGE ON SCHEMA ai_research TO lol_research_queue_writer;
GRANT SELECT ON ai_research.queue_principals,ai_research.jobs,
 ai_research.source_matches,ai_research.source_events,ai_research.source_odds,
 ai_research.source_news,ai_research.source_predictions,ai_research.source_meta TO lol_research_queue_writer;
GRANT INSERT ON ai_research.jobs TO lol_research_queue_writer;
