-- REVIEW REQUIRED. Forward-only measurement; no historical rows are promoted.
-- The trusted model writer is a security boundary; no LOGIN or role membership is granted.
-- The earlier draft has weaker guarantees: retain any existing rows but never expose it to application roles.
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='research_evaluation') THEN
  REVOKE ALL ON ALL TABLES IN SCHEMA research_evaluation FROM PUBLIC,lol_research_runtime,lol_research_ui_reader;
  COMMENT ON SCHEMA research_evaluation IS 'LEGACY UNVERIFIED DRAFT: not a trustworthy measurement source';
 END IF;
END $$;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='lol_measure_model_writer') THEN
  CREATE ROLE lol_measure_model_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='lol_measure_operator') THEN
  CREATE ROLE lol_measure_operator NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='lol_measure_result_writer') THEN
  CREATE ROLE lol_measure_result_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='lol_measure_quality_reviewer') THEN
  CREATE ROLE lol_measure_quality_reviewer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
 END IF;
END $$;
CREATE SCHEMA IF NOT EXISTS research_measurement;
REVOKE ALL ON SCHEMA research_measurement FROM PUBLIC;
CREATE TABLE research_measurement.model_events (
 id TEXT PRIMARY KEY, match_id TEXT NOT NULL, canonical_id TEXT NOT NULL REFERENCES ai_research.match_identities(canonical_id),
 kickoff TIMESTAMPTZ NOT NULL, captured_at TIMESTAMPTZ NOT NULL,
 model_version TEXT NOT NULL, payload JSONB NOT NULL, payload_hash TEXT NOT NULL,
 writer_login TEXT NOT NULL, UNIQUE(match_id)
);
CREATE TABLE research_measurement.candidates (
 id TEXT PRIMARY KEY, model_event_id TEXT NOT NULL REFERENCES research_measurement.model_events(id),
 canonical_id TEXT NOT NULL REFERENCES ai_research.match_identities(canonical_id),
 kickoff TIMESTAMPTZ NOT NULL, captured_at TIMESTAMPTZ NOT NULL,
 market TEXT NOT NULL, selection TEXT NOT NULL,
 independent_model_probability NUMERIC, market_probability NUMERIC, blended_probability NUMERIC,
 model_confidence JSONB, odds NUMERIC, odds_observed_at TIMESTAMPTZ, odds_source TEXT, quote_evidence JSONB,
 CHECK(independent_model_probability IS NULL OR independent_model_probability BETWEEN 0 AND 1),
 CHECK(market_probability IS NULL OR market_probability BETWEEN 0 AND 1),
 CHECK(blended_probability IS NULL OR blended_probability BETWEEN 0 AND 1),
 CHECK(odds IS NULL OR (odds>1 AND odds<10000)),
 CHECK((odds IS NULL AND odds_observed_at IS NULL AND odds_source IS NULL) OR
       (odds IS NOT NULL AND odds_observed_at IS NOT NULL AND odds_source='iddaa')),
 UNIQUE(model_event_id,market,selection)
);
CREATE TABLE research_measurement.pools (
 id TEXT PRIMARY KEY, day DATE NOT NULL, frozen_at TIMESTAMPTZ NOT NULL,
 candidate_ids TEXT[] NOT NULL CHECK(cardinality(candidate_ids)<=100)
);
CREATE TABLE research_measurement.decisions (
 pool_id TEXT NOT NULL REFERENCES research_measurement.pools(id), candidate_id TEXT NOT NULL REFERENCES research_measurement.candidates(id),
 recorded_at TIMESTAMPTZ NOT NULL, research_state TEXT NOT NULL CHECK(research_state IN ('complete','unreviewed','failed','incomplete','rejected')),
 decision TEXT NOT NULL CHECK(decision IN ('keep','avoid','uncertain','not_reviewed')),
 report_id TEXT, report_revision INTEGER, PRIMARY KEY(pool_id,candidate_id),
 FOREIGN KEY(report_id,report_revision) REFERENCES ai_research.report_revisions(report_id,revision),
 CHECK ((report_id IS NULL AND report_revision IS NULL) OR (report_id IS NOT NULL AND report_revision IS NOT NULL))
);
CREATE TABLE research_measurement.review_attestations (
 report_id TEXT NOT NULL, report_revision INTEGER NOT NULL, reviewed_at TIMESTAMPTZ NOT NULL,
 reviewed_by TEXT NOT NULL, reason TEXT NOT NULL CHECK(length(trim(reason))>0),
 PRIMARY KEY(report_id,report_revision),
 FOREIGN KEY(report_id,report_revision) REFERENCES ai_research.report_revisions(report_id,revision)
);
CREATE TABLE research_measurement.path_outcomes (
 pool_id TEXT NOT NULL REFERENCES research_measurement.pools(id), path TEXT NOT NULL CHECK(path IN ('A','B')),
 outcome TEXT NOT NULL CHECK(outcome IN ('coupon','no_coupon')), frozen_at TIMESTAMPTZ NOT NULL,
 legs JSONB, odds_product NUMERIC, no_coupon_reason TEXT,
 PRIMARY KEY(pool_id,path),
 CHECK((outcome='coupon' AND jsonb_typeof(legs)='array' AND jsonb_array_length(legs)=3 AND odds_product>1 AND no_coupon_reason IS NULL)
 OR (outcome='no_coupon' AND legs IS NULL AND odds_product IS NULL AND length(trim(no_coupon_reason))>0))
);
CREATE TABLE research_measurement.result_observations (
 canonical_id TEXT NOT NULL REFERENCES ai_research.match_identities(canonical_id), revision INTEGER NOT NULL,
 recorded_at TIMESTAMPTZ NOT NULL, source TEXT NOT NULL, source_observed_at TIMESTAMPTZ NOT NULL,
 home_goals INTEGER NOT NULL CHECK(home_goals>=0), away_goals INTEGER NOT NULL CHECK(away_goals>=0),
 source_hash TEXT NOT NULL, writer_login TEXT NOT NULL, verification_status TEXT NOT NULL CHECK(verification_status='unverified'), PRIMARY KEY(canonical_id,revision)
);
REVOKE ALL ON ALL TABLES IN SCHEMA research_measurement FROM PUBLIC, lol_measure_model_writer, lol_measure_operator, lol_measure_result_writer,lol_measure_quality_reviewer;
GRANT USAGE ON SCHEMA research_measurement TO lol_measure_operator,lol_measure_result_writer,lol_measure_quality_reviewer;

-- Hash the entire server-recorded envelope, not just the supplied model payload.
CREATE FUNCTION research_measurement.event_hash(p_match text,p_canonical text,p_kickoff timestamptz,
 p_captured timestamptz,p_version text,p_payload jsonb,p_writer text) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT encode(sha256(convert_to(jsonb_build_array(p_match,p_canonical,
   to_char(p_kickoff AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US'),
   to_char(p_captured AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US'),
   p_version,p_payload,p_writer)::text,'UTF8')),'hex');
$$;

-- Observe only new inserts by an explicitly trusted backend login; failure must not block the model.
CREATE FUNCTION research_measurement.observe_model_insert() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,research_measurement,ai_research AS $$
DECLARE now_at timestamptz:=clock_timestamp(); identity_row ai_research.match_identities%ROWTYPE; raw jsonb;
BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=session_user AND (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb))
    OR NOT pg_has_role(session_user,'lol_measure_model_writer','MEMBER') THEN RETURN NEW; END IF;
 SELECT i.* INTO identity_row FROM ai_research.match_aliases a JOIN ai_research.match_identities i ON i.canonical_id=a.canonical_id
  JOIN public.matches m ON m.id=NEW.match_id
  WHERE a.alias=NEW.match_id AND a.source_competition=NEW.league AND a.source_kickoff=i.kickoff
    AND m.league=a.source_competition AND m.home=a.source_home AND m.away=a.source_away
    AND m.kickoff::timestamptz=a.source_kickoff AND m.completed=0
    AND lower(COALESCE(m.status,'')) !~ '(postpon|cancel|abandon|suspend)';
 IF NOT FOUND OR NEW.kickoff !~ '(Z|[+-][0-9]{2}:[0-9]{2})$' OR NEW.captured_at !~ '(Z|[+-][0-9]{2}:[0-9]{2})$'
    OR identity_row.kickoff<=now_at OR NEW.kickoff::timestamptz<>identity_row.kickoff
    OR NEW.captured_at::timestamptz < now_at-interval '2 minutes' OR NEW.captured_at::timestamptz>now_at+interval '5 seconds' THEN RETURN NEW; END IF;
 raw:=NEW.payload::jsonb;
 INSERT INTO research_measurement.model_events(id,match_id,canonical_id,kickoff,captured_at,model_version,payload,payload_hash,writer_login)
 VALUES(gen_random_uuid()::text,NEW.match_id,identity_row.canonical_id,identity_row.kickoff,now_at,NEW.model_version,raw,
        research_measurement.event_hash(NEW.match_id,identity_row.canonical_id,identity_row.kickoff,now_at,NEW.model_version,raw,session_user),session_user);
 RETURN NEW;
EXCEPTION WHEN OTHERS THEN RETURN NEW;
END $$;
CREATE TRIGGER research_measurement_observe_model AFTER INSERT ON public.prediction_snapshots
 FOR EACH ROW EXECUTE FUNCTION research_measurement.observe_model_insert();

-- This records only completed 90-minute results from a separately approved writer.
-- Granting that role is an operational trust decision; source lineage is not inferred.
CREATE FUNCTION research_measurement.observe_final_match() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,research_measurement,ai_research AS $$
DECLARE identity_row ai_research.match_identities%ROWTYPE; now_at timestamptz:=clock_timestamp();
        digest text; last_hash text; next_revision integer;
BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=session_user AND (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb))
    OR NOT pg_has_role(session_user,'lol_measure_result_writer','MEMBER') THEN RETURN NEW; END IF;
 SELECT i.* INTO identity_row FROM ai_research.match_aliases a JOIN ai_research.match_identities i ON i.canonical_id=a.canonical_id
  WHERE a.alias=NEW.id AND a.source_competition=NEW.league AND a.source_home=NEW.home AND a.source_away=NEW.away
   AND a.source_kickoff=i.kickoff AND NEW.kickoff::timestamptz=i.kickoff;
 IF NOT FOUND OR now_at<=identity_row.kickoff OR NEW.completed<>1 OR NEW.home_goals IS NULL OR NEW.away_goals IS NULL
    OR NEW.home_goals<0 OR NEW.away_goals<0 OR lower(COALESCE(NEW.status,'')) NOT IN ('ft','final','status_final') THEN RETURN NEW; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('measure-result:'||identity_row.canonical_id,0));
 now_at:=clock_timestamp();
 digest:=encode(sha256(convert_to(jsonb_build_array(NEW.home_goals,NEW.away_goals,NEW.status)::text,'UTF8')),'hex');
 SELECT source_hash INTO last_hash FROM research_measurement.result_observations WHERE canonical_id=identity_row.canonical_id ORDER BY revision DESC LIMIT 1;
 IF last_hash=digest THEN RETURN NEW; END IF;
 SELECT COALESCE(max(revision),0)+1 INTO next_revision FROM research_measurement.result_observations WHERE canonical_id=identity_row.canonical_id;
 INSERT INTO research_measurement.result_observations VALUES(identity_row.canonical_id,next_revision,now_at,
   'trusted_backend_match_cache',now_at,NEW.home_goals,NEW.away_goals,digest,session_user,'unverified');
 RETURN NEW;
EXCEPTION WHEN OTHERS THEN RETURN NEW;
END $$;
CREATE TRIGGER research_measurement_observe_final AFTER INSERT OR UPDATE OF home_goals,away_goals,completed,status ON public.matches
 FOR EACH ROW EXECUTE FUNCTION research_measurement.observe_final_match();

CREATE FUNCTION research_measurement.match_still_upcoming(p_event text,p_kickoff timestamptz) RETURNS boolean
LANGUAGE sql VOLATILE SET search_path=pg_catalog,research_measurement AS $$
 SELECT EXISTS(SELECT 1 FROM research_measurement.model_events e JOIN public.matches m ON m.id=e.match_id
  WHERE e.id=p_event AND e.kickoff=p_kickoff AND m.kickoff::timestamptz=p_kickoff
   AND m.kickoff::timestamptz>clock_timestamp() AND m.completed=0
   AND lower(COALESCE(m.status,'')) !~ '(postpon|cancel|abandon|suspend)');
$$;

CREATE FUNCTION research_measurement.capture_candidate(p_match text,p_market text,p_selection text) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,research_measurement,ai_research AS $$
DECLARE ev research_measurement.model_events%ROWTYPE; q jsonb; price numeric; observed timestamptz; market_p numeric; blend_p numeric; out_id text; now_at timestamptz:=clock_timestamp();
BEGIN
 IF NOT pg_has_role(session_user,'lol_measure_operator','MEMBER') THEN RAISE EXCEPTION 'MEASURE_FORBIDDEN'; END IF;
 -- Existing odds keys encode the selection; do not attach the price to a different pick.
 IF p_market IS NULL OR length(trim(p_market))=0 OR p_selection IS DISTINCT FROM p_market THEN RAISE EXCEPTION 'MEASURE_INVALID_SELECTION'; END IF;
 SELECT * INTO ev FROM research_measurement.model_events WHERE match_id=p_match;
 IF NOT FOUND OR now_at>=ev.kickoff OR NOT research_measurement.match_still_upcoming(ev.id,ev.kickoff)
    OR ev.payload_hash<>research_measurement.event_hash(ev.match_id,ev.canonical_id,ev.kickoff,ev.captured_at,ev.model_version,ev.payload,ev.writer_login) THEN RAISE EXCEPTION 'MEASURE_SNAPSHOT_UNTRUSTED'; END IF;
 IF jsonb_typeof(ev.payload->'market'->p_market)='number' THEN market_p:=(ev.payload->'market'->>p_market)::numeric; END IF;
 IF jsonb_typeof(ev.payload->'blend'->p_market)='number' THEN blend_p:=(ev.payload->'blend'->>p_market)::numeric; END IF;
 q:=ev.payload #> ARRAY['quote_provenance','quotes',p_market];
 IF ev.payload->'odds' ? p_market THEN
  IF jsonb_typeof(ev.payload->'odds'->p_market)<>'number' THEN RAISE EXCEPTION 'MEASURE_INVALID_ODDS'; END IF;
  price:=(ev.payload->'odds'->>p_market)::numeric;
  IF price<=1 OR price>=10000 OR price::text IN ('NaN','Infinity','-Infinity') THEN RAISE EXCEPTION 'MEASURE_INVALID_ODDS'; END IF;
  IF q IS NULL OR ev.payload #>> '{quote_provenance,quote_source}' IS DISTINCT FROM 'iddaa'
     OR q->>'cache_status' IS DISTINCT FROM 'network_no_cache_evidence'
     OR jsonb_typeof(q->'odd') IS DISTINCT FROM 'number' OR (q->>'odd')::numeric<>price
     OR jsonb_typeof(q->'identity') IS DISTINCT FROM 'array' OR jsonb_array_length(q->'identity')<>7
     OR NOT EXISTS(SELECT 1 FROM public.iddaa_events e
        JOIN public.iddaa_odds o ON o.event_id=e.id AND o.market=p_market
        JOIN ai_research.match_aliases a ON a.alias='iddaa:'||e.id AND a.provider='iddaa'
        WHERE e.id=q->'identity'->>0 AND e.match_id=ev.match_id AND a.canonical_id=ev.canonical_id
          AND e.kickoff::timestamptz=ev.kickoff AND o.odd::numeric=price)
     THEN RAISE EXCEPTION 'MEASURE_PRICE_UNVERIFIED'; END IF;
  IF COALESCE(q->>'quote_observed_at','') !~ '(Z|[+-][0-9]{2}:[0-9]{2})$' THEN RAISE EXCEPTION 'MEASURE_PRICE_UNVERIFIED'; END IF;
  BEGIN observed:=(q->>'quote_observed_at')::timestamptz; EXCEPTION WHEN OTHERS THEN RAISE EXCEPTION 'MEASURE_PRICE_UNVERIFIED'; END;
  IF observed IS NULL OR observed>ev.captured_at OR observed>=ev.kickoff OR now_at-observed>interval '2 hours' THEN RAISE EXCEPTION 'MEASURE_PRICE_STALE'; END IF;
 END IF;
 out_id:=gen_random_uuid()::text;
 INSERT INTO research_measurement.candidates VALUES(out_id,ev.id,ev.canonical_id,ev.kickoff,now_at,p_market,p_selection,
   NULL,market_p,blend_p,ev.payload->'confidence',price,observed,CASE WHEN price IS NOT NULL THEN 'iddaa' END,q);
 RETURN out_id;
END $$;

CREATE FUNCTION research_measurement.freeze_pool(p_candidates text[]) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,research_measurement AS $$
DECLARE out_id text:=gen_random_uuid()::text; n int; now_at timestamptz:=clock_timestamp();
BEGIN
 IF NOT pg_has_role(session_user,'lol_measure_operator','MEMBER') THEN RAISE EXCEPTION 'MEASURE_FORBIDDEN'; END IF;
 IF p_candidates IS NULL OR cardinality(p_candidates)>100 THEN RAISE EXCEPTION 'MEASURE_INVALID_POOL'; END IF;
 SELECT count(*) INTO n FROM research_measurement.candidates WHERE id=ANY(p_candidates) AND captured_at<=now_at
  AND kickoff>now_at AND research_measurement.match_still_upcoming(model_event_id,kickoff);
 IF n<>cardinality(p_candidates) THEN RAISE EXCEPTION 'MEASURE_INVALID_POOL'; END IF;
 INSERT INTO research_measurement.pools VALUES(out_id,(now_at AT TIME ZONE 'Europe/Istanbul')::date,now_at,p_candidates);
 RETURN out_id;
END $$;

-- Schema-valid is not a quality verdict: a separate authorized human must attest it.
CREATE FUNCTION research_measurement.attest_complete(p_report text,p_revision integer,p_reason text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,research_measurement,ai_research AS $$
DECLARE r ai_research.report_revisions%ROWTYPE; ko timestamptz; data jsonb;
BEGIN
 IF NOT pg_has_role(session_user,'lol_measure_quality_reviewer','MEMBER') THEN RAISE EXCEPTION 'MEASURE_FORBIDDEN'; END IF;
 IF p_reason IS NULL OR length(trim(p_reason))=0 THEN RAISE EXCEPTION 'MEASURE_REASON_REQUIRED'; END IF;
 SELECT v.* INTO r FROM ai_research.report_revisions v WHERE v.report_id=p_report AND v.revision=p_revision;
 IF NOT FOUND THEN RAISE EXCEPTION 'MEASURE_INCOMPLETE_RESEARCH'; END IF;
 SELECT i.kickoff INTO ko FROM ai_research.jobs j JOIN ai_research.match_identities i ON i.canonical_id=j.canonical_id WHERE j.id=r.job_id;
 data:=r.payload_json::jsonb;
 IF ko IS NULL OR ko<=clock_timestamp() OR r.freshness_status<>'current' OR r.validation_status<>'schema_valid'
    OR r.researched_at::timestamptz>=ko OR r.received_at::timestamptz>clock_timestamp()
    OR jsonb_typeof(data->'findings') IS DISTINCT FROM 'array' OR jsonb_array_length(data->'findings')=0
    OR jsonb_typeof(data->'sources') IS DISTINCT FROM 'array' OR jsonb_array_length(data->'sources')=0
    OR NOT EXISTS(SELECT 1 FROM jsonb_array_elements(data->'findings') x WHERE x->>'original_status' IS NOT NULL)
    THEN RAISE EXCEPTION 'MEASURE_INCOMPLETE_RESEARCH'; END IF;
 INSERT INTO research_measurement.review_attestations VALUES(p_report,p_revision,clock_timestamp(),session_user,p_reason);
END $$;

CREATE FUNCTION research_measurement.record_decision(p_pool text,p_candidate text,p_state text,p_decision text,p_report text DEFAULT NULL,p_revision integer DEFAULT NULL) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,research_measurement,ai_research AS $$
DECLARE c research_measurement.candidates%ROWTYPE; pool research_measurement.pools%ROWTYPE; now_at timestamptz:=clock_timestamp();
BEGIN
 IF NOT pg_has_role(session_user,'lol_measure_operator','MEMBER') THEN RAISE EXCEPTION 'MEASURE_FORBIDDEN'; END IF;
 SELECT * INTO pool FROM research_measurement.pools WHERE id=p_pool;
 SELECT * INTO c FROM research_measurement.candidates WHERE id=p_candidate;
 IF pool.id IS NULL OR c.id IS NULL OR NOT (p_candidate=ANY(pool.candidate_ids)) OR now_at>=c.kickoff
    OR NOT research_measurement.match_still_upcoming(c.model_event_id,c.kickoff)
    OR pool.day<>(now_at AT TIME ZONE 'Europe/Istanbul')::date THEN RAISE EXCEPTION 'MEASURE_INVALID_DECISION'; END IF;
 IF p_state='complete' THEN
  IF p_decision NOT IN ('keep','avoid','uncertain') THEN RAISE EXCEPTION 'MEASURE_INVALID_DECISION'; END IF;
  IF p_report IS NULL OR p_revision IS NULL OR NOT EXISTS(
    SELECT 1 FROM ai_research.report_revisions r JOIN ai_research.jobs j ON j.id=r.job_id
    JOIN research_measurement.review_attestations a ON a.report_id=r.report_id AND a.report_revision=r.revision
    WHERE r.report_id=p_report AND r.revision=p_revision AND j.canonical_id=c.canonical_id
     AND r.validation_status='schema_valid' AND r.freshness_status='current'
     AND r.researched_at::timestamptz<c.kickoff AND r.received_at::timestamptz<=now_at) THEN RAISE EXCEPTION 'MEASURE_INCOMPLETE_RESEARCH'; END IF;
 ELSIF p_report IS NOT NULL OR p_revision IS NOT NULL OR p_decision='keep' THEN
  RAISE EXCEPTION 'MEASURE_INCOMPLETE_RESEARCH';
 END IF;
 INSERT INTO research_measurement.decisions VALUES(p_pool,p_candidate,now_at,p_state,p_decision,p_report,p_revision);
END $$;

-- Pool/path outcome is a single row. All three frozen legs are built in one DB transaction.
CREATE FUNCTION research_measurement.freeze_outcome(p_pool text,p_path text,p_candidates text[],p_reason text DEFAULT NULL) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,research_measurement AS $$
DECLARE pool research_measurement.pools%ROWTYPE; c research_measurement.candidates%ROWTYPE; now_at timestamptz:=clock_timestamp();
        n int; distinct_matches int; legs jsonb:='[]'::jsonb; total numeric:=1;
BEGIN
 IF NOT pg_has_role(session_user,'lol_measure_operator','MEMBER') THEN RAISE EXCEPTION 'MEASURE_FORBIDDEN'; END IF;
 SELECT * INTO pool FROM research_measurement.pools WHERE id=p_pool FOR UPDATE;
 -- Hold source rows stable while validating and freezing the outcome.
 PERFORM m.id FROM research_measurement.candidates cand JOIN research_measurement.model_events e ON e.id=cand.model_event_id
  JOIN public.matches m ON m.id=e.match_id WHERE cand.id=ANY(pool.candidate_ids) ORDER BY m.id FOR SHARE OF m;
 PERFORM o.event_id FROM research_measurement.candidates cand JOIN public.iddaa_odds o
  ON o.event_id=cand.quote_evidence->'identity'->>0 AND o.market=cand.market
  WHERE cand.id=ANY(p_candidates) ORDER BY o.event_id,o.market FOR SHARE OF o;
 now_at:=clock_timestamp();
 IF pool.id IS NULL OR p_path NOT IN ('A','B') OR pool.day<>(now_at AT TIME ZONE 'Europe/Istanbul')::date THEN RAISE EXCEPTION 'MEASURE_INVALID_POOL'; END IF;
 IF EXISTS(SELECT 1 FROM research_measurement.candidates WHERE id=ANY(pool.candidate_ids)
    AND (kickoff<=now_at OR NOT research_measurement.match_still_upcoming(model_event_id,kickoff))) THEN RAISE EXCEPTION 'MEASURE_POOL_EXPIRED'; END IF;
 IF p_path='B' AND (SELECT count(*) FROM research_measurement.decisions WHERE pool_id=p_pool)<>cardinality(pool.candidate_ids) THEN RAISE EXCEPTION 'MEASURE_DECISIONS_INCOMPLETE'; END IF;
 IF p_candidates IS NULL THEN
  IF p_reason IS NULL OR length(trim(p_reason))=0 THEN RAISE EXCEPTION 'MEASURE_REASON_REQUIRED'; END IF;
  INSERT INTO research_measurement.path_outcomes VALUES(p_pool,p_path,'no_coupon',now_at,NULL,NULL,p_reason); RETURN;
 END IF;
 IF cardinality(p_candidates)<>3 OR p_reason IS NOT NULL THEN RAISE EXCEPTION 'MEASURE_THREE_LEGS_REQUIRED'; END IF;
 SELECT count(*),count(DISTINCT canonical_id) INTO n,distinct_matches FROM research_measurement.candidates WHERE id=ANY(p_candidates) AND id=ANY(pool.candidate_ids);
 IF n<>3 OR distinct_matches<>3 THEN RAISE EXCEPTION 'MEASURE_THREE_DISTINCT_MATCHES_REQUIRED'; END IF;
 IF p_path='B' AND (SELECT count(*) FROM research_measurement.decisions WHERE pool_id=p_pool AND candidate_id=ANY(p_candidates) AND research_state='complete' AND decision='keep')<>3 THEN RAISE EXCEPTION 'MEASURE_INCOMPLETE_RESEARCH'; END IF;
 FOR c IN SELECT * FROM research_measurement.candidates WHERE id=ANY(p_candidates) ORDER BY id LOOP
  IF c.kickoff<=now_at OR c.odds IS NULL OR c.odds<=1 OR c.odds>=10000 OR c.odds_observed_at IS NULL OR now_at-c.odds_observed_at>interval '2 hours' OR c.odds_observed_at>now_at
     OR NOT EXISTS(SELECT 1 FROM public.iddaa_events e JOIN public.iddaa_odds o ON o.event_id=e.id
       JOIN research_measurement.model_events me ON me.id=c.model_event_id AND e.match_id=me.match_id
       WHERE e.id=c.quote_evidence->'identity'->>0 AND o.market=c.market AND o.odd::numeric=c.odds) THEN
   RAISE EXCEPTION 'MEASURE_PRICE_STALE_OR_INVALID';
  END IF;
  legs:=legs||jsonb_build_array(jsonb_build_object('candidate_id',c.id,'canonical_id',c.canonical_id,'market',c.market,'selection',c.selection,'odds',c.odds,'odds_observed_at',c.odds_observed_at,'model_event_id',c.model_event_id));
  total:=total*c.odds;
 END LOOP;
 INSERT INTO research_measurement.path_outcomes VALUES(p_pool,p_path,'coupon',now_at,legs,total,NULL);
END $$;

-- No authoritative settlement source is configured in the existing database.
-- Never record a winning or final result based solely on an unverified cache.
CREATE FUNCTION research_measurement.record_final_result(p_canonical text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,research_measurement AS $$
BEGIN
 IF NOT pg_has_role(session_user,'lol_measure_result_writer','MEMBER') THEN RAISE EXCEPTION 'MEASURE_FORBIDDEN'; END IF;
 IF NOT EXISTS(SELECT 1 FROM ai_research.match_identities WHERE canonical_id=p_canonical AND kickoff<clock_timestamp()) THEN RAISE EXCEPTION 'MEASURE_RESULT_BEFORE_KICKOFF'; END IF;
 RAISE EXCEPTION 'MEASURE_TRUSTED_RESULT_SOURCE_NOT_CONFIGURED';
END $$;

CREATE FUNCTION research_measurement.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'MEASURE_APPEND_ONLY'; END $$;

-- Check again at normal transaction commit; model writer's original transaction is never blocked by this guard.
CREATE FUNCTION research_measurement.check_prematch_at_commit() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,research_measurement AS $$
DECLARE ids text[];
BEGIN
 IF TG_TABLE_NAME='candidates' THEN ids:=ARRAY[NEW.id];
 ELSIF TG_TABLE_NAME='pools' THEN ids:=NEW.candidate_ids;
 ELSIF TG_TABLE_NAME='decisions' THEN ids:=ARRAY[NEW.candidate_id];
 ELSE SELECT candidate_ids INTO ids FROM research_measurement.pools WHERE id=NEW.pool_id;
 END IF;
 IF ids IS NULL OR EXISTS(SELECT 1 FROM research_measurement.candidates cand WHERE cand.id=ANY(ids)
   AND (cand.kickoff<=clock_timestamp() OR NOT research_measurement.match_still_upcoming(cand.model_event_id,cand.kickoff))) THEN
  RAISE EXCEPTION 'MEASURE_COMMIT_AFTER_KICKOFF';
 END IF;
 IF TG_TABLE_NAME='path_outcomes' THEN
  IF NEW.outcome='coupon' AND EXISTS(
    SELECT 1 FROM jsonb_array_elements(NEW.legs) leg JOIN research_measurement.candidates cand ON cand.id=leg->>'candidate_id'
    WHERE cand.odds_observed_at IS NULL OR cand.odds_observed_at>clock_timestamp()
       OR clock_timestamp()-cand.odds_observed_at>interval '2 hours') THEN
   RAISE EXCEPTION 'MEASURE_PRICE_STALE_OR_INVALID';
  END IF;
 END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER candidate_commit_before_kickoff AFTER INSERT ON research_measurement.candidates
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION research_measurement.check_prematch_at_commit();
CREATE CONSTRAINT TRIGGER pool_commit_before_kickoff AFTER INSERT ON research_measurement.pools
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION research_measurement.check_prematch_at_commit();
CREATE CONSTRAINT TRIGGER decision_commit_before_kickoff AFTER INSERT ON research_measurement.decisions
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION research_measurement.check_prematch_at_commit();
CREATE CONSTRAINT TRIGGER outcome_commit_before_kickoff AFTER INSERT ON research_measurement.path_outcomes
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION research_measurement.check_prematch_at_commit();
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['model_events','candidates','pools','decisions','review_attestations','path_outcomes','result_observations'] LOOP
  EXECUTE format('CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON research_measurement.%I FOR EACH ROW EXECUTE FUNCTION research_measurement.reject_mutation()',t);
  EXECUTE format('CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON research_measurement.%I FOR EACH STATEMENT EXECUTE FUNCTION research_measurement.reject_mutation()',t);
 END LOOP;
END $$;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA research_measurement FROM PUBLIC;
GRANT EXECUTE ON FUNCTION research_measurement.capture_candidate(text,text,text),research_measurement.freeze_pool(text[]),
 research_measurement.record_decision(text,text,text,text,text,integer),research_measurement.freeze_outcome(text,text,text[],text) TO lol_measure_operator;
GRANT EXECUTE ON FUNCTION research_measurement.record_final_result(text) TO lol_measure_result_writer;
GRANT EXECUTE ON FUNCTION research_measurement.attest_complete(text,integer,text) TO lol_measure_quality_reviewer;
GRANT SELECT ON research_measurement.model_events,research_measurement.candidates,research_measurement.pools,
 research_measurement.decisions,research_measurement.path_outcomes,research_measurement.result_observations TO lol_measure_operator;
-- No role receives direct INSERT/UPDATE/DELETE/TRUNCATE and no automatic backfill occurs.
