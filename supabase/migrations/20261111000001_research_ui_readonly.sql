-- REVIEW REQUIRED: no production apply or login/secret is created here.
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='lol_research_ui_reader') THEN
  CREATE ROLE lol_research_ui_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
 END IF;
END $$;
CREATE OR REPLACE VIEW ai_research.ui_reports AS
 SELECT id,owner_id,match_id,mode FROM ai_research.reports;
CREATE OR REPLACE VIEW ai_research.ui_revisions AS
 SELECT v.report_id,v.revision,v.payload_json,v.researched_at,v.received_at,v.freshness_status,v.validation_status,
 jsonb_build_object('prediction_snapshot', CASE WHEN (j.context_json::jsonb)->'prediction_snapshot' IS NULL
                 OR (j.context_json::jsonb)->'prediction_snapshot'='null'::jsonb THEN NULL ELSE '{}'::jsonb END,
     'prices',jsonb_build_object('observed_at',(j.context_json::jsonb)#>'{prices,observed_at}',
        'quote_provenance',jsonb_build_object(
          'pilot_freshness_status',(j.context_json::jsonb)#>'{prices,quote_provenance,pilot_freshness_status}',
          'pilot_research_limitations',(j.context_json::jsonb)#>'{prices,quote_provenance,pilot_research_limitations}')))::text AS context_json
 FROM ai_research.report_revisions v JOIN ai_research.jobs j ON j.id=v.job_id;
REVOKE ALL ON ai_research.ui_reports,ai_research.ui_revisions FROM PUBLIC;
GRANT USAGE ON SCHEMA ai_research TO lol_research_ui_reader;
GRANT SELECT ON ai_research.ui_reports,ai_research.ui_revisions TO lol_research_ui_reader;
