BEGIN;

CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL, 
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> 0001

CREATE TABLE audit_logs (
    user_id UUID, 
    action VARCHAR(64) NOT NULL, 
    meta JSONB NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id)
);

CREATE INDEX ix_audit_logs_user_id ON audit_logs (user_id);

CREATE TABLE economic_series (
    source VARCHAR(16) NOT NULL, 
    series_code VARCHAR(64) NOT NULL, 
    geography VARCHAR(16) NOT NULL, 
    category VARCHAR(32) NOT NULL, 
    title VARCHAR(255) NOT NULL, 
    unit VARCHAR(64) NOT NULL, 
    frequency VARCHAR(8) NOT NULL, 
    last_observation_date DATE, 
    fetched_at TIMESTAMP WITH TIME ZONE, 
    status VARCHAR(16) NOT NULL, 
    error TEXT, 
    id UUID NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT uq_series_source_code_geo UNIQUE (source, series_code, geography)
);

CREATE TABLE fx_rates (
    base VARCHAR(3) NOT NULL, 
    quote VARCHAR(3) NOT NULL, 
    rate_date DATE NOT NULL, 
    rate FLOAT NOT NULL, 
    source VARCHAR(16) NOT NULL, 
    series_code VARCHAR(32) NOT NULL, 
    PRIMARY KEY (base, quote, rate_date)
);

CREATE TABLE profiles (
    id UUID NOT NULL, 
    email VARCHAR(320), 
    display_name VARCHAR(120), 
    base_currency VARCHAR(3) NOT NULL, 
    country VARCHAR(2) NOT NULL, 
    date_order VARCHAR(3), 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id)
);

CREATE TABLE agent_conversations (
    user_id UUID NOT NULL, 
    title VARCHAR(120) NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(user_id) REFERENCES profiles (id) ON DELETE CASCADE
);

CREATE INDEX ix_agent_conversations_user_id ON agent_conversations (user_id);

CREATE TABLE economic_observations (
    series_id UUID NOT NULL, 
    obs_date DATE NOT NULL, 
    value FLOAT NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (series_id, obs_date), 
    FOREIGN KEY(series_id) REFERENCES economic_series (id) ON DELETE CASCADE
);

CREATE INDEX ix_obs_series_date ON economic_observations (series_id, obs_date);

CREATE TABLE forecasts (
    user_id UUID NOT NULL, 
    request JSONB NOT NULL, 
    result JSONB NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(user_id) REFERENCES profiles (id) ON DELETE CASCADE
);

CREATE INDEX ix_forecasts_user_id ON forecasts (user_id);

CREATE TABLE import_batches (
    user_id UUID NOT NULL, 
    filename VARCHAR(120) NOT NULL, 
    file_sha256 VARCHAR(64) NOT NULL, 
    row_count INTEGER NOT NULL, 
    imported_count INTEGER NOT NULL, 
    duplicate_count INTEGER NOT NULL, 
    rejected_count INTEGER NOT NULL, 
    normalization JSONB NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(user_id) REFERENCES profiles (id) ON DELETE CASCADE
);

CREATE INDEX ix_import_batches_user_id ON import_batches (user_id);

CREATE TABLE portfolio_simulations (
    user_id UUID NOT NULL, 
    request JSONB NOT NULL, 
    result JSONB NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(user_id) REFERENCES profiles (id) ON DELETE CASCADE
);

CREATE INDEX ix_portfolio_simulations_user_id ON portfolio_simulations (user_id);

CREATE TABLE agent_messages (
    conversation_id UUID NOT NULL, 
    user_id UUID NOT NULL, 
    role VARCHAR(16) NOT NULL, 
    content TEXT NOT NULL, 
    meta JSONB NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(conversation_id) REFERENCES agent_conversations (id) ON DELETE CASCADE, 
    FOREIGN KEY(user_id) REFERENCES profiles (id) ON DELETE CASCADE
);

CREATE INDEX ix_agent_messages_conversation_id ON agent_messages (conversation_id);

CREATE INDEX ix_agent_messages_user_id ON agent_messages (user_id);

CREATE TABLE transactions (
    user_id UUID NOT NULL, 
    import_id UUID, 
    booked_on DATE NOT NULL, 
    description VARCHAR(140) NOT NULL, 
    merchant VARCHAR(120), 
    merchant_key VARCHAR(80) NOT NULL, 
    amount NUMERIC(18, 4) NOT NULL, 
    currency VARCHAR(3) NOT NULL, 
    kind VARCHAR(16) NOT NULL, 
    category VARCHAR(32) NOT NULL, 
    category_source VARCHAR(16) NOT NULL, 
    category_confidence FLOAT, 
    user_corrected BOOLEAN NOT NULL, 
    dedupe_key VARCHAR(64) NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(import_id) REFERENCES import_batches (id) ON DELETE SET NULL, 
    FOREIGN KEY(user_id) REFERENCES profiles (id) ON DELETE CASCADE, 
    CONSTRAINT uq_transactions_user_dedupe UNIQUE (user_id, dedupe_key)
);

CREATE INDEX ix_transactions_import_id ON transactions (import_id);

CREATE INDEX ix_transactions_merchant_key ON transactions (merchant_key);

CREATE INDEX ix_transactions_user_date ON transactions (user_id, booked_on);

CREATE INDEX ix_transactions_user_id ON transactions (user_id);

alter table profiles add constraint fk_profiles_auth_users foreign key (id) references auth.users(id) on delete cascade;

alter table profiles enable row level security;

create policy profiles_owner on profiles for all using (id = (select auth.uid())) with check (id = (select auth.uid()));

alter table import_batches enable row level security;

create policy import_batches_owner on import_batches for all using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));

alter table transactions enable row level security;

create policy transactions_owner on transactions for all using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));

alter table forecasts enable row level security;

create policy forecasts_owner on forecasts for all using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));

alter table portfolio_simulations enable row level security;

create policy portfolio_simulations_owner on portfolio_simulations for all using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));

alter table agent_conversations enable row level security;

create policy agent_conversations_owner on agent_conversations for all using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));

alter table agent_messages enable row level security;

create policy agent_messages_owner on agent_messages for all using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));

alter table audit_logs enable row level security;

alter table economic_series enable row level security;

create policy economic_series_read on economic_series for select using (true);

alter table economic_observations enable row level security;

create policy economic_observations_read on economic_observations for select using (true);

alter table fx_rates enable row level security;

create policy fx_rates_read on fx_rates for select using (true);

INSERT INTO alembic_version (version_num) VALUES ('0001') RETURNING alembic_version.version_num;

COMMIT;

