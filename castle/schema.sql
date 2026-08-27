-- 城 ── 共通の骨
--
-- 方針: 種類ごとに列を足さない。骨は小さく固定し、種類ごとに違う項目は body(JSON) に持つ。
-- よく引く項目だけ生成列で肉から引き出し、索引を張る。列は増やさない。
--
-- body のキーのうち先頭が "_" のものは骨の運用情報（業務データではない）。
--   _key   … 自然キー。同じ元データを二度取り込んでも増えないようにする
--   _batch … 取り込みバッチID。取り消しの手掛かり

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS records (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL,   -- 種類（売上／労働時間／原価…）
  occurred_at TEXT NOT NULL,   -- いつ（YYYY-MM-DD）
  subject     TEXT,            -- 誰・何について（部門）
  status      TEXT NOT NULL,   -- 状態（confirmed／draft…）
  created_by  TEXT NOT NULL,   -- 誰が入れたか
  updated_at  TEXT NOT NULL,
  body        TEXT NOT NULL,   -- 肉：種類ごとに違う項目をJSONで持つ

  -- 肉から引き出した生成列（実体は持たない。索引のためだけに存在する）
  source_key  TEXT    GENERATED ALWAYS AS (json_extract(body, '$._key'))   VIRTUAL,
  batch_id    INTEGER GENERATED ALWAYS AS (json_extract(body, '$._batch')) VIRTUAL,
  amount      INTEGER GENERATED ALWAYS AS (json_extract(body, '$.amount')) VIRTUAL,
  hours       REAL    GENERATED ALWAYS AS (json_extract(body, '$.hours'))  VIRTUAL
);

CREATE INDEX        IF NOT EXISTS idx_records_kind  ON records(kind, occurred_at, subject);
CREATE INDEX        IF NOT EXISTS idx_records_batch ON records(batch_id);
-- NULL は重複可（SQLiteの仕様）。自然キーを持たない種類はここに乗らない。
CREATE UNIQUE INDEX IF NOT EXISTS idx_records_key   ON records(source_key);

-- 取り込みの実行ログ。「取り消せる操作は自動でよい」を成立させるための取り消し手段。
CREATE TABLE IF NOT EXISTS import_log (
  id        INTEGER PRIMARY KEY,
  ran_at    TEXT NOT NULL,
  source    TEXT NOT NULL,   -- 元ファイル名
  kind      TEXT NOT NULL,
  encoding  TEXT NOT NULL,   -- 判定した文字コード（判定ミスの追跡用）
  rows_ok   INTEGER NOT NULL,
  rows_skip INTEGER NOT NULL,
  note      TEXT NOT NULL DEFAULT '',
  undone_at TEXT
);
